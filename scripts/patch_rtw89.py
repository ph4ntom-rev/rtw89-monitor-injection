#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
"""
patch_rtw89.py

Automated source integrator for rtw89 monitor mode & diagnostic Radiotap TX
support (RTL8852AE / RTW89_CHIP_AX).

Supported kernel driver generations:
  - API Gen 1: Linux 6.1 - 6.10 (legacy struct rtw89_vif, rtwdev->mutex)
  - API Gen 2: Linux 6.11 - 7.x+ (MLO struct rtw89_vif_link, pure_monitor_mode_vif, wiphy_lock)
"""

import argparse
import platform
import re
import shutil
from pathlib import Path

MARKER = "RTW89_MON_EXT"
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"


def detect_tree_features(rtw89_dir: Path) -> dict:
    core_h = rtw89_dir / "core.h"
    if not core_h.is_file():
        raise FileNotFoundError(f"core.h not found in {rtw89_dir}")

    text = core_h.read_text(encoding="utf-8", errors="replace")
    has_vif_link = bool(re.search(r"\bstruct\s+rtw89_vif_link\s*\{", text))
    has_pure_mon = bool(re.search(r"\bpure_monitor_mode_vif\b", text))
    has_wiphy_lock = False

    mac80211_c = rtw89_dir / "mac80211.c"
    if mac80211_c.is_file():
        m_text = mac80211_c.read_text(encoding="utf-8", errors="replace")
        has_wiphy_lock = "lockdep_assert_wiphy" in m_text or "wiphy_lock(" in m_text

    return {
        "api_gen": 2 if has_vif_link else 1,
        "has_vif_link": has_vif_link,
        "has_pure_mon_vif": has_pure_mon,
        "uses_wiphy_lock": has_wiphy_lock,
        "host_os": platform.system(),
        "host_release": platform.release(),
    }


def find_c_function_span(src: str, func_name: str):
    """Locate (sig_start, brace_open, brace_close) of a C function definition."""
    pattern = re.compile(
        rf"(?:^|\n)([^\n;{{}}]*?\b{re.escape(func_name)}\s*\([^;{{}}]*\))\s*\{{",
        re.MULTILINE,
    )
    m = pattern.search(src)
    if not m:
        return None
    sig_start = m.start(1)
    brace_open = src.find("{", m.end(1))
    depth = 0
    i = brace_open
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (sig_start, brace_open, i)
        i += 1
    return None


def insert_include(src: str) -> str:
    inc_line = '#include "rtw89_mon.h"\n'
    if inc_line in src:
        return src
    m = list(re.finditer(r'^#include\s+"[^"]+"\s*$', src, re.MULTILINE))
    if not m:
        raise RuntimeError("Could not find local #include anchor")
    pos = m[-1].end()
    return src[:pos] + "\n" + inc_line.rstrip("\n") + src[pos:]


def patch_makefile(makefile_path: Path) -> bool:
    txt = makefile_path.read_text(encoding="utf-8", errors="replace")
    if "rtw89_mon.o" in txt:
        return False

    m = list(re.finditer(r"^(rtw89_core-(?:y|objs))\b", txt, re.MULTILINE))
    var_name = m[0].group(1) if m else "rtw89_core-y"
    addition = f"\n# {MARKER}\n{var_name} += rtw89_mon.o\n"
    makefile_path.write_text(txt.rstrip() + "\n" + addition, encoding="utf-8", newline="\n")
    return True


