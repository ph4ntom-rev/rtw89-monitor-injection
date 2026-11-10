// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/*
 * Copyright (c) 2026 Enver Ikromov <owner@ph4ntom-rev.uz>
 *
 * Cross-kernel (Linux 6.1 - 7.x+) out-of-tree helper module implementing the
 * PATCH v5 rtw89 monitor-mode fixes for distribution trees (best effort):
 *
 * 1. Monitor RX Filter (PATCH v5 3/10):
 *    Retains FIF_CONTROL and FIF_PSPOLL in *new_flags and clears
 *    B_AX_A_A1_MATCH, B_AX_A_BC, B_AX_A_MC, B_AX_A_UC_CAM_MATCH,
 *    B_AX_A_BC_CAM_MATCH, B_AX_A_MC_LIST_CAM_MATCH, B_AX_A_BCN_CHK_EN, and
 *    B_AX_UID_FILTER_MASK whenever B_AX_SNIFFER_MODE is set across MAC_0/MAC_1
 *    and scan/ROC/SER restores without mutating baseline rtwdev->hal.rx_fltr.
 *
 * 2. RCU-Protected Monitor VIF Resolution (PATCH v5 4/10 & 6/10):
 *    Publishes the active standalone monitor VIF via RCU across interface
 *    addition and removal (including when a second non-monitor VIF is removed)
 *    using dynamically allocated RCU-freed per-device nodes (avoiding slot
 *    reuse ABA races) and resolves NULL or monitor info->control.vif pointers
 *    safely under rcu_read_lock().
 *
 * 3. Firmware Role & Injection Descriptor Handling (PATCH v5 2/10, 5/10, 7/10-9/10):
 *    Ensures NL80211_IFTYPE_MONITOR uses RTW89_NET_TYPE_NO_LINK and
 *    RTW89_SELF_ROLE_AP, routes non-Data frames (including 10-16B Control
 *    frames) via RTW89_CORE_TX_TYPE_MGMT, handles negative and out-of-range
 *    rate indices across AX and Wi-Fi 7 BE chips, disables hardware sequence
 *    generation unless IEEE80211_TX_CTL_ASSIGN_SEQ is set on a non-control
 *    frame, gates RTS/HW_RTS on IEEE80211_TX_RC_USE_RTS_CTS, and bounds
 *    retries on IEEE80211_TX_CTL_NO_ACK.
 *
 * 4. Monitor Restart Channel Restoration (PATCH v5 10/10):
 *    Restores hw->conf.chandef on IEEE80211_CONF_CHANGE_MONITOR when
 *    RTW89_PHY_0 is inactive after rtw89_ops_stop() and chanctx ops are not
 *    registered (!rtwdev->ops->assign_vif_chanctx).
 */

#include <linux/etherdevice.h>
#include <linux/ieee80211.h>
#include <linux/list.h>
#include <linux/lockdep.h>
#include <linux/minmax.h>
#include <linux/rculist.h>
#include <linux/rcupdate.h>
#include <linux/slab.h>
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
#ifndef B_AX_SNIFFER_MODE
#define B_AX_SNIFFER_MODE		BIT(0)
#endif
#ifndef B_AX_A_A1_MATCH
#define B_AX_A_A1_MATCH		BIT(1)
#endif
#ifndef B_AX_A_BC
#define B_AX_A_BC			BIT(2)
#endif
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
#ifndef B_AX_RX_MPDU_MAX_LEN_MASK
#define B_AX_RX_MPDU_MAX_LEN_MASK	GENMASK(21, 16)
#endif
#ifndef B_AX_RX_FLTR_CFG_MASK
#define B_AX_RX_FLTR_CFG_MASK		((u32)~B_AX_RX_MPDU_MAX_LEN_MASK)
#endif
#ifndef R_AX_RX_FLTR_OPT
#define R_AX_RX_FLTR_OPT		0xCE20
#endif

#define RTW89_MON_SNIFFER_CLRMASK	(B_AX_A_A1_MATCH | \
					 B_AX_A_BC | \
					 B_AX_A_MC | \
					 B_AX_A_UC_CAM_MATCH | \
					 B_AX_A_BC_CAM_MATCH | \
					 B_AX_A_MC_LIST_CAM_MATCH | \
					 B_AX_A_BCN_CHK_EN | \
					 B_AX_UID_FILTER_MASK)

#if RTW89_MON_USES_WIPHY_LOCK
#define rtw89_mon_lockdep_is_held(rtwdev) \
	lockdep_is_held(&(rtwdev)->hw->wiphy->mtx)
#else
#define rtw89_mon_lockdep_is_held(rtwdev) \
	lockdep_is_held(&(rtwdev)->mutex)
