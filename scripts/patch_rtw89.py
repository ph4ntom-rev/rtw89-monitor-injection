#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
"""
patch_rtw89.py

Automated cross-kernel source integrator for rtw89 monitor mode, standalone
raw frame injection, and monitor restart channel restoration (PATCH v5, best effort).

Supported kernel driver generations:
  - API Gen 1: Linux 6.1 - 6.10 (legacy struct rtw89_vif, rtwdev->mutex)
  - API Gen 2: Linux 6.11 - 7.x+ (MLO struct rtw89_vif_link, pure_monitor_mode_vif, wiphy_lock)

Note: For mainline Linux 7.2+ / wireless-next trees, prefer applying the
upstream patch series in patches/v5/ directly via `git am patches/v5/v5-00*-wifi-*.patch`.
"""

import argparse
import platform
import re
import shutil
from pathlib import Path

MARKER = "RTW89_MON_EXT"
SCRIPT_DIR = Path(__file__).resolve().parent
SRC_DIR = SCRIPT_DIR.parent / "src"

BACKUP_TARGETS = (
    "Makefile",
    "Kbuild",
    "core.h",
    "txrx.h",
    "core.c",
    "mac80211.c",
    "mac.c",
    "mac_be.c",
)


def detect_tree_features(rtw89_dir: Path) -> dict:
    core_h = rtw89_dir / "core.h"
    if not core_h.is_file():
        raise FileNotFoundError(f"core.h not found in {rtw89_dir}")

    text = core_h.read_text(encoding="utf-8", errors="replace")
    has_vif_link = bool(re.search(r"\bstruct\s+rtw89_vif_link\s*\{", text))
    has_pure_mon = bool(re.search(r"\bpure_monitor_mode_vif\b", text))
    pure_mon_rcu = bool(re.search(r"struct\s+rtw89_vif\s+__rcu\s*\*\s*pure_monitor_mode_vif\b", text))
    has_rtwdev_ops = bool(re.search(r"\bstruct\s+ieee80211_ops\s+\*\s*ops\s*;", text))
    has_wiphy_lock = False

    mac80211_c = rtw89_dir / "mac80211.c"
    if mac80211_c.is_file():
        m_text = mac80211_c.read_text(encoding="utf-8", errors="replace")
        has_wiphy_lock = "lockdep_assert_wiphy" in m_text or "wiphy_lock(" in m_text

    has_tx_inj = False
    core_c = rtw89_dir / "core.c"
    if core_c.is_file():
        c_text = core_c.read_text(encoding="utf-8", errors="replace")
        has_tx_inj = "rtw89_core_tx_update_injection" in c_text

    return {
        "api_gen": 2 if has_vif_link else 1,
        "has_vif_link": has_vif_link,
        "has_pure_mon_vif": has_pure_mon,
        "pure_mon_rcu": pure_mon_rcu,
        "has_rtwdev_ops": has_rtwdev_ops,
        "uses_wiphy_lock": has_wiphy_lock,
        "has_tx_injection_func": has_tx_inj,
        "host_os": platform.system(),
        "host_release": platform.release(),
    }


def scan_matching_brace(src: str, brace_open: int) -> int:
    """Find matching closing '}' while skipping C comments, string, and char literals."""
    n = len(src)
    depth = 0
    i = brace_open
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == "/" and nxt == "/":
            end = src.find("\n", i + 2)
            i = n if end == -1 else end + 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


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
    brace_close = scan_matching_brace(src, brace_open)
    if brace_close == -1:
        return None
    return (sig_start, brace_open, brace_close)


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