def patch_core_c(core_c_path: Path) -> list:
    src = core_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        return ["core.c already patched"]

    notes = []
    src = insert_include(src)

    # 1. rtw89_core_register_hw or rtw89_core_register -> before ieee80211_register_hw(...)
    reg_func = "rtw89_core_register_hw"
    span = find_c_function_span(src, reg_func)
    if not span or "ieee80211_register_hw" not in src[span[1]:span[2]]:
        reg_func = "rtw89_core_register"
        span = find_c_function_span(src, reg_func)
    if not span:
        raise RuntimeError("Neither rtw89_core_register_hw nor rtw89_core_register found in core.c")

    body = src[span[1]:span[2]]
    m = re.search(r"^(\s*)(?:ret\s*=\s*)?ieee80211_register_hw\s*\(", body, re.MULTILINE)
    if not m:
        raise RuntimeError(f"ieee80211_register_hw call not found in {reg_func}")
    indent = m.group(1)
    hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_register_hw_caps(rtwdev);\n"
    body = body[:m.start()] + hook + body[m.start():]
    src = src[:span[1]] + body + src[span[2]:]
    notes.append(f"hooked {reg_func}()")

    # 2. rtw89_set_channel -> right after MAC_0 __rtw89_set_channel (before AX early return) or before return 0
    span = find_c_function_span(src, "rtw89_set_channel")
    if span:
        body = src[span[1]:span[2]]
        mac0_m = re.search(
            r"^(\s*)__rtw89_set_channel\s*\([^;]*RTW89_MAC_0[^;]*\)\s*;",
            body,
            re.MULTILINE,
        )
        if mac0_m:
            indent = mac0_m.group(1)
            hook = f"\n{indent}/* {MARKER} */\n{indent}rtw89_mon_post_set_channel(rtwdev);"
            body = body[:mac0_m.end()] + hook + body[mac0_m.end():]
        else:
            rets = list(re.finditer(r"^(\s*)return\s+0\s*;", body, re.MULTILINE))
            if rets:
                first_ret = rets[0]
                indent = first_ret.group(1)
                hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_post_set_channel(rtwdev);\n"
                body = body[:first_ret.start()] + hook + body[first_ret.start():]
        src = src[:span[1]] + body + src[span[2]:]
        notes.append("hooked rtw89_set_channel()")

    # 3. rtw89_core_tx_write -> resolve NULL vif before vif_to_rtwvif(vif) dereference
    span = find_c_function_span(src, "rtw89_core_tx_write")
    if not span:
        raise RuntimeError("Function rtw89_core_tx_write not found in core.c")
    body = src[span[1]:span[2]]
    had_rtwvif_init = False
    if "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);" in body:
        body = body.replace(
            "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);",
            "struct rtw89_vif *rtwvif;",
            1,
        )
        had_rtwvif_init = True

    m = re.search(r"\n\n", body)
    if not m:
        raise RuntimeError("Could not locate end of declarations in rtw89_core_tx_write")
    assign_rtwvif = "\n\trtwvif = vif_to_rtwvif(vif);" if had_rtwvif_init else ""
    hook = (
        f"\n\n\t/* {MARKER}: resolve NULL vif on monitor TX */\n"
        f"\tvif = rtw89_mon_resolve_tx_vif(rtwdev, vif);\n"
        f"\tif (!vif)\n"
        f"\t\treturn -ENODEV;{assign_rtwvif}"
    )
    body = body[:m.start()] + hook + body[m.start():]
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_core_tx_write()")

    # 4. rtw89_core_tx_update_desc_info -> append rtw89_mon_fixup_tx_desc at end
    span = find_c_function_span(src, "rtw89_core_tx_update_desc_info")
    if not span:
        raise RuntimeError("Function rtw89_core_tx_update_desc_info not found in core.c")
    body = src[span[1]:span[2]]
    body = body.rstrip() + f"\n\n\t/* {MARKER} */\n\trtw89_mon_fixup_tx_desc(rtwdev, tx_req);\n"
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_core_tx_update_desc_info()")

    core_c_path.write_text(src, encoding="utf-8", newline="\n")
    return notes


