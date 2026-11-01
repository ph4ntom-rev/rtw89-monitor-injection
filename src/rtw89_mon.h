/* SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause */
/*
 * Copyright (c) 2026 rtw89-8852ae-monitor contributors
 *
 * mac80211 NL80211_IFTYPE_MONITOR RX filter & Radiotap diagnostic TX support
 * for Realtek 802.11ax (RTW89_CHIP_AX / RTL8852AE) wireless adapters.
 */

#ifndef __RTW89_MON_H__
#define __RTW89_MON_H__

#include "core.h"

#if __has_include("rtw89_mon_autoconf.h")
#include "rtw89_mon_autoconf.h"
#endif

/*
 * Default to API Gen 2 (Linux 6.11+ MLO struct rtw89_vif_link).
 * scripts/patch_rtw89.py auto-generates rtw89_mon_autoconf.h with
 * RTW89_MON_API_GEN = 1 when targeting older kernels (Linux 6.1 - 6.10).
 */
#ifndef RTW89_MON_API_GEN
#define RTW89_MON_API_GEN 2
#endif

#ifndef RTW89_MON_HAS_PURE_VIF
#define RTW89_MON_HAS_PURE_VIF 1
#endif

void rtw89_mon_register_hw_caps(struct rtw89_dev *rtwdev);
bool rtw89_mon_is_active(struct rtw89_dev *rtwdev);

int rtw89_mon_add_vif_post(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif);
void rtw89_mon_remove_vif_pre(struct rtw89_dev *rtwdev, struct ieee80211_vif *vif);

void rtw89_mon_apply_rx_filter(struct rtw89_dev *rtwdev, unsigned int *total_flags);
void rtw89_mon_post_set_channel(struct rtw89_dev *rtwdev);

struct ieee80211_vif *rtw89_mon_resolve_tx_vif(struct rtw89_dev *rtwdev,
					       struct ieee80211_vif *vif);
void rtw89_mon_fixup_tx_desc(struct rtw89_dev *rtwdev,
			     struct rtw89_core_tx_request *tx_req);

#endif /* __RTW89_MON_H__ */