def patch_core_h(core_h_path: Path) -> tuple:
    txt = core_h_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in txt:
        has_rcu = bool(re.search(r"struct\s+rtw89_vif\s+__rcu\s*\*\s*pure_monitor_mode_vif\b", txt))
        has_txd_ext = "bool dis_rts;" in txt and "bool no_ack;" in txt
        return (has_rcu, has_txd_ext, False)

    changed = False
    patched_rcu = False
    patched_txd_ext = False

    m = re.search(r"^(\s*)struct\s+rtw89_vif\s+\*pure_monitor_mode_vif\s*;", txt, re.MULTILINE)
    if m:
        indent = m.group(1)
        repl = f"{indent}/* {MARKER} */\n{indent}struct rtw89_vif __rcu *pure_monitor_mode_vif;"
        txt = txt[:m.start()] + repl + txt[m.end():]
        changed = True
        patched_rcu = True

    txd_m = re.search(r"\bstruct\s+rtw89_tx_desc_info\s*\{", txt)
    if txd_m:
        brace_open = txt.find("{", txd_m.start())
        brace_close = scan_matching_brace(txt, brace_open)
        if brace_close != -1:
            body = txt[brace_open:brace_close]
            bmc_m = re.search(r"^(\s*)bool\s+is_bmc\s*;", body, re.MULTILINE)
            if bmc_m and "dis_rts" not in body and "no_ack" not in body:
                indent = bmc_m.group(1)
                add = (
                    f"\n{indent}/* {MARKER} */\n"
                    f"{indent}bool dis_rts;\n"
                    f"{indent}bool no_ack;"
                )
                body = body[:bmc_m.end()] + add + body[bmc_m.end():]
                txt = txt[:brace_open] + body + txt[brace_close:]
                changed = True
                patched_txd_ext = True

    if changed:
        core_h_path.write_text(txt, encoding="utf-8", newline="\n")
    return (patched_rcu, patched_txd_ext, changed)


def patch_txrx_h(txrx_h_path: Path) -> bool:
    if not txrx_h_path.is_file():
        return False
    txt = txrx_h_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in txt or "BE_TXD_BODY6_NO_ACK" in txt:
        return False
    anchor = "#define BE_TXD_BODY6_UPD_WLAN_HDR BIT(15)"
    if anchor not in txt:
        return False
    txt = txt.replace(
        anchor,
        f"/* {MARKER} */\n#define BE_TXD_BODY6_NO_ACK BIT(16)\n{anchor}",
        1,
    )
    txrx_h_path.write_text(txt, encoding="utf-8", newline="\n")
    return True