def patch_mac80211_c(mac_c_path: Path) -> list:
    src = mac_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        return ["mac80211.c already patched"]

    notes = []
    src = insert_include(src)

    # 1. rtw89_ops_tx -> resolve NULL info->control.vif before vif_to_rtwvif(vif) dereference
    span = find_c_function_span(src, "rtw89_ops_tx")
    if span:
        body = src[span[1]:span[2]]
        if "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);" in body:
            body = body.replace(
                "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);",
                "struct rtw89_vif *rtwvif;",
                1,
            )
            m = re.search(r"\n\n", body)
            if m:
                hook = (
                    f"\n\n\t/* {MARKER}: guard against NULL control.vif on monitor TX */\n"
                    f"\tvif = rtw89_mon_resolve_tx_vif(rtwdev, vif);\n"
                    f"\tif (!vif) {{\n"
                    f"\t\tieee80211_free_txskb(hw, skb);\n"
                    f"\t\treturn;\n"
                    f"\t}}\n"
                    f"\trtwvif = vif_to_rtwvif(vif);"
                )
                body = body[:m.start()] + hook + body[m.start():]
                src = src[:span[1]] + body + src[span[2]:]
                notes.append("hooked rtw89_ops_tx()")

    # 2. rtw89_ops_add_interface -> on success path while lock is held
    span = find_c_function_span(src, "rtw89_ops_add_interface")
    if not span:
        raise RuntimeError("Function rtw89_ops_add_interface not found in mac80211.c")
    body = src[span[1]:span[2]]
    unlock_add = list(re.finditer(r"^(\s*)mutex_unlock\s*\(&rtwdev->mutex\)\s*;", body, re.MULTILINE))
    ret0_list = list(re.finditer(r"^(\s*)return\s+0\s*;", body, re.MULTILINE))
    if unlock_add:
        u = unlock_add[-1]
        indent = u.group(1)
        has_ret_var = bool(re.search(r"\bint\s+ret\b", body))
        cond = f"if (!ret)\n{indent}\t" if has_ret_var else ""
        hook = f"{indent}/* {MARKER} */\n{indent}{cond}rtw89_mon_add_vif_post(rtwdev, vif);\n"
        body = body[:u.start()] + hook + body[u.start():]
    elif ret0_list:
        r0 = ret0_list[0]
        indent = r0.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_add_vif_post(rtwdev, vif);\n"
        body = body[:r0.start()] + hook + body[r0.start():]
    else:
        rets = list(re.finditer(r"^(\s*)return\s+ret\s*;", body, re.MULTILINE))
        if not rets:
            raise RuntimeError("Could not find final return in rtw89_ops_add_interface")
        last_ret = rets[-1]
        indent = last_ret.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}if (!ret)\n{indent}\trtw89_mon_add_vif_post(rtwdev, vif);\n"
        body = body[:last_ret.start()] + hook + body[last_ret.start():]
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_ops_add_interface()")

    # 3. rtw89_ops_remove_interface -> after lock assertion / acquisition
    span = find_c_function_span(src, "rtw89_ops_remove_interface")
    if not span:
        raise RuntimeError("Function rtw89_ops_remove_interface not found in mac80211.c")
    body = src[span[1]:span[2]]
    lock_m = re.search(
        r"^(\s*)(?:mutex_lock\s*\(&rtwdev->mutex\)|lockdep_assert_wiphy\s*\([^)]+\))\s*;",
        body,
        re.MULTILINE,
    )
    if lock_m:
        indent = lock_m.group(1)
        hook = f"\n{indent}/* {MARKER} */\n{indent}rtw89_mon_remove_vif_pre(rtwdev, vif);"
        body = body[:lock_m.end()] + hook + body[lock_m.end():]
    else:
        m = re.search(r"\n\n", body)
        if not m:
            raise RuntimeError("Could not find insertion point in rtw89_ops_remove_interface")
        body = body[:m.start()] + f"\n\n\t/* {MARKER} */\n\trtw89_mon_remove_vif_pre(rtwdev, vif);" + body[m.start():]
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_ops_remove_interface()")

    # 4. rtw89_ops_configure_filter -> detect 3rd arg name (new_flags / total_flags) and hook after MAC_0 rx_fltr
    span = find_c_function_span(src, "rtw89_ops_configure_filter")
    if not span:
        raise RuntimeError("Function rtw89_ops_configure_filter not found in mac80211.c")
    sig = src[span[0]:span[1]]
    arg_m = re.search(r"unsigned\s+int\s*\*\s*(\w+)", sig)
    flags_arg = arg_m.group(1) if arg_m else "new_flags"

    body = src[span[1]:span[2]]
    mac0_fltr = re.search(
        r"^(\s*)rtw89_mac_set_rx_fltr\s*\(\s*rtwdev\s*,\s*RTW89_MAC_0\s*,[^;]+\)\s*;",
        body,
        re.MULTILINE,
    )
    unlock_m = list(re.finditer(r"^(\s*)mutex_unlock\s*\(&rtwdev->mutex\)\s*;", body, re.MULTILINE))
    if mac0_fltr:
        indent = mac0_fltr.group(1)
        hook = f"\n{indent}/* {MARKER} */\n{indent}rtw89_mon_apply_rx_filter(rtwdev, {flags_arg});"
        body = body[:mac0_fltr.end()] + hook + body[mac0_fltr.end():]
    elif unlock_m:
        u = unlock_m[-1]
        indent = unlock_m[-1].group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_apply_rx_filter(rtwdev, {flags_arg});\n"
        body = body[:u.start()] + hook + body[u.start():]
    else:
        body = body.rstrip() + f"\n\n\t/* {MARKER} */\n\trtw89_mon_apply_rx_filter(rtwdev, {flags_arg});\n"
    src = src[:span[1]] + body + src[span[2]:]
    notes.append(f"hooked rtw89_ops_configure_filter({flags_arg})")

    mac_c_path.write_text(src, encoding="utf-8", newline="\n")
    return notes


def backup_file(p: Path):
    bak = p.with_suffix(p.suffix + ".orig")
    if not bak.exists() and p.exists():
        shutil.copy2(p, bak)


