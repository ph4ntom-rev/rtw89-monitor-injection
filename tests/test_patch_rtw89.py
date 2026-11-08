#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
"""
Automated unit tests for scripts/patch_rtw89.py, patches/v4/, and repository hygiene.
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

static enum rtw89_core_tx_type
rtw89_core_get_tx_type(struct rtw89_dev *rtwdev, struct sk_buff *skb)
{
\tstruct ieee80211_hdr *hdr = (void *)skb->data;
\t__le16 fc = hdr->frame_control;

\t/* comment with fake brace { inside */
\tif (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc))
\t\treturn RTW89_CORE_TX_TYPE_MGMT;

\treturn RTW89_CORE_TX_TYPE_DATA;
}

static void rtw89_core_tx_update_desc_info(struct rtw89_dev *rtwdev,
\t\t\t\t\t   struct rtw89_core_tx_request *tx_req)
{
\tseq = (le16_to_cpu(hdr->seq_ctrl) & IEEE80211_SCTL_SEQ) >> 4;
\tswitch (tx_req->tx_type) {
\tcase RTW89_CORE_TX_TYPE_DATA:
\t\trtw89_core_tx_update_data_info(rtwdev, tx_req);
\t\tpkt_type = rtw89_core_tx_btc_spec_pkt_notify(rtwdev, tx_req);
\t\trtw89_core_tx_update_ampdu_info(rtwdev, tx_req, pkt_type);
\t\trtw89_core_tx_update_llc_hdr(rtwdev, desc_info, skb);
\t\tbreak;
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

\trtw89_traffic_stats_accu(rtwdev, rtwvif, skb, true);
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

\tif (!rtwdev)
\t\treturn;
\t(void)rtwvif;
}

static int rtw89_ops_config(struct ieee80211_hw *hw, u32 changed)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tmutex_lock(&rtwdev->mutex);
\tmutex_unlock(&rtwdev->mutex);
\treturn 0;
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

void rtw89_chip_rfk_channel(struct rtw89_dev *rtwdev)
{
\tbool mon = !!rtwdev->pure_monitor_mode_vif;

\t(void)mon;
}

static enum rtw89_core_tx_type
rtw89_core_get_tx_type(struct rtw89_dev *rtwdev, struct sk_buff *skb)
{
\tstruct ieee80211_hdr *hdr = (void *)skb->data;
\t__le16 fc = hdr->frame_control;

\tif (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc))
\t\treturn RTW89_CORE_TX_TYPE_MGMT;

\treturn RTW89_CORE_TX_TYPE_DATA;
}

static void rtw89_core_tx_update_injection(struct rtw89_dev *rtwdev,
\t\t\t\t\t   struct rtw89_core_tx_request *tx_req,
\t\t\t\t\t   struct ieee80211_tx_info *info)
{
\tstruct rtw89_tx_desc_info *desc_info = &tx_req->desc_info;
\tu8 idx = info->control.rates[0].idx;

\tdesc_info->use_rate = true;
\tdesc_info->dis_data_fb = true;
\tdesc_info->data_rate = idx;
}

static void rtw89_core_tx_update_desc_info(struct rtw89_dev *rtwdev,
\t\t\t\t\t   struct rtw89_core_tx_request *tx_req)
{
\tseq = (le16_to_cpu(hdr->seq_ctrl) & IEEE80211_SCTL_SEQ) >> 4;
\tswitch (tx_req->tx_type) {
\tcase RTW89_CORE_TX_TYPE_DATA:
\t\trtw89_core_tx_update_data_info(rtwdev, tx_req);
\t\tpkt_type = rtw89_core_tx_btc_spec_pkt_notify(rtwdev, tx_req);
\t\trtw89_core_tx_update_he_qos_htc(rtwdev, tx_req, pkt_type);
\t\trtw89_core_tx_update_ampdu_info(rtwdev, tx_req, pkt_type);
\t\trtw89_core_tx_update_llc_hdr(rtwdev, desc_info, skb);
\t\tbreak;
\tdefault:
\t\tbreak;
\t}
}

static int rtw89_core_tx_write_link(struct rtw89_dev *rtwdev,
\t\t\t\t    struct sk_buff *skb)
{
\trtw89_traffic_stats_accu(rtwdev, rtwvif, skb, true, true);
\trtw89_wow_parse_akm(rtwdev, skb);
\treturn 0;
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

void rtw89_vif_type_mapping(struct ieee80211_vif *vif, bool assoc)
{
\tswitch (vif->type) {
\tcase NL80211_IFTYPE_MONITOR:
\t\tbreak;
\tdefault:
\t\trtwvif_link->net_type = RTW89_NET_TYPE_NO_LINK;
\t\tbreak;
\t}
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

\tif (!rtwdev)
\t\treturn;
\t(void)rtwvif;
}

static int rtw89_ops_config(struct ieee80211_hw *hw, u32 changed)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
\treturn 0;
}

static int rtw89_ops_add_interface(struct ieee80211_hw *hw,
\t\t\t\t   struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
\trtwdev->pure_monitor_mode_vif = vif->type == NL80211_IFTYPE_MONITOR ?
\t\t\t\t\trtwvif : NULL;
\treturn 0;
}

static void rtw89_ops_remove_interface(struct ieee80211_hw *hw,
\t\t\t\t       struct ieee80211_vif *vif)
{
\tstruct rtw89_dev *rtwdev = hw->priv;

\tlockdep_assert_wiphy(hw->wiphy);
\trtwdev->pure_monitor_mode_vif = NULL;
}

static void rtw89_ops_configure_filter(struct ieee80211_hw *hw,
\t\t\t\t       unsigned int changed_flags,
\t\t\t\t       unsigned int *new_flags,
\t\t\t\t       u64 multicast)
{
\tstruct rtw89_dev *rtwdev = hw->priv;
\tu32 rx_fltr = rtwdev->hal.rx_fltr;

\tlockdep_assert_wiphy(hw->wiphy);
\t*new_flags &= FIF_ALLMULTI | FIF_PROBE_REQ;
\trtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_0, rx_fltr);
\tif (!rtwdev->dbcc_en)
\t\treturn;
\trtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_1, rx_fltr);
}
"""


