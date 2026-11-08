// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/*
 * Copyright (c) 2026 Enver Ikromov <owner@ph4ntom-rev.uz>
 *
 * Cross-kernel (Linux 6.1 - 7.x+) out-of-tree helper module implementing the
 * PATCH v5 rtw89 monitor-mode fixes for distribution trees (best effort):
 *
 * 1. Monitor RX Filter (PATCH v5 3/10):
 *    Retains FIF_CONTROL and FIF_PSPOLL in *new_flags and clears B_AX_A_MC,
 *    B_AX_A_UC_CAM_MATCH, B_AX_A_BC_CAM_MATCH, B_AX_A_MC_LIST_CAM_MATCH,
 *    B_AX_A_BCN_CHK_EN, and B_AX_UID_FILTER_MASK in the local rx_fltr copy
 *    prior to rtw89_mac_set_rx_fltr() (for both RTW89_MAC_0 and RTW89_MAC_1)
 *    without mutating the baseline rtwdev->hal.rx_fltr state.
 *
 * 2. RCU-Protected Monitor VIF Resolution (PATCH v5 4/10 & 6/10):
 *    Publishes the active standalone monitor VIF via RCU across interface
 *    addition and removal (including when a second non-monitor VIF is removed)
 *    and resolves NULL or monitor info->control.vif pointers safely under
 *    rcu_read_lock().
 *
 * 3. Firmware Role & Injection Descriptor Handling (PATCH v5 2/10, 5/10, 7/10-9/10):
 *    Ensures NL80211_IFTYPE_MONITOR uses RTW89_NET_TYPE_NO_LINK and
 *    RTW89_SELF_ROLE_AP, routes non-Data frames (including 10-16B Control
 *    frames) via RTW89_CORE_TX_TYPE_MGMT, handles negative rate indices for
 *    both Management/Control and Data frames, preserves software sequence
 *    numbers unless IEEE80211_TX_CTL_ASSIGN_SEQ is set, and bounds retries on
 *    IEEE80211_TX_CTL_NO_ACK.
 *
 * 4. Monitor Restart Channel Restoration (PATCH v5 10/10):
 *    Restores hw->conf.chandef on IEEE80211_CONF_CHANGE_MONITOR when
 *    RTW89_PHY_0 is inactive after rtw89_ops_stop() on non-chanctx chips.
 */

#include <linux/etherdevice.h>
#include <linux/ieee80211.h>
#include <linux/rcupdate.h>
#include <linux/spinlock.h>
#include <net/mac80211.h>

#include "chan.h"
#include "core.h"
#include "debug.h"
#include "fw.h"
#include "mac.h"
#include "ps.h"
#include "reg.h"
#include "txrx.h"
#include "rtw89_mon.h"

/*
 * Exact R_AX_RX_FLTR_OPT / R_BE_RX_FLTR_OPT bit positions from upstream
 * drivers/net/wireless/realtek/rtw89/reg.h, guarded with #ifndef for
 * older 6.x headers.
 */
#ifndef B_AX_A_MC
#define B_AX_A_MC			BIT(3)
#endif
#ifndef B_AX_A_UC_CAM_MATCH
#define B_AX_A_UC_CAM_MATCH		BIT(4)
#endif
#ifndef B_AX_A_BC_CAM_MATCH
#define B_AX_A_BC_CAM_MATCH		BIT(5)
#endif
#ifndef B_AX_A_MC_LIST_CAM_MATCH
#define B_AX_A_MC_LIST_CAM_MATCH	BIT(6)
#endif
#ifndef B_AX_A_BCN_CHK_EN
#define B_AX_A_BCN_CHK_EN		BIT(7)
#endif
#ifndef B_AX_A_PWR_MGNT
#define B_AX_A_PWR_MGNT			BIT(10)
#endif
#ifndef B_AX_A_CRC32_ERR
#define B_AX_A_CRC32_ERR		BIT(11)
#endif
#ifndef B_AX_UID_FILTER_MASK
#define B_AX_UID_FILTER_MASK		GENMASK(31, 24)
#endif

#define RTW89_MON_MAX_DEVS		8

struct rtw89_mon_ctx {
	struct rtw89_dev *rtwdev;
	struct ieee80211_vif __rcu *mon_vif;
};

static DEFINE_SPINLOCK(rtw89_mon_lock);
static struct rtw89_mon_ctx rtw89_mon_table[RTW89_MON_MAX_DEVS];