def patch_core_c(core_c_path: Path, pure_mon_rcu: bool, has_txd_ext: bool, has_be_noack: bool) -> tuple:
    src = core_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        span_inj = find_c_function_span(src, "rtw89_core_tx_update_injection")
        inj_safe = (span_inj is None) or ("s8 idx = info->control.rates[0].idx;" in src[span_inj[1]:span_inj[2]])
        return (["core.c already patched"], inj_safe)

    notes = []
    src = insert_include(src)

    # 0. RCU accessors for pure_monitor_mode_vif in core.c if present
    if pure_mon_rcu:
        if "bool mon = !!rtwdev->pure_monitor_mode_vif;" in src:
            src = src.replace(
                "bool mon = !!rtwdev->pure_monitor_mode_vif;",
                "bool mon = !!rcu_access_pointer(rtwdev->pure_monitor_mode_vif);",
                1,
            )
            notes.append("updated rtw89_chip_rfk_channel() to rcu_access_pointer()")
        span_rfk = find_c_function_span(src, "rtw89_chip_rfk_channel_for_pure_mon_vif")
        if span_rfk:
            body = src[span_rfk[1]:span_rfk[2]]
            old_line = "struct rtw89_vif *rtwvif = rtwdev->pure_monitor_mode_vif;"
            if old_line in body:
                body = body.replace(
                    old_line,
                    "struct rtw89_vif *rtwvif = rcu_dereference_protected("
                    "rtwdev->pure_monitor_mode_vif, "
                    "lockdep_is_held(&rtwdev->hw->wiphy->mtx));",
                    1,
                )
                src = src[:span_rfk[1]] + body + src[span_rfk[2]:]
                notes.append("updated rtw89_chip_rfk_channel_for_pure_mon_vif() RCU dereference")

    # 1. rtw89_core_get_tx_type -> route injected non-Data (e.g. 10-16B Control) frames via MGMT.
    span_tx_type = find_c_function_span(src, "rtw89_core_get_tx_type")
    if not span_tx_type:
        raise RuntimeError("Function rtw89_core_get_tx_type not found in core.c")
    body = src[span_tx_type[1]:span_tx_type[2]]
    old_cond = (
        "\tif (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc))\n"
        "\t\treturn RTW89_CORE_TX_TYPE_MGMT;\n"
    )
    if "IEEE80211_TX_CTL_INJECTED" not in body:
        if old_cond not in body:
            raise RuntimeError("Unexpected body in rtw89_core_get_tx_type(); cannot hook injected control frames")
        body = body.replace(
            old_cond,
            old_cond
            + "\n"
            "\tif (unlikely(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED) &&\n"
            "\t    !ieee80211_is_data(fc))\n"
            "\t\treturn RTW89_CORE_TX_TYPE_MGMT;\n",
            1,
        )
        src = src[:span_tx_type[1]] + body + src[span_tx_type[2]:]
        notes.append("updated rtw89_core_get_tx_type() for injected Control frames")

    # 2. rtw89_core_tx_update_injection -> fix unsigned u8 idx = -1 (0xff) corruption if present
    span_inj = find_c_function_span(src, "rtw89_core_tx_update_injection")
    tx_inj_safe = True
    if span_inj:
        body = src[span_inj[1]:span_inj[2]]
        if "u8 idx = info->control.rates[0].idx;" in body:
            body = body.replace(
                "u8 idx = info->control.rates[0].idx;",
                "s8 idx = info->control.rates[0].idx;",
                1,
            )
            if "\tdesc_info->dis_data_fb = true;\n" in body:
                body = body.replace(
                    "\tdesc_info->dis_data_fb = true;\n",
                    "\tdesc_info->dis_data_fb = true;\n\n\tif (idx < 0)\n\t\treturn;\n",
                    1,
                )
            src = src[:span_inj[1]] + body + src[span_inj[2]:]
            notes.append("guarded rtw89_core_tx_update_injection() against negative rate idx")
        tx_inj_safe = ("s8 idx = info->control.rates[0].idx;" in body) and ("idx < 0" in body)

    # 3. rtw89_core_register_hw or rtw89_core_register -> before ieee80211_register_hw(...)
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

    # 4. rtw89_core_tx_update_desc_info -> guard short frames on seq_ctrl, skip IP/HTC/AMPDU on injected Data, & append fixup
    span = find_c_function_span(src, "rtw89_core_tx_update_desc_info")
    if not span:
        raise RuntimeError("Function rtw89_core_tx_update_desc_info not found in core.c")
    body = src[span[1]:span[2]]
    seq_old = "\tseq = (le16_to_cpu(hdr->seq_ctrl) & IEEE80211_SCTL_SEQ) >> 4;"
    if seq_old in body:
        body = body.replace(
            seq_old,
            "\tseq = (!ieee80211_is_ctl(hdr->frame_control) &&\n"
            "\t       skb_headlen(skb) >= sizeof(struct ieee80211_hdr_3addr)) ?\n"
            "\t      (le16_to_cpu(hdr->seq_ctrl) & IEEE80211_SCTL_SEQ) >> 4 : 0;",
            1,
        )
    elif "!ieee80211_is_ctl(hdr->frame_control)" not in body:
        raise RuntimeError("Could not find hdr->seq_ctrl read in rtw89_core_tx_update_desc_info()")

    data_m = re.search(
        r"(\t\tpkt_type\s*=\s*rtw89_core_tx_btc_spec_pkt_notify\([^\n]+\);\n"
        r"(?:\t\trtw89_core_tx_update_he_qos_htc\([^\n]+\);\n)?"
        r"\t\trtw89_core_tx_update_ampdu_info\([^\n]+\);\n)",
        body,
    )
    if data_m:
        indented = "".join("\t" + ln for ln in data_m.group(1).splitlines(True))
        body = (
            body[:data_m.start()]
            + "\t\tif (!(info->flags & IEEE80211_TX_CTL_INJECTED)) {\n"
            + indented
            + "\t\t}\n"
            + body[data_m.end():]
        )
    body = body.rstrip() + f"\n\n\t/* {MARKER} */\n\trtw89_mon_fixup_tx_desc(rtwdev, tx_req);\n"
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_core_tx_update_desc_info()")

    # 5. Guard rtw89_core_txq_check_agg() against injected frames on station TXQs if present
    span_txq = find_c_function_span(src, "rtw89_core_txq_check_agg")
    if span_txq:
        body = src[span_txq[1]:span_txq[2]]
        forbid_m = re.search(
            r"(\tif\s*\(test_bit\(RTW89_TXQ_F_FORBID_BA,\s*&?rtwtxq->flags\)\)\n\t\treturn;\n)",
            body,
        )
        if forbid_m and "IEEE80211_TX_CTL_INJECTED" not in body:
            body = (
                body[:forbid_m.end()]
                + "\n\tif (unlikely(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED))\n"
                + "\t\treturn;\n"
                + body[forbid_m.end():]
            )
            src = src[:span_txq[1]] + body + src[span_txq[2]:]
            notes.append("guarded rtw89_core_txq_check_agg() on injected frames")

    # 6. Skip rtw89_traffic_stats_accu() (4 or 5 args) and rtw89_wow_parse_akm() on injected frames
    stats_m = re.search(
        r"(\trtw89_traffic_stats_accu\(rtwdev,\s*rtwvif,\s*skb,\s*true(?:,\s*true)?\);\n"
        r"(?:\trtw89_wow_parse_akm\(rtwdev,\s*skb\);\n)?)",
        src,
    )
    wow_only = "\trtw89_wow_parse_akm(rtwdev, skb);"
    if stats_m:
        indented = "".join("\t" + ln for ln in stats_m.group(1).splitlines(True))
        src = (
            src[:stats_m.start()]
            + "\tif (!(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED)) {\n"
            + indented
            + "\t}\n"
            + src[stats_m.end():]
        )
        notes.append("guarded rtw89_traffic_stats_accu() and rtw89_wow_parse_akm() on injected frames")
    elif wow_only in src:
        src = src.replace(
            wow_only,
            "\tif (!(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED))\n"
            "\t\trtw89_wow_parse_akm(rtwdev, skb);",
            1,
        )
        notes.append("guarded rtw89_wow_parse_akm() on injected frames")

    # 7. rtw89_vif_type_mapping -> map NL80211_IFTYPE_MONITOR to RTW89_SELF_ROLE_AP
    span_map = find_c_function_span(src, "rtw89_vif_type_mapping")
    if span_map:
        body = src[span_map[1]:span_map[2]]
        target_var = "rtwvif_link" if "rtwvif_link->" in body else "rtwvif"
        mon_case = "\tcase NL80211_IFTYPE_MONITOR:\n\t\tbreak;"
        if mon_case in body:
            body = body.replace(
                mon_case,
                f"\tcase NL80211_IFTYPE_MONITOR:\n"
                f"\t\t{target_var}->net_type = RTW89_NET_TYPE_NO_LINK;\n"
                f"\t\t{target_var}->self_role = RTW89_SELF_ROLE_AP;\n"
                f"\t\tbreak;",
                1,
            )
            src = src[:span_map[1]] + body + src[span_map[2]:]
            notes.append("mapped NL80211_IFTYPE_MONITOR to RTW89_SELF_ROLE_AP in rtw89_vif_type_mapping()")

    # 8. Gate RTS_EN and BE_TXD_BODY6_NO_ACK in TXWD builders when core.h has dis_rts/no_ack
    if has_txd_ext:
        rts_old = "\tbool rts_en = !desc_info->is_bmc;"
        if rts_old in src:
            src = src.replace(
                rts_old,
                "\tbool rts_en = !desc_info->is_bmc && !desc_info->dis_rts;",
            )
            notes.append("gated rts_en on !desc_info->dis_rts in TXWD info4 builders")
        if has_be_noack:
            span_b6 = find_c_function_span(src, "rtw89_build_txwd_body6_v2")
            if span_b6:
                body = src[span_b6[1]:span_b6[2]]
                upd_fp = "FIELD_PREP(BE_TXD_BODY6_UPD_WLAN_HDR, desc_info->upd_wlan_hdr);"
                upd_u32 = "u32_encode_bits(desc_info->upd_wlan_hdr, BE_TXD_BODY6_UPD_WLAN_HDR) |"
                if upd_fp in body and "BE_TXD_BODY6_NO_ACK" not in body:
                    body = body.replace(
                        upd_fp,
                        "FIELD_PREP(BE_TXD_BODY6_NO_ACK, desc_info->no_ack) |\n"
                        "\t\t    " + upd_fp,
                        1,
                    )
                    src = src[:span_b6[1]] + body + src[span_b6[2]:]
                    notes.append("encoded BE_TXD_BODY6_NO_ACK in rtw89_build_txwd_body6_v2()")
                elif upd_u32 in body and "BE_TXD_BODY6_NO_ACK" not in body:
                    body = body.replace(
                        upd_u32,
                        upd_u32 + "\n\t\t u32_encode_bits(desc_info->no_ack, BE_TXD_BODY6_NO_ACK) |",
                        1,
                    )
                    src = src[:span_b6[1]] + body + src[span_b6[2]:]
                    notes.append("encoded BE_TXD_BODY6_NO_ACK in rtw89_build_txwd_body6_v2()")

    core_c_path.write_text(src, encoding="utf-8", newline="\n")
    return (notes, tx_inj_safe)