class PatchRtw89TestCase(unittest.TestCase):
    def test_brace_scanner_ignores_comments_and_strings(self):
        snippet = (
            'int foo(void)\n'
            '{\n'
            '\t/* { unmatched brace in block comment */\n'
            '\t// } unmatched brace in line comment\n'
            '\tconst char *s = "{ string } \\"{ escaped";\n'
            '\tchar c = \'{\';\n'
            '\treturn 0;\n'
            '}\n'
        )
        span = patch_rtw89.find_c_function_span(snippet, "foo")
        self.assertIsNotNone(span)
        self.assertEqual(snippet[span[2]], "}")
        self.assertEqual(span[2], len(snippet) - 2)

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
            self.assertIn("#define RTW89_MON_PURE_VIF_RCU 0", autoconf)
            self.assertIn("#define RTW89_MON_HAS_TX_INJECTION_FUNC 0", autoconf)

            patched_core = (tree / "core.c").read_text(encoding="utf-8")
            patched_mac = (tree / "mac80211.c").read_text(encoding="utf-8")
            self.assertIn("if (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc))", patched_core)
            self.assertIn("IEEE80211_TX_CTL_INJECTED) &&\n\t    !ieee80211_is_data(fc))", patched_core)
            self.assertNotIn("!ieee80211_is_data(fc) || ieee80211_is_any_nullfunc(fc)", patched_core)
            self.assertIn("rtw89_mon_register_hw_caps(rtwdev);", patched_core)
            self.assertIn("!ieee80211_is_ctl(hdr->frame_control)", patched_core)
            self.assertIn("if (!(info->flags & IEEE80211_TX_CTL_INJECTED))", patched_core)
            self.assertIn("if (!(IEEE80211_SKB_CB(skb)->flags & IEEE80211_TX_CTL_INJECTED))", patched_core)
            self.assertNotIn("rtw89_mon_resolve_tx_vif", patched_core)
            self.assertIn("rtw89_mon_apply_rx_filter(rtwdev, total_flags);", patched_mac)
            self.assertIn("rtw89_mon_config_channel(rtwdev, changed);", patched_mac)
            self.assertIn("if (!rtwdev)\n\t\tgoto out_rcu_unlock;", patched_mac)
            self.assertIn("out_rcu_unlock:\n\trcu_read_unlock();", patched_mac)

            # Re-applying must be idempotent
            patch_rtw89.apply_tree(tree)
            self.assertEqual(patched_core, (tree / "core.c").read_text(encoding="utf-8"))
            self.assertEqual(patched_mac, (tree / "mac80211.c").read_text(encoding="utf-8"))

            # Revert must restore exact originals
            patch_rtw89.revert_tree(tree)
            self.assertEqual((tree / "core.h").read_text(encoding="utf-8"), GEN1_CORE_H)
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
            self.assertTrue(feats["has_tx_injection_func"])

            patch_rtw89.apply_tree(tree)
            autoconf = (tree / "rtw89_mon_autoconf.h").read_text(encoding="utf-8")
            self.assertIn("#define RTW89_MON_PURE_VIF_RCU 1", autoconf)
            self.assertIn("#define RTW89_MON_HAS_TX_INJECTION_FUNC 1", autoconf)

            patched_core_h = (tree / "core.h").read_text(encoding="utf-8")
            patched_core = (tree / "core.c").read_text(encoding="utf-8")
            patched_mac = (tree / "mac80211.c").read_text(encoding="utf-8")

            self.assertIn("struct rtw89_vif __rcu *pure_monitor_mode_vif;", patched_core_h)
            self.assertIn("rcu_access_pointer(rtwdev->pure_monitor_mode_vif)", patched_core)
            self.assertIn("if (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc))", patched_core)
            self.assertIn("IEEE80211_TX_CTL_INJECTED) &&\n\t    !ieee80211_is_data(fc))", patched_core)
            self.assertNotIn("!ieee80211_is_data(fc) || ieee80211_is_any_nullfunc(fc)", patched_core)
            self.assertIn("s8 idx = info->control.rates[0].idx;", patched_core)
            self.assertIn("if (idx < 0)\n\t\treturn;", patched_core)
            self.assertIn("rtwvif_link->self_role = RTW89_SELF_ROLE_AP;", patched_core)
            self.assertIn("if (!(info->flags & IEEE80211_TX_CTL_INJECTED))", patched_core)
            self.assertNotIn("rtw89_mon_resolve_tx_vif", patched_core)
            self.assertNotIn("rtwdev->pure_monitor_mode_vif = NULL;", patched_mac)
            self.assertIn("rtw89_mon_add_vif_post(rtwdev, vif);", patched_mac)
            self.assertIn("rtw89_mon_remove_vif_pre(rtwdev, vif);", patched_mac)
            self.assertIn("if (!rtwdev)\n\t\tgoto out_rcu_unlock;", patched_mac)
            self.assertIn("rtw89_mon_config_channel(rtwdev, changed);", patched_mac)
            self.assertIn("FIF_CONTROL | FIF_PSPOLL;", patched_mac)
            self.assertIn("ieee80211_hdrlen(", patched_mac)

            # Verify NULL/monitor vif resolution precedes vif_to_rtwvif(vif) in mac80211.c
            self.assertLess(
                patched_mac.index("vif = rtw89_mon_resolve_tx_vif(rtwdev, vif);"),
                patched_mac.index("rtwvif = vif_to_rtwvif(vif);"),
            )
            # Verify rx_fltr preparation precedes both RTW89_MAC_0 and RTW89_MAC_1 writes
            self.assertLess(
                patched_mac.index("rtw89_mon_prepare_rx_filter(rtwdev, &rx_fltr, new_flags);"),
                patched_mac.index("rtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_0, rx_fltr);"),
            )

            patch_rtw89.revert_tree(tree)
            self.assertEqual((tree / "core.h").read_text(encoding="utf-8"), GEN2_CORE_H)
            self.assertEqual((tree / "core.c").read_text(encoding="utf-8"), GEN2_CORE_C)
            self.assertEqual((tree / "mac80211.c").read_text(encoding="utf-8"), GEN2_MAC80211_C)

    def test_v4_patch_series_metadata_and_hygiene(self):
        v4_dir = REPO_ROOT / "patches" / "v4"
        self.assertTrue(v4_dir.is_dir(), "patches/v4 directory missing")
        patches = sorted(v4_dir.glob("v4-000[0-4]-*.patch"))
        self.assertEqual(len(patches), 5, "Expected cover letter + 4 patches in patches/v4/")

        p1 = patches[1].read_text(encoding="utf-8")
        self.assertIn("B_AX_A_MC", p1)
        self.assertIn("B_AX_A_MC_LIST_CAM_MATCH", p1)
        self.assertIn("FIF_CONTROL | FIF_PSPOLL", p1)

        p2 = patches[2].read_text(encoding="utf-8")
        self.assertIn("rtw89_recalc_pure_monitor_vif", p2)
        self.assertIn("RCU_INIT_POINTER(rtwdev->pure_monitor_mode_vif, NULL)", p2)
        self.assertNotIn("rcu_assign_pointer(rtwdev->pure_monitor_mode_vif, NULL)", p2)

        p3 = patches[3].read_text(encoding="utf-8")
        self.assertIn("desc_info->hw_ssn_sel = 0;", p3)
        self.assertIn("desc_info->data_retry_lowest_rate", p3)
        self.assertIn("IEEE80211_TX_CTL_NO_CCK_RATE", p3)
        self.assertIn("ieee80211_hdrlen(hdr->frame_control)", p3)
        self.assertIn("info->control.vif = vif;", p3)
        self.assertNotIn("desc_info->en_wd_info = true;", p3)

        p4 = patches[4].read_text(encoding="utf-8")
        self.assertIn("!rtwdev->chip->support_chanctx_num", p4)

        for p in patches[1:]:
            content = p.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("From "), f"Missing mbox From line in {p.name}")
            self.assertIn("From: Enver Ikromov <owner@ph4ntom-rev.uz>", content)
            self.assertIn("Signed-off-by: Enver Ikromov <owner@ph4ntom-rev.uz>", content)
            self.assertNotIn("Fixes: d9204e99b696", content)
            self.assertNotIn("target_rx_fltr", content)
            diff_body = content.split("\ndiff --git ", 1)[1]
            self.assertNotIn("rtw89_fw_h2c_join_info", diff_body)

    def test_v5_patch_series_metadata_and_hygiene(self):
        v5_dir = REPO_ROOT / "patches" / "v5"
        self.assertTrue(v5_dir.is_dir(), "patches/v5 directory missing")
        patches = sorted(v5_dir.glob("v5-00[0-9][0-9]-*.patch"))
        self.assertEqual(len(patches), 11, "Expected cover letter + 10 patches in patches/v5/")

        cover = patches[0].read_text(encoding="utf-8")
        self.assertNotIn("*** SUBJECT HERE ***", cover)
        self.assertNotIn("*** BLURB HERE ***", cover)
        self.assertIn("base-commit: d48723711bae9b8cdc56719c448ba42fef28cbee", cover)

        def body(idx):
            return patches[idx].read_text(encoding="utf-8")

        # 1/10 and 2/10 are standalone fixes with Fixes: tags
        self.assertIn('Fixes: a0e97ae3f332 ("wifi: rtw89: add ieee80211::remain_on_channel ops")', body(1))
        self.assertIn("Cc: stable@vger.kernel.org", body(1))
        self.assertIn("Fixes: e96abe4fd7cd", body(2))
        self.assertIn("+\ts8 idx = info->control.rates[0].idx;", body(2))

        # 4/10 is a plain RCU conversion without the v4 recalc helper
        self.assertIn("rtw89_set_pure_monitor_vif", body(4))
        self.assertNotIn("rtw89_recalc_pure_monitor_vif", body(4))
        self.assertIn("No functional change intended.", body(4))

        # 7/10 only reroutes injected control frames
        p7 = body(7)
        self.assertIn("IEEE80211_TX_CTL_INJECTED) &&", p7)
        self.assertNotIn("+\tif (!ieee80211_is_data(fc) || ieee80211_is_any_nullfunc(fc))", p7)

        for p in patches[1:]:
            content = p.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("From "), f"Missing mbox From line in {p.name}")
            self.assertIn("From: Enver Ikromov <owner@ph4ntom-rev.uz>", content)
            self.assertIn("Signed-off-by: Enver Ikromov <owner@ph4ntom-rev.uz>", content)
            self.assertNotIn("Fixes: d9204e99b696", content)
            self.assertNotIn("\r\n", content, f"CRLF line endings in {p.name}")
            diff_body = content.split("\ndiff --git ", 1)[1]
            self.assertNotIn("rtw89_fw_h2c_join_info", diff_body)

    def test_repository_contains_no_forbidden_artifacts(self):
        forbidden_suffixes = {".bin", ".fw", ".rom", ".img", ".i64", ".idb", ".ko", ".pcap", ".pcapng"}
        forbidden_names = {"wireshark_test.png", "PROMOTION_KIT.md", "capture_to_pcap.py"}
        for path in REPO_ROOT.rglob("*"):
            if ".git" in path.parts:
                continue
            if path.is_file():
                self.assertNotIn(
                    path.suffix.lower(),
                    forbidden_suffixes,
                    f"Forbidden binary file present in repository: {path}",
                )
                self.assertNotIn(
                    path.name,
                    forbidden_names,
                    f"Forbidden legacy/draft file present in repository: {path}",
                )


if __name__ == "__main__":
    unittest.main()
