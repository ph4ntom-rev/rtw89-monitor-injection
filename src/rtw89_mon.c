// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/*
 * Copyright (c) 2026 rtw89-8852ae-monitor contributors
 *
 * mac80211 NL80211_IFTYPE_MONITOR & IEEE 802.11 diagnostic Radiotap TX support
 * for Realtek 802.11ax (RTW89_CHIP_AX / RTL8852AE) PCIe wireless adapters.
 *
 * Architectural overview (using standard upstream rtw89 MAC & H2C interfaces):
 * 1. MAC RX Filter Configuration (reg.h):
 *    - Clears Band 0 RX packet filter pause state and disables hardware
 *      ADDR_CAM / BSSID match drops in R_AX_RX_FLTR_OPT while honoring
 *      mac80211 FIF_FCSFAIL.
 *    - Programs R_AX_CTRL_FLTR, R_AX_MGNT_FLTR, and R_AX_DATA_FLTR subtype
 *      target tables (0x55555555) to deliver all 16 Control, Management, and
 *      Data subtypes to the host RX DMA ring for Radiotap/Wireshark capture.
 * 2. Power-Save Suppression:
 *    - Keeps RF/BB active via rtw89_leave_ps_mode() while a monitor VIF is
 *      present.
 * 3. Unassociated Monitor VIF Context Initialization (fw.h / mac.h):
 *    - Initializes default CMAC/DMAC tables (h2c_default_cmac_tbl /
 *      h2c_default_dmac_tbl) for the monitor VIF's allocated mac_id.
 *    - Registers the monitor mac_id via rtw89_fw_h2c_join_info() with
 *      RTW89_SELF_ROLE_AP and RTW89_NET_TYPE_NO_LINK so per-MACID TX
 *      scheduling is active without arming client-mode beacon-loss tracking.
 *    - Unpauses the mac_id via rtw89_fw_h2c_macid_pause(), clears the sleep
 *      bit in R_AX_MACID_SLEEP_0, and enables R_AX_CTN_TXEN queues.
 * 4. Upstream Monitor TX Bug Fixes (core.c / mac80211.c):
 *    - Resolves NULL info->control.vif pointers before vif_to_rtwvif()
 *      dereferences in rtw89_ops_tx() and rtw89_core_tx_write().
 *    - Fixes signed rate->idx == -1 handling when Radiotap omits a rate,
 *      enables TXWD_INFO (en_wd_info, use_rate, dis_data_fb) for unassociated
 *      frames lacking a peer RA mask, and bounds retries on IEEE80211_TX_CTL_NO_ACK.
 *
 * Regulatory domain enforcement (cfg80211), DFS radar rules, SAR limits, and
 * hardware TX power tables remain completely unmodified.
 */

#include <linux/etherdevice.h>
#include <linux/ieee80211.h>
#include <linux/spinlock.h>
#include <net/mac80211.h>

#include "core.h"
#include "debug.h"
#include "fw.h"
#include "mac.h"
#include "ps.h"
#include "reg.h"
#include "txrx.h"
#include "rtw89_mon.h"

/*
 * Standard RTW89_CHIP_AX Band 0 MAC register fallbacks (drivers/net/wireless/realtek/rtw89/reg.h).
 * Guarded with #ifndef for cross-kernel header compatibility (Linux 6.1 - 7.x+).
 */
#ifndef R_AX_CTN_TXEN
#define R_AX_CTN_TXEN			0xC348
#endif
#ifndef B_AX_CTN_TXEN_ALL_MASK
#define B_AX_CTN_TXEN_ALL_MASK		0x0000FFFFU
#endif

#ifndef R_AX_MACID_SLEEP_0
#define R_AX_MACID_SLEEP_0		0xC2C0
#endif

#ifndef R_AX_PKT_CTRL_B0_PAUSE
#define R_AX_PKT_CTRL_B0_PAUSE		0xCE06
#endif