def patch_mac80211_c(mac_c_path: Path, pure_mon_rcu: bool) -> list:
    src = mac_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        return ["mac80211.c already patched"]

    notes = []
    src = insert_include(src)

    # 1. rtw89_ops_tx -> resolve NULL or monitor info->control.vif under rcu_read_lock()
    span = find_c_function_span(src, "rtw89_ops_tx")
    if not span:
        raise RuntimeError("Function rtw89_ops_tx not found in mac80211.c")
    body = src[span[1]:span[2]]
    if "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);" not in body:
        raise RuntimeError("Unexpected rtwvif declaration in rtw89_ops_tx()")
    body = body.replace(
        "struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);",
        "struct rtw89_vif *rtwvif;",
        1,
    )
    m = re.search(r"\n\n", body)
    if not m:
        raise RuntimeError("Could not find declaration boundary in rtw89_ops_tx()")
    rest = body[m.start():]
    rest = re.sub(
        r"^(\s*)return\s*;",
        r"\1goto out_rcu_unlock;",
        rest,
        flags=re.MULTILINE,
    )
    rest = rest.rstrip() + "\n\nout_rcu_unlock:\n\trcu_read_unlock();\n"
    hook = (
        f"\n\n\t/* {MARKER}: guard monitor TX vif under RCU */\n"
        f"\trcu_read_lock();\n"
        f"\tif (!vif || vif->type == NL80211_IFTYPE_MONITOR) {{\n"
        f"\t\tvif = rtw89_mon_resolve_tx_vif(rtwdev, vif);\n"
        f"\t\tif (!vif || skb_headlen(skb) < sizeof(struct ieee80211_cts) ||\n"
        f"\t\t    skb_headlen(skb) < ieee80211_hdrlen(((struct ieee80211_hdr *)skb->data)->frame_control)) {{\n"
        f"\t\t\tieee80211_free_txskb(hw, skb);\n"
        f"\t\t\tgoto out_rcu_unlock;\n"
        f"\t\t}}\n"
        f"\t\tIEEE80211_SKB_CB(skb)->control.vif = vif;\n"
        f"\t}} else if (unlikely(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED)) {{\n"
        f"\t\tif (skb_headlen(skb) < sizeof(struct ieee80211_cts) ||\n"
        f"\t\t    skb_headlen(skb) < ieee80211_hdrlen(((struct ieee80211_hdr *)skb->data)->frame_control)) {{\n"
        f"\t\t\tieee80211_free_txskb(hw, skb);\n"
        f"\t\t\tgoto out_rcu_unlock;\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"\trtwvif = vif_to_rtwvif(vif);"
    )
    body = body[:m.start()] + hook + rest
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_ops_tx() with RCU protection and skb_headlen() guards")

    # 2. rtw89_ops_config -> restore channel on monitor mode restart for non-chanctx chips
    span_cfg = find_c_function_span(src, "rtw89_ops_config")
    if not span_cfg:
        raise RuntimeError("Function rtw89_ops_config not found in mac80211.c")
    body = src[span_cfg[1]:span_cfg[2]]
    unlock_cfg = list(re.finditer(r"^(\s*)mutex_unlock\s*\(&rtwdev->mutex\)\s*;", body, re.MULTILINE))
    ret0_cfg = list(re.finditer(r"^(\s*)return\s+0\s*;", body, re.MULTILINE))
    if unlock_cfg:
        u = unlock_cfg[-1]
        indent = u.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_config_channel(rtwdev, changed);\n"
        body = body[:u.start()] + hook + body[u.start():]
    elif ret0_cfg:
        r0 = ret0_cfg[-1]
        indent = r0.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_config_channel(rtwdev, changed);\n\n"
        body = body[:r0.start()] + hook + body[r0.start():]
    else:
        raise RuntimeError("Could not find insertion point in rtw89_ops_config()")
    src = src[:span_cfg[1]] + body + src[span_cfg[2]:]
    notes.append("hooked rtw89_ops_config()")

    # 3. rtw89_ops_add_interface -> post-add hook (handles RCU pure_monitor_mode_vif + ctx)
    span = find_c_function_span(src, "rtw89_ops_add_interface")
    if not span:
        raise RuntimeError("Function rtw89_ops_add_interface not found in mac80211.c")
    body = src[span[1]:span[2]]
    if pure_mon_rcu:
        body = re.sub(
            r"\trtwdev->pure_monitor_mode_vif\s*=\s*vif->type\s*==\s*NL80211_IFTYPE_MONITOR\s*\?\s*\n\s*rtwvif\s*:\s*NULL\s*;\n",
            "",
            body,
            count=1,
        )
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

    # 4. rtw89_ops_remove_interface -> pre-remove hook (handles RCU pure_monitor_mode_vif + ctx)
    span = find_c_function_span(src, "rtw89_ops_remove_interface")
    if not span:
        raise RuntimeError("Function rtw89_ops_remove_interface not found in mac80211.c")
    body = src[span[1]:span[2]]
    body = body.replace("\n\trtwdev->pure_monitor_mode_vif = NULL;\n", "\n", 1)
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
        body = (
            body[:m.start()]
            + f"\n\n\t/* {MARKER} */\n\trtw89_mon_remove_vif_pre(rtwdev, vif);"
            + body[m.start():]
        )
    src = src[:span[1]] + body + src[span[2]:]
    notes.append("hooked rtw89_ops_remove_interface()")

    # 5. rtw89_ops_configure_filter -> retain FIF_CONTROL|FIF_PSPOLL and mask rx_fltr before MAC_0/MAC_1 write
    span = find_c_function_span(src, "rtw89_ops_configure_filter")
    if not span:
        raise RuntimeError("Function rtw89_ops_configure_filter not found in mac80211.c")
    sig = src[span[0]:span[1]]
    arg_m = re.search(r"unsigned\s+int\s*\*\s*(\w+)", sig)
    flags_arg = arg_m.group(1) if arg_m else "new_flags"

    body = src[span[1]:span[2]]
    if "FIF_PROBE_REQ;" in body and "FIF_CONTROL" not in body:
        body = body.replace(
            "FIF_PROBE_REQ;",
            "FIF_PROBE_REQ |\n\t\t      FIF_CONTROL | FIF_PSPOLL;",
            1,
        )
    mac0_fltr = re.search(
        r"^(\s*)rtw89_mac_set_rx_fltr\s*\(\s*rtwdev\s*,\s*RTW89_MAC_0\s*,\s*rx_fltr\s*\)\s*;",
        body,
        re.MULTILINE,
    )
    unlock_m = list(re.finditer(r"^(\s*)mutex_unlock\s*\(&rtwdev->mutex\)\s*;", body, re.MULTILINE))
    if mac0_fltr:
        indent = mac0_fltr.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_prepare_rx_filter(rtwdev, &rx_fltr, {flags_arg});\n"
        body = body[:mac0_fltr.start()] + hook + body[mac0_fltr.start():]
    elif unlock_m:
        u = unlock_m[-1]
        indent = u.group(1)
        hook = f"{indent}/* {MARKER} */\n{indent}rtw89_mon_apply_rx_filter(rtwdev, {flags_arg});\n"
        body = body[:u.start()] + hook + body[u.start():]
    else:
        body = body.rstrip() + f"\n\n\t/* {MARKER} */\n\trtw89_mon_apply_rx_filter(rtwdev, {flags_arg});\n"
    src = src[:span[1]] + body + src[span[2]:]
    notes.append(f"hooked rtw89_ops_configure_filter({flags_arg})")

    mac_c_path.write_text(src, encoding="utf-8", newline="\n")
    return notes