static struct rtw89_mon_ctx *rtw89_mon_get_ctx(struct rtw89_dev *rtwdev,
					       bool create)
{
	struct rtw89_mon_ctx *free_slot = NULL;
	int i;

	for (i = 0; i < RTW89_MON_MAX_DEVS; i++) {
		if (READ_ONCE(rtw89_mon_table[i].rtwdev) == rtwdev)
			return &rtw89_mon_table[i];
		if (!free_slot && !READ_ONCE(rtw89_mon_table[i].rtwdev))
			free_slot = &rtw89_mon_table[i];
	}

	if (create && free_slot) {
		RCU_INIT_POINTER(free_slot->mon_vif, NULL);
		smp_wmb();
		WRITE_ONCE(free_slot->rtwdev, rtwdev);
		return free_slot;
	}

	return NULL;
}

void rtw89_mon_register_hw_caps(struct rtw89_dev *rtwdev)
{
	struct ieee80211_hw *hw = rtwdev->hw;

	hw->wiphy->interface_modes |= BIT(NL80211_IFTYPE_MONITOR);
	ieee80211_hw_set(hw, WANT_MONITOR_VIF);
	ieee80211_hw_set(hw, SIGNAL_DBM);
}

static void rtw89_mon_sync_standalone_vif(struct rtw89_dev *rtwdev,
					  struct rtw89_vif *exclude_rtwvif)
{
	struct rtw89_vif *sole = NULL;
	struct rtw89_vif *iter;
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	bool need_sync = false;
	int count = 0;

	list_for_each_entry(iter, &rtwdev->rtwvifs_list, list) {
		if (iter == exclude_rtwvif)
			continue;
		sole = iter;
		count++;
	}

	if (count != 1 || rtwvif_to_vif(sole)->type != NL80211_IFTYPE_MONITOR)
		sole = NULL;

	if (!sole) {
#if RTW89_MON_HAS_PURE_VIF
#if RTW89_MON_PURE_VIF_RCU
		if (rcu_access_pointer(rtwdev->pure_monitor_mode_vif)) {
			RCU_INIT_POINTER(rtwdev->pure_monitor_mode_vif, NULL);
			need_sync = true;
		}
#else
		if (READ_ONCE(rtwdev->pure_monitor_mode_vif)) {
			WRITE_ONCE(rtwdev->pure_monitor_mode_vif, NULL);
			need_sync = true;
		}
#endif
#endif
		spin_lock_irqsave(&rtw89_mon_lock, flags);
		ctx = rtw89_mon_get_ctx(rtwdev, false);
		if (ctx) {
			if (rcu_access_pointer(ctx->mon_vif)) {
				RCU_INIT_POINTER(ctx->mon_vif, NULL);
				need_sync = true;
			}
			WRITE_ONCE(ctx->rtwdev, NULL);
		}
		spin_unlock_irqrestore(&rtw89_mon_lock, flags);

		if (need_sync)
			synchronize_rcu();
		return;
	}

	/*
	 * On Gen 1 kernels (6.1 - 6.10) where rtw89_vif_type_mapping() did not
	 * set self_role = RTW89_SELF_ROLE_AP prior to rtw89_mac_vif_init(),
	 * update the role and notify firmware via rtw89_fw_h2c_join_info().
	 * On Gen 2 kernels patched by patch_rtw89.py, rtw89_vif_type_mapping()
	 * already configured RTW89_SELF_ROLE_AP before rtw89_mac_vif_init().
	 */
#if RTW89_MON_API_GEN < 2
	if (sole->self_role != RTW89_SELF_ROLE_AP) {
		sole->net_type = RTW89_NET_TYPE_NO_LINK;
		sole->self_role = RTW89_SELF_ROLE_AP;
		rtw89_fw_h2c_join_info(rtwdev, sole, NULL, false);
	}
#endif

#if RTW89_MON_HAS_PURE_VIF
#if RTW89_MON_PURE_VIF_RCU
	rcu_assign_pointer(rtwdev->pure_monitor_mode_vif, sole);
#else
	WRITE_ONCE(rtwdev->pure_monitor_mode_vif, sole);
#endif
#endif

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, true);
	if (ctx)
		rcu_assign_pointer(ctx->mon_vif, rtwvif_to_vif(sole));
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);
}