#ifndef R_AX_RX_FLTR_OPT
#define R_AX_RX_FLTR_OPT		0xCE20
#endif
#ifndef B_AX_A_CRC32_ERR
#define B_AX_A_CRC32_ERR		BIT(0)
#endif
#ifndef B_AX_A_PWR_MGNT
#define B_AX_A_PWR_MGNT			BIT(1)
#endif
#ifndef B_AX_A_UC_CAM_MATCH
#define B_AX_A_UC_CAM_MATCH		BIT(2)
#endif
#ifndef B_AX_A_BC_CAM_MATCH
#define B_AX_A_BC_CAM_MATCH		BIT(3)
#endif
#ifndef B_AX_A_MC_LIST_CAM_MATCH
#define B_AX_A_MC_LIST_CAM_MATCH	BIT(4)
#endif
#ifndef B_AX_A_BCN_CHK_EN
#define B_AX_A_BCN_CHK_EN		BIT(6)
#endif
#ifndef B_AX_UID_FILTER_MASK
#define B_AX_UID_FILTER_MASK		GENMASK(15, 8)
#endif

#ifndef R_AX_CTRL_FLTR
#define R_AX_CTRL_FLTR			0xCE24
#endif
#ifndef R_AX_MGNT_FLTR
#define R_AX_MGNT_FLTR			0xCE28
#endif
#ifndef R_AX_DATA_FLTR
#define R_AX_DATA_FLTR			0xCE2C
#endif

/*
 * Each 32-bit R_AX_*_FLTR register encodes sixteen 2-bit subtype target fields
 * (see rx_fltr_init in upstream mac.c):
 *   0b00 = drop in MAC, 0b01 = forward to host RX ring, 0b10 = forward to MCU.
 * 0x55555555 forwards all 16 IEEE 802.11 subtypes to the host RX ring.
 */
#define RTW89_MON_FLTR_ALL_TO_HOST	0x55555555U

#define RTW89_MON_MAX_DEVS		4

struct rtw89_mon_ctx {
	struct rtw89_dev *rtwdev;
	struct ieee80211_vif *mon_vif;
	u8 mon_mac_id;
	u8 mon_port;
	bool role_joined;
	unsigned int last_fif_flags;
};

static DEFINE_SPINLOCK(rtw89_mon_lock);
static struct rtw89_mon_ctx rtw89_mon_table[RTW89_MON_MAX_DEVS];

static struct rtw89_mon_ctx *rtw89_mon_get_ctx(struct rtw89_dev *rtwdev, bool create)
{
	struct rtw89_mon_ctx *free_slot = NULL;
	int i;

	for (i = 0; i < RTW89_MON_MAX_DEVS; i++) {
		if (rtw89_mon_table[i].rtwdev == rtwdev)
			return &rtw89_mon_table[i];
		if (!free_slot && !rtw89_mon_table[i].rtwdev)
			free_slot = &rtw89_mon_table[i];
	}

	if (create && free_slot) {
		memset(free_slot, 0, sizeof(*free_slot));
		free_slot->rtwdev = rtwdev;
		return free_slot;
	}

	return NULL;
}

void rtw89_mon_register_hw_caps(struct rtw89_dev *rtwdev)
{
	struct ieee80211_hw *hw = rtwdev->hw;

	hw->wiphy->interface_modes |= BIT(NL80211_IFTYPE_MONITOR);

	/*
	 * Request an explicit driver-level monitor VIF from mac80211 so
	 * rtw89_ops_add_interface() allocates a hardware port/mac_id context.
	 */
	ieee80211_hw_set(hw, WANT_MONITOR_VIF);
	ieee80211_hw_set(hw, SIGNAL_DBM);
}
EXPORT_SYMBOL(rtw89_mon_register_hw_caps);

bool rtw89_mon_is_active(struct rtw89_dev *rtwdev)
{
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	bool active = false;

	if (rtwdev->hw->conf.flags & IEEE80211_CONF_MONITOR)
		return true;

#if RTW89_MON_HAS_PURE_VIF
	if (rtwdev->pure_monitor_mode_vif)
		return true;
#endif

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, false);
	if (ctx && ctx->mon_vif)
		active = true;
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	return active;
}
EXPORT_SYMBOL(rtw89_mon_is_active);

