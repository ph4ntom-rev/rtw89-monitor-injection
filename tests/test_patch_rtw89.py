#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
"""
Automated unit tests for scripts/patch_rtw89.py and repository hygiene.
Run with:
  python3 -m unittest discover -s tests -v
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHER_PATH = REPO_ROOT / "scripts" / "patch_rtw89.py"

spec = importlib.util.spec_from_file_location("patch_rtw89", PATCHER_PATH)
patch_rtw89 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_rtw89)


GEN1_CORE_H = """\
/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
#ifndef __RTW89_CORE_H__
#define __RTW89_CORE_H__
struct rtw89_vif {
\tu8 mac_id;
\tu8 port;
};
#endif
"""

GEN1_MAKEFILE = """\
rtw89_core-y += core.o \\
\t\tmac80211.o
"""

GEN1_CORE_C = """\
// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
#include "core.h"
#include "txrx.h"

int rtw89_set_channel(struct rtw89_dev *rtwdev)
{
\t__rtw89_set_channel(rtwdev, NULL, RTW89_MAC_0, RTW89_PHY_0);
\treturn 0;
}

static void rtw89_core_tx_update_desc_info(struct rtw89_dev *rtwdev,
\t\t\t\t\t   struct rtw89_core_tx_request *tx_req)
{
\tswitch (tx_req->tx_type) {
\tdefault:
\t\tbreak;
\t}
}

int rtw89_core_tx_write(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif,
\t\t\tstruct ieee80211_sta *sta, struct sk_buff *skb,
\t\t\tint *txdesc_size)
{
\tstruct rtw89_core_tx_request tx_req = {};
\tint ret = 0;

\ttx_req.skb = skb;
\ttx_req.vif = vif;
\treturn ret;
}

int rtw89_core_register(struct rtw89_dev *rtwdev)
{
\tint ret;

\tret = ieee80211_register_hw(rtwdev->hw);
\treturn ret;
}
"""

GEN1_MAC80211_C = """\
// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
#include "core.h"
#include "mac.h"

static void rtw89_ops_tx(struct ieee80211_hw *hw,
\t\t\t struct ieee80211_tx_control *control,
\t\t\t struct sk_buff *skb)
{
\tstruct rtw89_dev *rtwdev = hw->priv;
\tstruct ieee80211_tx_info *info = IEEE80211_SKB_CB(skb);
\tstruct ieee80211_vif *vif = info->control.vif;
\tstruct rtw89_vif *rtwvif = vif_to_rtwvif(vif);

\t(void)rtwdev;
\t(void)rtwvif;
}

static int rtw89_ops_add_interface(struct ieee80211_hw *hw,
\t\t\t\t   struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;
\tint ret = 0;

\tmutex_lock(&rtwdev->mutex);
\tmutex_unlock(&rtwdev->mutex);
\treturn ret;
}

static void rtw89_ops_remove_interface(struct ieee80211_hw *hw,
\t\t\t\t       struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tmutex_lock(&rtwdev->mutex);
\tmutex_unlock(&rtwdev->mutex);
}

static void rtw89_ops_configure_filter(struct ieee80211_hw *hw,
\t\t\t\t       unsigned int changed_flags,
\t\t\t\t       unsigned int *total_flags,
\t\t\t\t       u64 multicast)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tmutex_lock(&rtwdev->mutex);
\tmutex_unlock(&rtwdev->mutex);
}
"""

GEN2_CORE_H = """\
/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
#ifndef __RTW89_CORE_H__
#define __RTW89_CORE_H__
struct rtw89_vif_link {
\tu8 mac_id;
\tu8 port;
};
struct rtw89_dev {
\tstruct rtw89_vif *pure_monitor_mode_vif;
};
#endif
"""

GEN2_CORE_C = """\
// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
#include "core.h"
#include "wow.h"

int rtw89_set_channel(struct rtw89_dev *rtwdev)
{
\t__rtw89_set_channel(rtwdev, NULL, RTW89_MAC_0, RTW89_PHY_0);
\tif (rtwdev->chip->chip_gen == RTW89_CHIP_AX)
\t\treturn 0;
\treturn 0;
}

static void rtw89_core_tx_update_desc_info(struct rtw89_dev *rtwdev,
\t\t\t\t\t   struct rtw89_core_tx_request *tx_req)
{
\tswitch (tx_req->tx_type) {
\tdefault:
\t\tbreak;
\t}
}

int rtw89_core_tx_write(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif,
\t\t\tstruct ieee80211_sta *sta, struct sk_buff *skb,
\t\t\tint *txdesc_size)
{
\tstruct rtw89_core_tx_request tx_req = {};
\tstruct rtw89_vif *rtwvif = vif_to_rtwvif(vif);
\tint ret = 0;

\t(void)rtwvif;
\ttx_req.skb = skb;
\treturn ret;
}

static int rtw89_core_register_hw(struct rtw89_dev *rtwdev)
{
\tint ret;

\tret = ieee80211_register_hw(rtwdev->hw);
\treturn ret;
}

int rtw89_core_register(struct rtw89_dev *rtwdev)
{
\treturn rtw89_core_register_hw(rtwdev);
}
"""

GEN2_MAC80211_C = """\
// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
#include "core.h"
#include "util.h"