int rtw89_mon_add_vif_post(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	(void)vif;
	rtw89_mon_sync_standalone_vif(rtwdev, NULL);
	return 0;
}

void rtw89_mon_remove_vif_pre(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	rtw89_mon_sync_standalone_vif(rtwdev, vif_to_rtwvif(vif));
}

void rtw89_mon_prepare_rx_filter(struct rtw89_dev *rtwdev, u32 *rx_fltr,
				 unsigned int *total_flags)
{
	(void)total_flags;

	if (!(rtwdev->hw->conf.flags & IEEE80211_CONF_MONITOR))
		return;

	*rx_fltr &= ~(B_AX_A_MC |
		      B_AX_A_UC_CAM_MATCH |
		      B_AX_A_BC_CAM_MATCH |
		      B_AX_A_MC_LIST_CAM_MATCH |
		      B_AX_A_BCN_CHK_EN |
		      B_AX_UID_FILTER_MASK);
}

void rtw89_mon_apply_rx_filter(struct rtw89_dev *rtwdev,
			       unsigned int *total_flags)
{
	u32 rx_fltr;

	if (!(rtwdev->hw->conf.flags & IEEE80211_CONF_MONITOR))
		return;

	rx_fltr = rtwdev->hal.rx_fltr;
	rtw89_mon_prepare_rx_filter(rtwdev, &rx_fltr, total_flags);

	if (rtwdev->scanning) {
		rx_fltr &= ~B_AX_A_BCN_CHK_EN;
		rx_fltr &= ~B_AX_A_BC;
		rx_fltr &= ~B_AX_A_A1_MATCH;
	}

	rtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_0, rx_fltr);
	if (rtwdev->dbcc_en)
		rtw89_mac_set_rx_fltr(rtwdev, RTW89_MAC_1, rx_fltr);
}

void rtw89_mon_config_channel(struct rtw89_dev *rtwdev, u32 changed)
{
	struct ieee80211_hw *hw = rtwdev->hw;

	if (!(changed & IEEE80211_CONF_CHANGE_MONITOR))
		return;

	if (!(hw->conf.flags & IEEE80211_CONF_MONITOR))
		return;

#if RTW89_MON_API_GEN >= 2
	if (rtwdev->chip->support_chanctx_num)
		return;
#endif

	if (rtw89_get_entity_state(rtwdev, RTW89_PHY_0))
		return;

	if (!cfg80211_chandef_valid(&hw->conf.chandef))
		return;

#if RTW89_MON_API_GEN >= 2
	rtw89_config_entity_chandef(rtwdev, RTW89_CHANCTX_0, &hw->conf.chandef);
#else
	rtw89_config_entity_chandef(rtwdev, RTW89_SUB_ENTITY_0, &hw->conf.chandef);
#endif
	rtw89_set_channel(rtwdev);
}

/*
 * Must be called under rcu_read_lock() (mac80211 TX fastpath already holds RCU,
 * and patch_rtw89.py wraps rtw89_ops_tx() in rcu_read_lock()).
 */
struct ieee80211_vif *rtw89_mon_resolve_tx_vif(struct rtw89_dev *rtwdev,
					       struct ieee80211_vif *vif)
{
	struct rtw89_mon_ctx *ctx;
	int i;

	if (vif && vif->type != NL80211_IFTYPE_MONITOR)
		return vif;

#if RTW89_MON_HAS_PURE_VIF
	{
		struct rtw89_vif *rtwvif;

#if RTW89_MON_PURE_VIF_RCU
		rtwvif = rcu_dereference(rtwdev->pure_monitor_mode_vif);
#else
		rtwvif = READ_ONCE(rtwdev->pure_monitor_mode_vif);
#endif
		if (rtwvif)
			return rtwvif_to_vif(rtwvif);
	}
#endif

	for (i = 0; i < RTW89_MON_MAX_DEVS; i++) {
		ctx = &rtw89_mon_table[i];
		if (READ_ONCE(ctx->rtwdev) == rtwdev)
			return rcu_dereference(ctx->mon_vif);
	}

	return NULL;
}

void rtw89_mon_fixup_tx_desc(struct rtw89_dev *rtwdev,
			     struct rtw89_core_tx_request *tx_req)
{
	struct rtw89_tx_desc_info *desc_info = &tx_req->desc_info;
	struct sk_buff *skb = tx_req->skb;
	struct ieee80211_tx_info *info = IEEE80211_SKB_CB(skb);
	struct ieee80211_hdr *hdr = (struct ieee80211_hdr *)skb->data;
	s8 idx = info->control.rates[0].idx;
	__le16 fc;