static void rtw89_mon_program_mac_rx_tx_regs(struct rtw89_dev *rtwdev,
					     u8 mac_id,
					     unsigned int fif_flags)
{
	u32 fltr;

	if (rtwdev->chip->chip_gen != RTW89_CHIP_AX)
		return;

	/* Ensure Band 0 RX packet filter is unpaused after scan/channel ops */
	rtw89_write8_clr(rtwdev, R_AX_PKT_CTRL_B0_PAUSE, BIT(0));

	/*
	 * Configure R_AX_RX_FLTR_OPT:
	 * Disable unicast/broadcast/multicast ADDR_CAM and beacon BSSID drops
	 * so frames from arbitrary BSSs reach mac80211 in monitor mode.
	 */
	fltr = rtw89_read32(rtwdev, R_AX_RX_FLTR_OPT);
	fltr &= ~(B_AX_A_UC_CAM_MATCH |
		  B_AX_A_BC_CAM_MATCH |
		  B_AX_A_MC_LIST_CAM_MATCH |
		  B_AX_A_BCN_CHK_EN |
		  B_AX_UID_FILTER_MASK);
	fltr |= B_AX_A_PWR_MGNT;

	if (fif_flags & FIF_FCSFAIL)
		fltr |= B_AX_A_CRC32_ERR;
	else
		fltr &= ~B_AX_A_CRC32_ERR;

	rtwdev->hal.rx_fltr = fltr;
	rtw89_write32(rtwdev, R_AX_RX_FLTR_OPT, fltr);

	/* Forward all Control, Management, and Data subtypes to host RX DMA */
	rtw89_write32(rtwdev, R_AX_CTRL_FLTR, RTW89_MON_FLTR_ALL_TO_HOST);
	rtw89_write32(rtwdev, R_AX_MGNT_FLTR, RTW89_MON_FLTR_ALL_TO_HOST);
	rtw89_write32(rtwdev, R_AX_DATA_FLTR, RTW89_MON_FLTR_ALL_TO_HOST);

	/* Keep monitor mac_id awake and Band 0 continuous TX queues enabled */
	if (mac_id < 32)
		rtw89_write32_clr(rtwdev, R_AX_MACID_SLEEP_0, BIT(mac_id));
	rtw89_write32_set(rtwdev, R_AX_CTN_TXEN, B_AX_CTN_TXEN_ALL_MASK);
}

int rtw89_mon_add_vif_post(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	const struct rtw89_chip_info *chip = rtwdev->chip;
	struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	u8 orig_self_role;
	u8 orig_net_type;
	u8 mac_id = 0;
	u8 port = 0;
	int ret = 0;

	if (vif->type != NL80211_IFTYPE_MONITOR)
		return 0;

	rtw89_leave_ps_mode(rtwdev);

#if RTW89_MON_API_GEN >= 2
	{
		struct rtw89_vif_link *rtwvif_link;

		rtwvif_link = rtw89_vif_get_link_inst(rtwvif, 0);
		if (!rtwvif_link)
			return -EINVAL;

		mac_id = rtwvif_link->mac_id;
		port = rtwvif_link->port;

		if (chip->ops->h2c_default_cmac_tbl)
			chip->ops->h2c_default_cmac_tbl(rtwdev, rtwvif_link, NULL);
		if (chip->ops->h2c_default_dmac_tbl)
			chip->ops->h2c_default_dmac_tbl(rtwdev, rtwvif_link, NULL);

		/*
		 * Activate per-MACID TX context with RTW89_SELF_ROLE_AP and
		 * RTW89_NET_TYPE_NO_LINK so diagnostic Radiotap TX frames can
		 * be scheduled without enabling STA beacon-loss monitoring.
		 */
		orig_self_role = rtwvif_link->self_role;
		orig_net_type = rtwvif_link->net_type;
		rtwvif_link->self_role = RTW89_SELF_ROLE_AP;
		rtwvif_link->net_type = RTW89_NET_TYPE_NO_LINK;

		ret = rtw89_fw_h2c_join_info(rtwdev, rtwvif_link, NULL, false);

		rtwvif_link->self_role = orig_self_role;
		rtwvif_link->net_type = orig_net_type;
	}
#else
	mac_id = rtwvif->mac_id;
	port = rtwvif->port;

	if (chip->ops->h2c_default_cmac_tbl)
		chip->ops->h2c_default_cmac_tbl(rtwdev, rtwvif, NULL);
	if (chip->ops->h2c_default_dmac_tbl)
		chip->ops->h2c_default_dmac_tbl(rtwdev, rtwvif, NULL);

	orig_self_role = rtwvif->self_role;
	orig_net_type = rtwvif->net_type;
	rtwvif->self_role = RTW89_SELF_ROLE_AP;
	rtwvif->net_type = RTW89_NET_TYPE_NO_LINK;

	ret = rtw89_fw_h2c_join_info(rtwdev, rtwvif, NULL, false);

	rtwvif->self_role = orig_self_role;
	rtwvif->net_type = orig_net_type;
#endif

	rtw89_fw_h2c_macid_pause(rtwdev, mac_id & 0x1F, mac_id >> 5, false);

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, true);
	if (ctx) {
		ctx->mon_vif = vif;
		ctx->mon_mac_id = mac_id;
		ctx->mon_port = port;
		ctx->role_joined = (ret == 0);
	}
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	rtw89_mon_program_mac_rx_tx_regs(rtwdev, mac_id, 0);

	rtw89_info(rtwdev,
		   "monitor mode initialized on mac_id=%u port=%u (ret=%d)\n",
		   mac_id, port, ret);
	return 0;
}
EXPORT_SYMBOL(rtw89_mon_add_vif_post);