def revert_tree(rtw89_dir: Path):
    for name in ("Makefile", "Kbuild", "core.c", "mac80211.c"):
        p = rtw89_dir / name
        bak = p.with_suffix(p.suffix + ".orig")
        if bak.exists():
            shutil.move(str(bak), str(p))
            print(f"[REVERT] Restored {p.name} from {bak.name}")
    for gen_file in ("rtw89_mon.c", "rtw89_mon.h", "rtw89_mon_autoconf.h"):
        gp = rtw89_dir / gen_file
        if gp.exists():
            gp.unlink()
            print(f"[REVERT] Removed {gp.name}")


def apply_tree(rtw89_dir: Path) -> dict:
    feats = detect_tree_features(rtw89_dir)
    for req in ("core.c", "mac80211.c"):
        if not (rtw89_dir / req).is_file():
            raise FileNotFoundError(
                f"Missing {req} in {rtw89_dir}. Point --rtw89-dir to full rtw89 kernel driver sources."
            )

    makefile = rtw89_dir / "Makefile"
    if not makefile.is_file():
        makefile = rtw89_dir / "Kbuild"
    if not makefile.is_file():
        raise FileNotFoundError(f"Neither Makefile nor Kbuild found in {rtw89_dir}")

    for target in (makefile, rtw89_dir / "core.c", rtw89_dir / "mac80211.c"):
        backup_file(target)

    autoconf = (
        "/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */\n"
        "/* Auto-generated by scripts/patch_rtw89.py */\n"
        "#ifndef __RTW89_MON_AUTOCONF_H__\n"
        "#define __RTW89_MON_AUTOCONF_H__\n\n"
        f"#define RTW89_MON_API_GEN {feats['api_gen']}\n"
        f"#define RTW89_MON_HAS_PURE_VIF {1 if feats['has_pure_mon_vif'] else 0}\n\n"
        "#endif /* __RTW89_MON_AUTOCONF_H__ */\n"
    )
    (rtw89_dir / "rtw89_mon_autoconf.h").write_text(autoconf, encoding="utf-8", newline="\n")

    shutil.copy2(SRC_DIR / "rtw89_mon.h", rtw89_dir / "rtw89_mon.h")
    shutil.copy2(SRC_DIR / "rtw89_mon.c", rtw89_dir / "rtw89_mon.c")
    print("[APPLY] Copied rtw89_mon.h, rtw89_mon.c, rtw89_mon_autoconf.h")

    if patch_makefile(makefile):
        print(f"[APPLY] Patched {makefile.name} (+= rtw89_mon.o)")
    for n in patch_core_c(rtw89_dir / "core.c"):
        print(f"[APPLY] core.c: {n}")
    for n in patch_mac80211_c(rtw89_dir / "mac80211.c"):
        print(f"[APPLY] mac80211.c: {n}")

    return feats


def main():
    ap = argparse.ArgumentParser(
        description="Integrate or revert rtw89 NL80211_IFTYPE_MONITOR & diagnostic TX support"
    )
    ap.add_argument("--rtw89-dir", required=True, help="Path to rtw89 driver source directory")
    ap.add_argument("--detect-only", action="store_true", help="Inspect core.h and report detected API generation")
    ap.add_argument("--apply", action="store_true", help="Copy extension files and patch Makefile/core.c/mac80211.c")
    ap.add_argument("--revert", action="store_true", help="Restore original files from .orig backups")
    args = ap.parse_args()

    rtw89_dir = Path(args.rtw89_dir).resolve()
    if args.revert:
        revert_tree(rtw89_dir)
        return

    feats = detect_tree_features(rtw89_dir)
    print("--- rtw89 Source Tree Detection ---")
    print(f"  Target directory     : {rtw89_dir}")
    print(f"  Host OS / Release    : {feats['host_os']} ({feats['host_release']})")
    print(
        f"  Detected API Gen     : Gen {feats['api_gen']} "
        f"({'Linux 6.11+ MLO rtw89_vif_link' if feats['api_gen'] == 2 else 'Linux 6.1-6.10 legacy rtw89_vif'})"
    )
    print(f"  pure_monitor_mode_vif: {'present' if feats['has_pure_mon_vif'] else 'absent (using internal ctx)'}")
    print(f"  Locking model        : {'wiphy_lock' if feats['uses_wiphy_lock'] else 'rtwdev->mutex'}")

    if args.detect_only or not args.apply:
        if not args.detect_only:
            print("\nRun with --apply to integrate monitor support into this rtw89 source tree.")
        return

    apply_tree(rtw89_dir)
    print("\n[SUCCESS] rtw89 monitor mode & diagnostic TX integration complete.")


if __name__ == "__main__":
    main()
