# Technical notes (`PATCH v5` and `rtw89_mon`)

This document explains the changes in [`patches/v5/`](../patches/v5/) and how the
out-of-tree integrator (`scripts/patch_rtw89.py` + `src/rtw89_mon.{c,h}`) maps to them.
All changes are in the host driver; the firmware image is not modified.

---

## 1. NULL vif in `rtw89_ops_tx()` (patch 1)

rtw89 sets `IEEE80211_HW_WANT_MONITOR_VIF` but not `IEEE80211_HW_QUEUE_CONTROL`. For a
frame sent on a monitor interface, `ieee80211_tx_frags()` passes the virtual monitor vif
if `local->monitor_sdata` exists, and `NULL` otherwise (`net/mac80211/tx.c`). mac80211
only creates the virtual monitor vif while no non-monitor interface is open
(`net/mac80211/iface.c`), so injecting on `mon0` while `wlan0` is up as a station gives
`info->control.vif == NULL`.

Since `a0e97ae3f332` ("wifi: rtw89: add ieee80211::remain_on_channel ops"),
`rtw89_ops_tx()` reads `rtwvif->offchan` from `vif->drv_priv` unconditionally, which
dereferences memory near `NULL`. Patch 1 drops such frames (`Fixes:` + `Cc: stable`).

## 2. Radiotap "no rate" index (patch 2)

Without a rate in the radiotap header, `info->control.rates[0].idx` is `-1`.
`rtw89_core_tx_update_injection()` (added in `e96abe4fd7cd`) stored it in an unsigned `u8`,
writing `0xff` + base rate into `desc_info->data_rate`. Patch 2 switches `idx` to `s8`;
management/control frames keep the rate from `rtw89_core_tx_update_mgmt_info()`, while
injected data frames fall back to `data_retry_lowest_rate` (or `OFDM6` with
`IEEE80211_TX_CTL_NO_CCK_RATE`).

## 3. Monitor RX filter across scan/ROC/MAC reinit (patch 3)

`rtw89_ops_configure_filter()` sets `B_AX_SNIFFER_MODE` (`BIT(0)`) in `rtwdev->hal.rx_fltr`
while `IEEE80211_CONF_MONITOR` is active, but leaves hardware target-address, CAM, beacon,
and UID drop conditions enabled. In addition, `rtw89_hw_scan_complete_cb()`,
`rtw89_roc_end()`, `rx_fltr_init_ax()`, and `rx_fltr_init_be()` rewrite `rtwdev->hal.rx_fltr`
directly to `R_AX_RX_FLTR_OPT` (`0xCE20`) / `R_BE_RX_FLTR_OPT`.

Patch 3 retains `FIF_CONTROL | FIF_PSPOLL` (both `rx_fltr_init_ax()` and `rx_fltr_init_be()`
already forward `RTW89_CTRL` to host) and introduces `rtw89_mac_refine_rx_fltr()` in
`mac.h`, clearing the following drop bits whenever `B_AX_SNIFFER_MODE` is set without
mutating baseline `rtwdev->hal.rx_fltr`:

| Bit (`reg.h`) | Position | Effect when cleared |
| :--- | :--- | :--- |
| `B_AX_A_A1_MATCH` | `BIT(1)` | unicast to other addresses passes |
| `B_AX_A_BC` | `BIT(2)` | broadcast passes |
| `B_AX_A_MC` | `BIT(3)` | multicast passes without `FIF_ALLMULTI` |
| `B_AX_A_UC_CAM_MATCH` | `BIT(4)` | unicast from other BSSs passes without an address CAM entry |
| `B_AX_A_BC_CAM_MATCH` | `BIT(5)` | broadcast from other BSSs passes |
| `B_AX_A_MC_LIST_CAM_MATCH` | `BIT(6)` | no multicast list filtering |
| `B_AX_A_BCN_CHK_EN` | `BIT(7)` | no beacon BSSID check |
| `B_AX_UID_FILTER_MASK` | `GENMASK(31, 24)` | no UID-based drops |

`B_AX_SNIFFER_MODE` remains gated strictly on `IEEE80211_CONF_MONITOR` (never on
`FIF_OTHER_BSS` alone) so hardware auto-ACK/BA/decryption on associated station interfaces
is unaffected.

## 4. `pure_monitor_mode_vif` and RCU (patches 4 and 6)

`rtw89_ops_tx()` runs without the wiphy lock, while interface add/remove updates
`rtwdev->pure_monitor_mode_vif` under the wiphy lock. Furthermore,
`ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` **before** clearing
`local->monitor_sdata`, so mac80211 can briefly pass a monitor vif whose MAC ID is already
being torn down.

- **Patch 4** annotates `pure_monitor_mode_vif` with `__rcu`, converts readers to
  `rcu_access_pointer()` / `rcu_dereference_protected()`, and updates the pointer via
  `rtw89_recalc_pure_monitor_vif()` + `rtw89_set_pure_monitor_vif()`. Walking
  `rtwdev->rtwvifs_list` guarantees the pointer is published if and only if a single
  monitor vif is the sole open interface (including if `MONITOR_FLAG_ACTIVE` is ever
  enabled or when a second non-monitor vif goes down), and calls `synchronize_rcu()` before
  releasing the link, port, and MAC ID on teardown.