void rtw89_mon_remove_vif_pre(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif)
{
	struct rtw89_vif *rtwvif = vif_to_rtwvif(vif);
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	bool need_disconn = false;

	if (vif->type != NL80211_IFTYPE_MONITOR)
		return;

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, false);
	if (ctx && ctx->mon_vif == vif) {
		need_disconn = ctx->role_joined;
		ctx->mon_vif = NULL;
		ctx->role_joined = false;
	}
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	if (!need_disconn)
		return;

#if RTW89_MON_API_GEN >= 2
	{
		struct rtw89_vif_link *rtwvif_link;

		rtwvif_link = rtw89_vif_get_link_inst(rtwvif, 0);
		if (rtwvif_link)
			rtw89_fw_h2c_join_info(rtwdev, rtwvif_link, NULL, true);
	}
#else
	rtw89_fw_h2c_join_info(rtwdev, rtwvif, NULL, true);
#endif
}
EXPORT_SYMBOL(rtw89_mon_remove_vif_pre);

void rtw89_mon_apply_rx_filter(struct rtw89_dev *rtwdev, unsigned int *total_flags)
{
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	unsigned int fif = total_flags ? *total_flags : 0;
	u8 mac_id = 0;

	if (!rtw89_mon_is_active(rtwdev))
		return;

	rtw89_leave_ps_mode(rtwdev);

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, true);
	if (ctx) {
		if (total_flags)
			ctx->last_fif_flags = fif;
		else
			fif = ctx->last_fif_flags;
		mac_id = ctx->mon_mac_id;
	}
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	rtw89_mon_program_mac_rx_tx_regs(rtwdev, mac_id, fif);
}
EXPORT_SYMBOL(rtw89_mon_apply_rx_filter);

void rtw89_mon_post_set_channel(struct rtw89_dev *rtwdev)
{
	if (!rtw89_mon_is_active(rtwdev))
		return;

	/* Re-apply monitor RX filter & queue state after channel switch / RFK */
	rtw89_mon_apply_rx_filter(rtwdev, NULL);
}
EXPORT_SYMBOL(rtw89_mon_post_set_channel);

struct ieee80211_vif *rtw89_mon_resolve_tx_vif(struct rtw89_dev *rtwdev,
					       struct ieee80211_vif *vif)
{
	struct rtw89_mon_ctx *ctx;
	struct ieee80211_vif *resolved = NULL;
	unsigned long flags;

	if (vif)
		return vif;

#if RTW89_MON_HAS_PURE_VIF
	if (rtwdev->pure_monitor_mode_vif)
		return rtwvif_to_vif_safe(rtwdev->pure_monitor_mode_vif);
#endif

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, false);
	if (ctx)
		resolved = ctx->mon_vif;
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	return resolved;
}
EXPORT_SYMBOL(rtw89_mon_resolve_tx_vif);

static u16 rtw89_mon_pick_hw_rate(struct rtw89_dev *rtwdev,
				  struct ieee80211_tx_info *info)
{
	const struct ieee80211_tx_rate *rate = &info->control.rates[0];
	enum nl80211_band band = info->band;

	if (rate->idx >= 0) {
		if (rate->flags & IEEE80211_TX_RC_MCS)
			return RTW89_HW_RATE_MCS0 + min_t(u8, rate->idx, 7);
		if (rate->flags & IEEE80211_TX_RC_VHT_MCS)
			return RTW89_HW_RATE_VHT_NSS1_MCS0 +
			       min_t(u8, ieee80211_rate_get_vht_mcs(rate), 9);

		/* Legacy bitrate table index */
		if (band == NL80211_BAND_2GHZ) {
			if (rate->idx < 4)
				return RTW89_HW_RATE_CCK1 + rate->idx;
			if (rate->idx < 12)
				return RTW89_HW_RATE_OFDM6 + (rate->idx - 4);
		} else {
			if (rate->idx < 8)
				return RTW89_HW_RATE_OFDM6 + rate->idx;
		}
	}