#endif

struct rtw89_mon_ctx {
	struct list_head list;
	struct rcu_head rcu;
	struct rtw89_dev *rtwdev;
	struct ieee80211_vif __rcu *mon_vif;
};

static DEFINE_SPINLOCK(rtw89_mon_lock);
static LIST_HEAD(rtw89_mon_list);

static struct rtw89_mon_ctx *
rtw89_mon_find_ctx_locked(struct rtw89_dev *rtwdev)
{
	struct rtw89_mon_ctx *ctx;

	list_for_each_entry(ctx, &rtw89_mon_list, list) {
		if (ctx->rtwdev == rtwdev)
			return ctx;
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
		ctx = rtw89_mon_find_ctx_locked(rtwdev);
		if (ctx) {
			if (rcu_access_pointer(ctx->mon_vif)) {
				RCU_INIT_POINTER(ctx->mon_vif, NULL);
				need_sync = true;
			}
			if (count == 0) {
				list_del_rcu(&ctx->list);
				kfree_rcu(ctx, rcu);
				need_sync = true;
			}
		}
		spin_unlock_irqrestore(&rtw89_mon_lock, flags);

		if (need_sync)
			synchronize_rcu();
		return;
	}

	/*
	 * On Gen 1 kernels (Linux 6.1 - 6.12) where rtw89_vif_type_mapping()
	 * may not have set self_role = RTW89_SELF_ROLE_AP prior to
	 * rtw89_mac_vif_init(), update the role and notify firmware via
	 * rtw89_fw_h2c_join_info(). On Gen 2 kernels (Linux 6.13+) patched by
	 * patch_rtw89.py, rtw89_vif_type_mapping() already configured
	 * RTW89_SELF_ROLE_AP before rtw89_mac_vif_init().
	 */
#if RTW89_MON_API_GEN < 2
	if (sole->self_role != RTW89_SELF_ROLE_AP) {
		sole->net_type = RTW89_NET_TYPE_NO_LINK;
		sole->self_role = RTW89_SELF_ROLE_AP;
		rtw89_fw_h2c_join_info(rtwdev, sole, NULL, false);
	}
#endif

#if RTW89_MON_HAS_PURE_VIF
	{
		struct rtw89_vif *old_pure;

#if RTW89_MON_PURE_VIF_RCU
		old_pure = rcu_dereference_protected(rtwdev->pure_monitor_mode_vif,
						     rtw89_mon_lockdep_is_held(rtwdev));
		rcu_assign_pointer(rtwdev->pure_monitor_mode_vif, sole);
#else
		old_pure = READ_ONCE(rtwdev->pure_monitor_mode_vif);
		WRITE_ONCE(rtwdev->pure_monitor_mode_vif, sole);
#endif
		if (old_pure && old_pure != sole)
			need_sync = true;
	}
#endif

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_find_ctx_locked(rtwdev);
	if (ctx) {
		struct ieee80211_vif *old_vif =
			rcu_dereference_protected(ctx->mon_vif,
						  lockdep_is_held(&rtw89_mon_lock));

		if (old_vif && old_vif != rtwvif_to_vif(sole))
			need_sync = true;
		rcu_assign_pointer(ctx->mon_vif, rtwvif_to_vif(sole));
	}
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	if (!ctx) {
		ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);
		if (!ctx) {
			rtw89_warn(rtwdev, "failed to allocate monitor context\n");
		} else {
			ctx->rtwdev = rtwdev;
			RCU_INIT_POINTER(ctx->mon_vif, rtwvif_to_vif(sole));
			spin_lock_irqsave(&rtw89_mon_lock, flags);
			list_add_rcu(&ctx->list, &rtw89_mon_list);
			spin_unlock_irqrestore(&rtw89_mon_lock, flags);
		}
	}

	if (need_sync)
		synchronize_rcu();
}

int rtw89_mon_add_vif_post(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	(void)vif;
	rtw89_mon_sync_standalone_vif(rtwdev, NULL);
	return 0;
}

void rtw89_mon_remove_vif_pre(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	rtw89_mon_sync_standalone_vif(rtwdev, vif_to_rtwvif_safe(vif));
}

u32 rtw89_mon_refine_rx_fltr(u32 rx_fltr)
{
	if (rx_fltr & B_AX_SNIFFER_MODE)
		rx_fltr &= ~RTW89_MON_SNIFFER_CLRMASK;

	return rx_fltr;
}