- **Patch 6** resolves `NULL` or monitor vifs in `rtw89_ops_tx()` under `rcu_read_lock()`
  and validates linear 802.11 header lengths via `skb_headlen(skb)`.

## 5. Firmware role for the monitor MAC ID (patch 5)

`rtw89_vif_type_mapping()` left `net_type`/`self_role` at zero for monitor interfaces
(`RTW89_NET_TYPE_NO_LINK`, `RTW89_SELF_ROLE_CLIENT`). Static inspection of RTL8852AE
firmware `0.13.36.2` (`H2C_FUNC_MAC_JOININFO_Handler`) confirmed that
`(self_role != RTW89_SELF_ROLE_AP && op == 1)` (`RTW89_NET_TYPE_NO_LINK`) enters the
role-disconnect branch, returning early on a new monitor MAC ID without initializing its
CMAC control entry via `CCTL_ResetByMacid`. Setting `self_role = RTW89_SELF_ROLE_AP` while
keeping `net_type = RTW89_NET_TYPE_NO_LINK` runs `CCTL_ResetByMacid` and registers the role
without enabling client TBTT tracking or AP beacon transmission.

## 6. Injected frames on the TX path (patches 7–9)

- **Patch 7:** injected non-data frames (including 10-byte ACK and 16-byte RTS) are routed
  through `RTW89_CORE_TX_TYPE_MGMT`, while mac80211-generated control frames (such as
  BlockAckReq) retain the normal data path. `skb_headlen(skb)` is also checked on
  non-monitor injected frames, and `hdr->seq_ctrl` is only read on non-control frames with
  at least 24 linear bytes.
- **Patch 8:** for `IEEE80211_TX_CTL_INJECTED`, skips `rtw89_core_txq_check_agg()` (so
  station TXQ pushes cannot start/stop BA sessions or set `AMPDU`),
  `rtw89_traffic_stats_accu()`, `rtw89_wow_parse_akm()`, `rtw89_core_tx_btc_spec_pkt_notify()`,
  `rtw89_core_tx_update_he_qos_htc()`, and `rtw89_core_tx_update_ampdu_info()`.
- **Patch 9:** disables `upd_wlan_hdr`, `hw_ssn_sel`, and `hw_seq_mode` whenever
  `!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ) || ieee80211_is_ctl(hdr->frame_control)`
  (since `ieee80211_monitor_start_xmit()` sets `ASSIGN_SEQ` by default and
  `ieee80211_tx_h_sequence()` does not clear it on `< 24`-byte control frames); enables
  hardware sequence assignment on non-control injected frames when `ASSIGN_SEQ` is set;
  gates `rts_en` on `IEEE80211_TX_RC_USE_RTS_CTS && !is_bmc && !NO_ACK`; and sets
  `tx_cnt_lmt = 1` + `BE_TXD_BODY6_NO_ACK` on `IEEE80211_TX_CTL_NO_ACK`.

## 7. Channel restoration after monitor restart (patch 10)

`rtw89_alloc_ieee80211_hw()` leaves `rtwdev->ops->assign_vif_chanctx == NULL` whenever
`chip->support_chanctx_num == 0` **or** firmware lacks `SCAN_OFFLOAD` or `BEACON_FILTER`
(on RTL8852AE with firmware `0.13.36.2`, `support_chanctx_num == 1` but `BEACON_FILTER` is
absent, so `no_chanctx` is always true). When the last monitor interface goes down,
`rtw89_ops_stop()` clears `RTW89_PHY_0` while mac80211 keeps `hw.conf.chandef`. On restart
on the same channel, `rtw89_ops_start()` sets `RTW89_FLAG_POWERON` (so `rtw89_leave_ips()`
returns early) and `ieee80211_hw_conf_chan()` omits `IEEE80211_CONF_CHANGE_CHANNEL`.

Patch 10 restores `hw->conf.chandef` in `rtw89_ops_config()` on
`IEEE80211_CONF_CHANGE_MONITOR` when `(hw->conf.flags & IEEE80211_CONF_MONITOR)`,
`!rtwdev->ops->assign_vif_chanctx`, and `!rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`.

---

## 8. Out-of-tree integrator (`scripts/patch_rtw89.py` + `src/rtw89_mon.{c,h}`)

For distribution kernels (`Linux 6.1 - 7.x`), `scripts/patch_rtw89.py` patches a copy of
the `rtw89` sources and links `src/rtw89_mon.c` into `rtw89_core.ko`:

- Detects API Gen 1 (`6.1 - 6.10`) vs Gen 2 (`6.11+`), `pure_monitor_mode_vif`,
  `rtwdev->ops`, `rtw89_core_tx_update_injection()`, and `struct rtw89_tx_desc_info`.
- Hooks `core.h`, `core.c`, `mac80211.c`, `mac.c`, and `mac_be.c` to match `PATCH v5`, and
  fails fast (`RuntimeError`) if any mandatory hook fails to match.
- `--revert` restores all modified files byte-for-byte from `.orig` backups.
- Verified in CI against `v6.6` (Gen 1), `v6.12` (Gen 2), and `v7.4-rc1` (`W=1 C=1`).