def patch_mac_c(mac_c_path: Path) -> list:
    if not mac_c_path.is_file():
        return []
    src = mac_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        return ["mac.c already patched"]

    notes = []
    changed = False

    span_set = find_c_function_span(src, "rtw89_mac_set_rx_fltr")
    if span_set:
        body = src[span_set[1]:span_set[2]]
        m = re.search(r"\n\n", body)
        if m and "rtw89_mon_refine_rx_fltr" not in body:
            hook = f"\n\n\t/* {MARKER} */\n\trx_fltr = rtw89_mon_refine_rx_fltr(rx_fltr);"
            body = body[:m.start()] + hook + body[m.start():]
            src = src[:span_set[1]] + body + src[span_set[2]:]
            changed = True
            notes.append("refined rx_fltr in rtw89_mac_set_rx_fltr()")

    span_ax = find_c_function_span(src, "rx_fltr_init_ax")
    if span_ax:
        body = src[span_ax[1]:span_ax[2]]
        old_line = "\tmac_ftlr = rtwdev->hal.rx_fltr;"
        if old_line in body:
            body = body.replace(
                old_line,
                f"\t/* {MARKER} */\n\tmac_ftlr = rtw89_mon_refine_rx_fltr(rtwdev->hal.rx_fltr);",
                1,
            )
            src = src[:span_ax[1]] + body + src[span_ax[2]:]
            changed = True
            notes.append("refined rx_fltr in rx_fltr_init_ax()")

    if changed:
        src = insert_include(src)
        mac_c_path.write_text(src, encoding="utf-8", newline="\n")
    return notes