static void rtw89_ops_tx(struct ieee80211_hw *hw,
\t\t\t struct ieee80211_tx_control *control,
\t\t\t struct sk_buff *skb)
{
\tstruct rtw89_dev *rtwdev = hw->priv;
\tstruct ieee80211_tx_info *info = IEEE80211_SKB_CB(skb);
\tstruct ieee80211_vif *vif = info->control.vif;
\tstruct rtw89_vif *rtwvif = vif_to_rtwvif(vif);

\t(void)rtwdev;
\t(void)rtwvif;
}

static int rtw89_ops_add_interface(struct ieee80211_hw *hw,
\t\t\t\t   struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
\treturn 0;
}

static void rtw89_ops_remove_interface(struct ieee80211_hw *hw,
\t\t\t\t       struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
}

static void rtw89_ops_configure_filter(struct ieee80211_hw *hw,
\t\t\t\t       unsigned int changed_flags,
\t\t\t\t       unsigned int *new_flags,
\t\t\t\t       u64 multicast)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
\trtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_0, 0);
\tif (!rtwdev->dbcc_en)
\t\treturn;
}
"""


class PatchRtw89TestCase(unittest.TestCase):
    def test_gen1_apply_idempotent_and_revert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "core.h").write_text(GEN1_CORE_H, encoding="utf-8", newline="\n")
            (tree / "Makefile").write_text(GEN1_MAKEFILE, encoding="utf-8", newline="\n")
            (tree / "core.c").write_text(GEN1_CORE_C, encoding="utf-8", newline="\n")
            (tree / "mac80211.c").write_text(GEN1_MAC80211_C, encoding="utf-8", newline="\n")

            feats = patch_rtw89.detect_tree_features(tree)
            self.assertEqual(feats["api_gen"], 1)
            self.assertFalse(feats["has_pure_mon_vif"])
            self.assertFalse(feats["uses_wiphy_lock"])

            patch_rtw89.apply_tree(tree)
            autoconf = (tree / "rtw89_mon_autoconf.h").read_text(encoding="utf-8")
            self.assertIn("#define RTW89_MON_API_GEN 1", autoconf)
            self.assertIn("#define RTW89_MON_HAS_PURE_VIF 0", autoconf)

            patched_core = (tree / "core.c").read_text(encoding="utf-8")
            patched_mac = (tree / "mac80211.c").read_text(encoding="utf-8")
            self.assertIn("rtw89_mon_register_hw_caps(rtwdev);", patched_core)
            self.assertIn("rtw89_mon_apply_rx_filter(rtwdev, total_flags);", patched_mac)

            # Re-applying must be idempotent
            patch_rtw89.apply_tree(tree)
            self.assertEqual(patched_core, (tree / "core.c").read_text(encoding="utf-8"))

            # Revert must restore exact originals
            patch_rtw89.revert_tree(tree)
            self.assertEqual((tree / "core.c").read_text(encoding="utf-8"), GEN1_CORE_C)
            self.assertEqual((tree / "mac80211.c").read_text(encoding="utf-8"), GEN1_MAC80211_C)
            self.assertFalse((tree / "rtw89_mon.c").exists())

    def test_gen2_apply_and_revert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "core.h").write_text(GEN2_CORE_H, encoding="utf-8", newline="\n")
            (tree / "Makefile").write_text(GEN1_MAKEFILE, encoding="utf-8", newline="\n")
            (tree / "core.c").write_text(GEN2_CORE_C, encoding="utf-8", newline="\n")
            (tree / "mac80211.c").write_text(GEN2_MAC80211_C, encoding="utf-8", newline="\n")

            feats = patch_rtw89.detect_tree_features(tree)
            self.assertEqual(feats["api_gen"], 2)
            self.assertTrue(feats["has_pure_mon_vif"])
            self.assertTrue(feats["uses_wiphy_lock"])

            patch_rtw89.apply_tree(tree)
            patched_core = (tree / "core.c").read_text(encoding="utf-8")
            patched_mac = (tree / "mac80211.c").read_text(encoding="utf-8")

            # Verify AX early-return ordering in rtw89_set_channel
            self.assertLess(
                patched_core.index("rtw89_mon_post_set_channel(rtwdev);"),
                patched_core.index("if (rtwdev->chip->chip_gen == RTW89_CHIP_AX)"),
            )
            # Verify NULL vif resolution precedes vif_to_rtwvif(vif) in both core.c & mac80211.c
            self.assertLess(
                patched_core.index("vif = rtw89_mon_resolve_tx_vif(rtwdev, vif);"),
                patched_core.index("rtwvif = vif_to_rtwvif(vif);"),
            )
            self.assertLess(
                patched_mac.index("vif = rtw89_mon_resolve_tx_vif(rtwdev, vif);"),
                patched_mac.index("rtwvif = vif_to_rtwvif(vif);"),
            )
            # Verify configure_filter hook runs before !rtwdev->dbcc_en early return
            self.assertLess(
                patched_mac.index("rtw89_mon_apply_rx_filter(rtwdev, new_flags);"),
                patched_mac.index("if (!rtwdev->dbcc_en)"),
            )

            patch_rtw89.revert_tree(tree)
            self.assertEqual((tree / "core.c").read_text(encoding="utf-8"), GEN2_CORE_C)
            self.assertEqual((tree / "mac80211.c").read_text(encoding="utf-8"), GEN2_MAC80211_C)

    def test_repository_contains_no_firmware_binaries(self):
        forbidden_suffixes = {".bin", ".fw", ".rom", ".img", ".i64", ".idb", ".ko"}
        for path in REPO_ROOT.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    path.suffix.lower(),
                    forbidden_suffixes,
                    f"Forbidden binary file present in repository: {path}",
                )


if __name__ == "__main__":
    unittest.main()