	/*
	 * Upstream rtw89_core_tx_update_injection() casts signed s8 idx == -1
	 * to u8 255 when Radiotap omits a rate field. Fall back to standard
	 * non-HT OFDM 6 Mbps across 2.4/5/6 GHz bands.
	 */
	return RTW89_HW_RATE_OFDM6;
}

void rtw89_mon_fixup_tx_desc(struct rtw89_dev *rtwdev,
			     struct rtw89_core_tx_request *tx_req)
{
	struct rtw89_tx_desc_info *desc_info = &tx_req->desc_info;
	struct sk_buff *skb = tx_req->skb;
	struct ieee80211_tx_info *info = IEEE80211_SKB_CB(skb);
	struct ieee80211_hdr *hdr = (struct ieee80211_hdr *)skb->data;
	struct rtw89_mon_ctx *ctx;
	unsigned long flags;
	bool is_mon_vif;
	__le16 fc;
	u8 mac_id = 0;
	u8 port = 0;

	/* Leave H2C firmware command descriptors (tx_req->vif == NULL) untouched */
	if (unlikely(tx_req->tx_type == RTW89_CORE_TX_TYPE_FWCMD))
		return;

	is_mon_vif = (!tx_req->vif ||
		      tx_req->vif->type == NL80211_IFTYPE_MONITOR ||
		      (info->flags & IEEE80211_TX_CTL_INJECTED));
	if (!is_mon_vif)
		return;

	if (skb->len < 10)
		return;

	fc = hdr->frame_control;

	spin_lock_irqsave(&rtw89_mon_lock, flags);
	ctx = rtw89_mon_get_ctx(rtwdev, false);
	if (ctx && ctx->mon_vif) {
		mac_id = ctx->mon_mac_id;
		port = ctx->mon_port;
	}
	spin_unlock_irqrestore(&rtw89_mon_lock, flags);

	/*
	 * Enable TXWD_INFO with explicit fixed rate selection (use_rate=true,
	 * dis_data_fb=true) since unassociated monitor frames have no peer
	 * station rate-adaptive (RA) entry.
	 */
	desc_info->en_wd_info = true;
	desc_info->use_rate = true;
	desc_info->dis_data_fb = true;
	desc_info->mac_id = mac_id;
	desc_info->port = port;
	desc_info->sec_en = false;
	desc_info->agg_en = false;
	desc_info->ampdu_density = 0;
	desc_info->ampdu_num = 0;
	desc_info->data_rate = rtw89_mon_pick_hw_rate(rtwdev, info);
	desc_info->data_retry_lowest_rate = desc_info->data_rate;

	desc_info->hdr_llc_len = DIV_ROUND_UP(ieee80211_hdrlen(fc), 2);

	if (ieee80211_is_mgmt(fc) || ieee80211_is_any_nullfunc(fc)) {
		tx_req->tx_type = RTW89_CORE_TX_TYPE_MGMT;
		desc_info->qsel = RTW89_TX_QSEL_B0_MGMT;
		desc_info->hiq = false;
	} else {
		tx_req->tx_type = RTW89_CORE_TX_TYPE_DATA;
		desc_info->qsel = RTW89_TX_QSEL_BE_0;
	}
#if RTW89_MON_API_GEN >= 2
	desc_info->ch_dma = rtw89_chip_get_ch_dma(rtwdev, desc_info->qsel);
#else
	desc_info->ch_dma = rtw89_core_get_ch_dma(rtwdev, desc_info->qsel);
#endif

	/* Bound hardware retries for NO_ACK, Control, and group-addressed frames */
	if ((info->flags & IEEE80211_TX_CTL_NO_ACK) ||
	    ieee80211_is_ctl(fc) ||
	    is_multicast_ether_addr(hdr->addr1)) {
		desc_info->is_bmc = true;
		desc_info->tx_cnt_lmt_en = true;
		desc_info->tx_cnt_lmt = 1;
	}

	/* Preserve caller-supplied 802.11 sequence number unless requested */
	if (!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ)) {
		desc_info->upd_wlan_hdr = false;
		desc_info->hw_seq_mode = 0;
	}
}
EXPORT_SYMBOL(rtw89_mon_fixup_tx_desc);