void rtw89_mon_prepare_rx_filter(struct rtw89_dev *rtwdev, u32 *rx_fltr,
				 unsigned int *total_flags)
{
	(void)total_flags;

	if (!(rtwdev->hw->conf.flags & IEEE80211_CONF_MONITOR)) {
		rtwdev->hal.rx_fltr &= ~B_AX_SNIFFER_MODE;
		return;
	}

	rtwdev->hal.rx_fltr |= B_AX_SNIFFER_MODE;
	*rx_fltr |= B_AX_SNIFFER_MODE;
	*rx_fltr = rtw89_mon_refine_rx_fltr(*rx_fltr);
}

static void rtw89_mon_write_rx_fltr_reg(struct rtw89_dev *rtwdev, u8 mac_idx,
					u32 rx_fltr)
{
#if RTW89_MON_HAS_SET_RX_FLTR
	rtw89_mac_set_rx_fltr(rtwdev, mac_idx, rx_fltr);
#elif RTW89_MON_HAS_MAC_DEF_RX_FLTR
	rtw89_write32_mask(rtwdev,
			   rtw89_mac_reg_by_idx(rtwdev,
						rtwdev->chip->mac_def->rx_fltr,
						mac_idx),
			   B_AX_RX_FLTR_CFG_MASK, rx_fltr);
#else
	rtw89_write32_mask(rtwdev,
			   rtw89_mac_reg_by_idx(R_AX_RX_FLTR_OPT, mac_idx),
			   B_AX_RX_FLTR_CFG_MASK, rx_fltr);
#endif
}

void rtw89_mon_apply_rx_filter(struct rtw89_dev *rtwdev,
			       unsigned int *total_flags)
{
	u32 rx_fltr;

	if (!(rtwdev->hw->conf.flags & IEEE80211_CONF_MONITOR)) {
		rtwdev->hal.rx_fltr &= ~B_AX_SNIFFER_MODE;
		return;
	}

	rtwdev->hal.rx_fltr |= B_AX_SNIFFER_MODE;
	rx_fltr = rtwdev->hal.rx_fltr;
	rtw89_mon_prepare_rx_filter(rtwdev, &rx_fltr, total_flags);

	if (rtwdev->scanning) {
		rx_fltr &= ~B_AX_A_BCN_CHK_EN;
		rx_fltr &= ~B_AX_A_BC;
		rx_fltr &= ~B_AX_A_A1_MATCH;
	}

	rtw89_mon_write_rx_fltr_reg(rtwdev, RTW89_MAC_0, rx_fltr);
	if (rtwdev->dbcc_en)
		rtw89_mon_write_rx_fltr_reg(rtwdev, RTW89_MAC_1, rx_fltr);
}