	if (unlikely(tx_req->tx_type == RTW89_CORE_TX_TYPE_FWCMD))
		return;

	if (!(info->flags & IEEE80211_TX_CTL_INJECTED))
		return;

	if (skb->len < sizeof(struct ieee80211_cts))
		return;

	fc = hdr->frame_control;

	desc_info->use_rate = true;
	desc_info->dis_data_fb = true;

	/*
	 * Route non-Data frames (including 10-16 byte Control frames) through
	 * the MGMT queue and clear any stale seq / LLC fields computed for them.
	 */
	if (!ieee80211_is_data(fc) || ieee80211_is_any_nullfunc(fc)) {
		if (ieee80211_is_ctl(fc) ||
		    skb->len < sizeof(struct ieee80211_hdr_3addr)) {
			desc_info->seq = 0;
			desc_info->hdr_llc_len = 0;
			desc_info->agg_en = false;
		}
		tx_req->tx_type = RTW89_CORE_TX_TYPE_MGMT;
		desc_info->qsel = desc_info->hiq ? RTW89_TX_QSEL_B0_HI :
						   RTW89_TX_QSEL_B0_MGMT;
#if RTW89_MON_API_GEN >= 2
		desc_info->ch_dma = rtw89_chip_get_ch_dma(rtwdev, desc_info->qsel);
#else
		desc_info->ch_dma = rtw89_core_get_ch_dma(rtwdev, desc_info->qsel);
#endif
	}

	if (!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ)) {
		desc_info->upd_wlan_hdr = false;
		desc_info->hw_ssn_sel = 0;
		desc_info->hw_seq_mode = 0;
	}

	if (info->flags & IEEE80211_TX_CTL_NO_ACK) {
		desc_info->tx_cnt_lmt_en = true;
		desc_info->tx_cnt_lmt = 1;
	}

	/*
	 * Unconditionally assign a valid hardware rate when idx < 0 across both
	 * RTW89_CORE_TX_TYPE_MGMT and RTW89_CORE_TX_TYPE_DATA, preventing an
	 * unpatched rtw89_core_tx_update_injection() (u8 idx == 0xff) from
	 * leaving a corrupted data_rate on Management or Control frames.
	 */
	if (idx < 0) {
		if (info->flags & IEEE80211_TX_CTL_NO_CCK_RATE)
			desc_info->data_rate = RTW89_HW_RATE_OFDM6;
		else if (tx_req->tx_type != RTW89_CORE_TX_TYPE_MGMT &&
			 desc_info->data_retry_lowest_rate)
			desc_info->data_rate = desc_info->data_retry_lowest_rate;
		else
			desc_info->data_rate = info->band == NL80211_BAND_2GHZ ?
					       RTW89_HW_RATE_CCK1 :
					       RTW89_HW_RATE_OFDM6;
		return;
	}

#if !RTW89_MON_HAS_TX_INJECTION_FUNC
	{
		enum mac80211_rate_control_flags flags =
			info->control.rates[0].flags;

		if (flags & IEEE80211_TX_RC_160_MHZ_WIDTH)
			desc_info->data_bw = 3;
		else if (flags & IEEE80211_TX_RC_80_MHZ_WIDTH)
			desc_info->data_bw = 2;
		else if (flags & IEEE80211_TX_RC_40_MHZ_WIDTH)
			desc_info->data_bw = 1;

		if (flags & IEEE80211_TX_RC_SHORT_GI)
			desc_info->gi_ltf = 1;

		if (flags & IEEE80211_TX_RC_VHT_MCS)
			desc_info->data_rate = RTW89_HW_RATE_VHT_NSS1_MCS0 +
					       (((idx >> 4) & 0x3) << 4) +
					       (idx & 0xf);
		else if (flags & IEEE80211_TX_RC_MCS)
			desc_info->data_rate = RTW89_HW_RATE_MCS0 + (idx & 0x1f);
		else
			desc_info->data_rate = (u8)idx +
					       (info->band == NL80211_BAND_2GHZ ?
						RTW89_HW_RATE_CCK1 :
						RTW89_HW_RATE_OFDM6);
	}
#endif
}