def patch_mac_be_c(mac_be_c_path: Path) -> list:
    if not mac_be_c_path.is_file():
        return []
    src = mac_be_c_path.read_text(encoding="utf-8", errors="replace")
    if MARKER in src:
        return ["mac_be.c already patched"]

    notes = []
    span_be = find_c_function_span(src, "rx_fltr_init_be")
    if span_be:
        body = src[span_be[1]:span_be[2]]
        old_line = "\trtw89_write32(rtwdev, reg, rtwdev->hal.rx_fltr);"
        if old_line in body:
            body = body.replace(
                old_line,
                f"\t/* {MARKER} */\n\trtw89_write32(rtwdev, reg, rtw89_mon_refine_rx_fltr(rtwdev->hal.rx_fltr));",
                1,
            )
            src = src[:span_be[1]] + body + src[span_be[2]:]
            src = insert_include(src)
            mac_be_c_path.write_text(src, encoding="utf-8", newline="\n")
            notes.append("refined rx_fltr in rx_fltr_init_be()")
    return notes


def backup_file(p: Path):
    bak = p.with_suffix(p.suffix + ".orig")
    if not bak.exists() and p.exists():
        shutil.copy2(p, bak)


def revert_tree(rtw89_dir: Path):
    for name in BACKUP_TARGETS:
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
    for req in ("core.h", "core.c", "mac80211.c"):
        if not (rtw89_dir / req).is_file():
            raise FileNotFoundError(
                f"Missing {req} in {rtw89_dir}. Point --rtw89-dir to full rtw89 kernel driver sources."
            )

    makefile = rtw89_dir / "Makefile"
    if not makefile.is_file():
        makefile = rtw89_dir / "Kbuild"
    if not makefile.is_file():
        raise FileNotFoundError(f"Neither Makefile nor Kbuild found in {rtw89_dir}")

    for name in BACKUP_TARGETS:
        target = rtw89_dir / name
        if target.is_file():
            backup_file(target)

    patched_pure_rcu, patched_txd_ext, changed_core_h = patch_core_h(rtw89_dir / "core.h")
    patched_txrx_h = patch_txrx_h(rtw89_dir / "txrx.h")
    has_be_noack = patched_txrx_h or (
        (rtw89_dir / "txrx.h").is_file()
        and "BE_TXD_BODY6_NO_ACK" in (rtw89_dir / "txrx.h").read_text(encoding="utf-8", errors="replace")
    )
    pure_mon_rcu = feats["pure_mon_rcu"] or patched_pure_rcu

    core_notes, tx_inj_safe = patch_core_c(
        rtw89_dir / "core.c", pure_mon_rcu, patched_txd_ext, has_be_noack
    )
    mac80211_notes = patch_mac80211_c(rtw89_dir / "mac80211.c", pure_mon_rcu)
    mac_notes = patch_mac_c(rtw89_dir / "mac.c")
    mac_be_notes = patch_mac_be_c(rtw89_dir / "mac_be.c")

    autoconf = (
        "/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */\n"
        "/* Auto-generated by scripts/patch_rtw89.py */\n"
        "#ifndef __RTW89_MON_AUTOCONF_H__\n"
        "#define __RTW89_MON_AUTOCONF_H__\n\n"
        f"#define RTW89_MON_API_GEN {feats['api_gen']}\n"
        f"#define RTW89_MON_HAS_PURE_VIF {1 if feats['has_pure_mon_vif'] else 0}\n"
        f"#define RTW89_MON_PURE_VIF_RCU {1 if pure_mon_rcu else 0}\n"
        f"#define RTW89_MON_HAS_TX_INJECTION_FUNC {1 if feats['has_tx_injection_func'] else 0}\n"
        f"#define RTW89_MON_TX_INJ_SAFE {1 if tx_inj_safe else 0}\n"
        f"#define RTW89_MON_HAS_RTWDEV_OPS {1 if feats['has_rtwdev_ops'] else 0}\n"
        f"#define RTW89_MON_HAS_TXD_EXT {1 if patched_txd_ext else 0}\n\n"
        "#endif /* __RTW89_MON_AUTOCONF_H__ */\n"
    )
    (rtw89_dir / "rtw89_mon_autoconf.h").write_text(autoconf, encoding="utf-8", newline="\n")

    shutil.copy2(SRC_DIR / "rtw89_mon.h", rtw89_dir / "rtw89_mon.h")
    shutil.copy2(SRC_DIR / "rtw89_mon.c", rtw89_dir / "rtw89_mon.c")
    print("[APPLY] Copied rtw89_mon.h, rtw89_mon.c, rtw89_mon_autoconf.h")

    if changed_core_h:
        print("[APPLY] core.h: updated RCU / TX descriptor annotations")
    if patched_txrx_h:
        print("[APPLY] txrx.h: added BE_TXD_BODY6_NO_ACK")
    if patch_makefile(makefile):
        print(f"[APPLY] Patched {makefile.name} (+= rtw89_mon.o)")
    for n in core_notes:
        print(f"[APPLY] core.c: {n}")
    for n in mac80211_notes:
        print(f"[APPLY] mac80211.c: {n}")
    for n in mac_notes:
        print(f"[APPLY] mac.c: {n}")
    for n in mac_be_notes:
        print(f"[APPLY] mac_be.c: {n}")

    return feats


def main():
    ap = argparse.ArgumentParser(
        description="Integrate or revert rtw89 NL80211_IFTYPE_MONITOR & raw frame injection support"
    )
    ap.add_argument("--rtw89-dir", required=True, help="Path to rtw89 driver source directory")
    ap.add_argument("--detect-only", action="store_true", help="Inspect core.h and report detected API generation")
    ap.add_argument("--apply", action="store_true", help="Copy extension files and patch rtw89 driver sources")
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
    print(f"  rtwdev->ops pointer  : {'present' if feats['has_rtwdev_ops'] else 'absent'}")
    print(f"  Locking model        : {'wiphy_lock' if feats['uses_wiphy_lock'] else 'rtwdev->mutex'}")

    if args.detect_only or not args.apply:
        if not args.detect_only:
            print("\nRun with --apply to integrate monitor support into this rtw89 source tree.")
        return

    apply_tree(rtw89_dir)
    print("\n[SUCCESS] rtw89 monitor mode & raw frame injection integration complete.")


if __name__ == "__main__":
    main()