void rtw89_mon_config_channel(struct rtw89_dev *rtwdev, u32 changed)
{
	struct ieee80211_hw *hw = rtwdev->hw;

	if (!(changed & IEEE80211_CONF_CHANGE_MONITOR))
		return;

	if (!(hw->conf.flags & IEEE80211_CONF_MONITOR))
		return;

#if RTW89_MON_HAS_RTWDEV_OPS
	if (rtwdev->ops->assign_vif_chanctx)
		return;
#else
	if (rtwdev->chip->support_chanctx_num)
		return;
#endif

#if RTW89_MON_ENTITY_STATE_2ARGS
	if (rtw89_get_entity_state(rtwdev, RTW89_PHY_0))
		return;
#else
	if (rtw89_get_entity_state(rtwdev))
		return;
#endif

	if (!cfg80211_chandef_valid(&hw->conf.chandef))
		return;

#if RTW89_MON_HAS_CHANCTX_0
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

	list_for_each_entry_rcu(ctx, &rtw89_mon_list, list) {
		if (ctx->rtwdev == rtwdev)
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

	if (skb_headlen(skb) < sizeof(struct ieee80211_cts))
		return;

	fc = hdr->frame_control;

	desc_info->use_rate = true;
	desc_info->dis_data_fb = true;

	/*
	 * Route non-Data frames (including 10-16 byte Control frames) through
	 * the MGMT queue and clear any stale seq / LLC fields computed for them.
	 */
	if (!ieee80211_is_data(fc) ||
	    ieee80211_is_nullfunc(fc) || ieee80211_is_qos_nullfunc(fc)) {
		if (ieee80211_is_ctl(fc) ||
		    skb_headlen(skb) < sizeof(struct ieee80211_hdr_3addr)) {
			desc_info->seq = 0;
			desc_info->hdr_llc_len = 0;
			desc_info->agg_en = false;
		}
		if (tx_req->tx_type != RTW89_CORE_TX_TYPE_MGMT) {
			u8 mac_idx;

#if RTW89_MON_API_GEN >= 2
			mac_idx = tx_req->rtwvif_link ?
				  tx_req->rtwvif_link->mac_idx : RTW89_MAC_0;
#else
			mac_idx = tx_req->vif ?
				  vif_to_rtwvif_safe(tx_req->vif)->mac_idx : RTW89_MAC_0;
#endif
			tx_req->tx_type = RTW89_CORE_TX_TYPE_MGMT;
			if (mac_idx == RTW89_MAC_1)
				desc_info->qsel = desc_info->hiq ?
						  RTW89_TX_QSEL_B1_HI :
						  RTW89_TX_QSEL_B1_MGMT;
			else
				desc_info->qsel = desc_info->hiq ?
						  RTW89_TX_QSEL_B0_HI :
						  RTW89_TX_QSEL_B0_MGMT;
#if RTW89_MON_HAS_CHIP_GET_CH_DMA
			desc_info->ch_dma = rtw89_chip_get_ch_dma(rtwdev,
								  desc_info->qsel);
#else
			desc_info->ch_dma = rtw89_core_get_ch_dma(rtwdev,
								  desc_info->qsel);
#endif
		}
	}

	if (!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ) ||
	    ieee80211_is_ctl(fc)) {
#if RTW89_MON_HAS_UPD_WLAN_HDR
		desc_info->upd_wlan_hdr = false;
#endif
		desc_info->hw_ssn_sel = 0;
		desc_info->hw_seq_mode = 0;
	} else {
		desc_info->hw_ssn_sel = RTW89_MGMT_HW_SSN_SEL;
		desc_info->hw_seq_mode = RTW89_MGMT_HW_SEQ_MODE;
	}

#if RTW89_MON_HAS_TXD_EXT
	desc_info->dis_hw_rts = true;
	if (!(info->control.rates[0].flags & IEEE80211_TX_RC_USE_RTS_CTS) ||
	    desc_info->is_bmc || (info->flags & IEEE80211_TX_CTL_NO_ACK))
		desc_info->dis_rts = true;
#endif

	if (info->flags & IEEE80211_TX_CTL_NO_ACK) {
#if RTW89_MON_HAS_TXD_EXT
		desc_info->no_ack = true;
#endif
		desc_info->tx_cnt_lmt_en = true;
		desc_info->tx_cnt_lmt = 1;
	}

	/*
	 * Preserve the mgmt basic/NO_CCK rate when rtw89_core_tx_update_injection()
	 * is absent or patched against negative idx; otherwise assign a valid
	 * fallback hardware rate when idx < 0.
	 */
	if (idx < 0) {
#if RTW89_MON_TX_INJ_SAFE
		if (tx_req->tx_type == RTW89_CORE_TX_TYPE_MGMT &&
		    desc_info->data_rate)
			return;
#endif
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
		bool is_be = false;
		u8 nss, mcs;

#if RTW89_MON_HAS_CHIP_BE
		is_be = rtwdev->chip->chip_gen == RTW89_CHIP_BE;
#endif
		if (flags & IEEE80211_TX_RC_160_MHZ_WIDTH)
			desc_info->data_bw = 3;
		else if (flags & IEEE80211_TX_RC_80_MHZ_WIDTH)
			desc_info->data_bw = 2;
		else if (flags & IEEE80211_TX_RC_40_MHZ_WIDTH)
			desc_info->data_bw = 1;

		if (flags & IEEE80211_TX_RC_SHORT_GI)
			desc_info->gi_ltf = 1;

		if (flags & IEEE80211_TX_RC_VHT_MCS) {
			nss = min_t(u8, (u8)idx >> 4, 3);
			mcs = min_t(u8, (u8)idx & 0xf, is_be ? 11 : 9);
#if RTW89_MON_HAS_CHIP_BE
			if (is_be)
				desc_info->data_rate =
					RTW89_HW_RATE_V1_VHT_NSS1_MCS0 +
					(nss << 5) + mcs;
			else
#endif
				desc_info->data_rate =
					RTW89_HW_RATE_VHT_NSS1_MCS0 +
					(nss << 4) + mcs;
		} else if (flags & IEEE80211_TX_RC_MCS) {
			mcs = min_t(u8, (u8)idx, 31);
#if RTW89_MON_HAS_CHIP_BE
			if (is_be)
				desc_info->data_rate =
					RTW89_HW_RATE_V1_MCS0 + mcs;
			else
#endif
				desc_info->data_rate =
					RTW89_HW_RATE_MCS0 + mcs;
		} else if (info->band == NL80211_BAND_2GHZ) {
			desc_info->data_rate = RTW89_HW_RATE_CCK1 +
					       min_t(u8, (u8)idx, 11);
		} else {
			desc_info->data_rate = RTW89_HW_RATE_OFDM6 +
					       min_t(u8, (u8)idx, 7);
		}
	}
#endif
}
