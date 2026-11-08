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

Since a0e97ae3f332 ("wifi: rtw89: add ieee80211::remain_on_channel ops"),
`rtw89_ops_tx()` reads `rtwvif->offchan` from `vif->drv_priv` unconditionally, which
dereferences memory near `NULL`. Patch 1 drops such frames.

## 2. Radiotap "no rate" index (patch 2)

Without a rate in the radiotap header, `info->control.rates[0].idx` is `-1`.
`rtw89_core_tx_update_injection()` (added in e96abe4fd7cd) stored it in a `u8`, so the
TX descriptor got `0xff` + base rate. Patch 2 uses `s8`; management frames keep the rate
from `rtw89_core_tx_update_mgmt_info()`, data frames fall back to
`data_retry_lowest_rate` (OFDM6 with `IEEE80211_TX_CTL_NO_CCK_RATE`).

## 3. Monitor RX filter (patch 3)

`rtw89_ops_configure_filter()` sets `B_AX_SNIFFER_MODE` in monitor mode but leaves
several drop conditions active. Patch 3 keeps `FIF_CONTROL | FIF_PSPOLL` and clears these
bits in the local `rx_fltr` value while `IEEE80211_CONF_MONITOR` is set, so
`rtwdev->hal.rx_fltr` stays unchanged for normal operation:

| Bit (`reg.h`) | Position | Effect when cleared |
| :--- | :--- | :--- |
| `B_AX_A_MC` | `BIT(3)` | multicast passes without `FIF_ALLMULTI` |
| `B_AX_A_UC_CAM_MATCH` | `BIT(4)` | unicast from other BSSs passes without an address CAM entry |
| `B_AX_A_BC_CAM_MATCH` | `BIT(5)` | broadcast from other BSSs passes |
| `B_AX_A_MC_LIST_CAM_MATCH` | `BIT(6)` | no multicast list filtering |
| `B_AX_A_BCN_CHK_EN` | `BIT(7)` | no beacon BSSID check |
| `B_AX_UID_FILTER_MASK` | `GENMASK(31, 24)` | no UID based drops |

The `B_BE_*` definitions for `R_BE_RX_FLTR_OPT` use the same positions. Wi-Fi 7 chips
were not runtime-tested.

## 4. `pure_monitor_mode_vif` and RCU (patches 4 and 6)

`rtw89_ops_tx()` runs without the wiphy lock, while interface add/remove changes
`rtwdev->pure_monitor_mode_vif` under it. Also, `ieee80211_del_virtual_monitor()` calls
`drv_remove_interface()` **before** it clears `local->monitor_sdata`, so mac80211 can
still pass a monitor vif that the driver has already removed.

- Patch 4 annotates the pointer `__rcu` and updates it through
  `rtw89_set_pure_monitor_vif()`, which calls `synchronize_rcu()` whenever a published
  vif is replaced or cleared. In `rtw89_ops_remove_interface()` the pointer is cleared
  before the link, port and MAC ID are released. The update rules stay as upstream
  (adding a monitor vif sets it, any removal clears it). Because mac80211 only adds the
  virtual monitor vif while it is the only interface, this already means "the monitor vif
  is the only interface". The `rtw89_recalc_pure_monitor_vif()` helper from v4 was
  therefore dropped.
- Patch 6 resolves both `NULL` and monitor vifs in `rtw89_ops_tx()` through
  `rcu_dereference(rtwdev->pure_monitor_mode_vif)` under `rcu_read_lock()`, checks that
  the frame is at least as long as its 802.11 header, and drops it otherwise.

## 5. Firmware role for the monitor MAC ID (patch 5)

`rtw89_vif_type_mapping()` left `net_type`/`self_role` at zero for monitor interfaces
(`RTW89_NET_TYPE_NO_LINK`, `RTW89_SELF_ROLE_CLIENT`). On RTL8852AE with firmware
0.13.36.2, frames queued on that MAC ID were not transmitted. With
`RTW89_SELF_ROLE_AP` they are. This was found by testing; the reason inside the firmware
is not documented, and the cover letter asks Realtek to confirm it. `self_role` is only
used by the join info and role maintain H2C commands, and `net_type` stays `NO_LINK`, so
port configuration is not affected.

## 6. Injected frames on the TX path (patches 7–9)

- **Patch 7**: injected non-data frames (e.g. 10-byte ACK, 16-byte RTS) are classified as
  management frames. Frames built by mac80211, such as BlockAckReq, keep the data path.
  `hdr->seq_ctrl` is only read from non-control frames of at least 24 bytes.
- **Patch 8**: for `IEEE80211_TX_CTL_INJECTED`, skip `rtw89_traffic_stats_accu()` and
  `rtw89_core_tx_btc_spec_pkt_notify()` (they read `ip_hdr()`/`udp_hdr()`, which point
  into the 802.11 header for injected frames), `rtw89_wow_parse_akm()`,
  `rtw89_core_tx_update_he_qos_htc()` and `rtw89_core_tx_update_ampdu_info()`.
- **Patch 9**: without `IEEE80211_TX_CTL_ASSIGN_SEQ`, clear `upd_wlan_hdr`, `hw_ssn_sel`
  and `hw_seq_mode` so the hardware keeps the sequence number from user space. With
  `IEEE80211_TX_CTL_NO_ACK`, limit the frame to one transmission.

## 7. Channel after monitor restart (patch 10)

Chips without channel contexts (`support_chanctx_num == 0`, e.g. RTL8852AE) get the
channel through `rtw89_ops_config()`. When the last interface goes down,
`rtw89_ops_stop()` clears the `RTW89_PHY_0` entity state, while mac80211 keeps
`hw.conf.chandef`. Bringing a monitor interface up again on the same channel then only
sends `IEEE80211_CONF_CHANGE_MONITOR`, and RX/TX stay silent.

Patch 10 re-applies `hw->conf.chandef` from `rtw89_ops_config()` when monitor mode is
enabled on such a chip and `RTW89_PHY_0` is inactive. With v4 on RTL8852AE, ftrace showed
one `rtw89_set_channel()` and one `rtw89_chip_rfk_channel()` for cold start, `down/up`
and `del/add/up`. The cover letter asks whether Realtek prefers a fix in
`rtw89_ops_start()`/`rtw89_ops_stop()` instead.

---

## 8. Out-of-tree integrator

For distribution kernels, `scripts/patch_rtw89.py` edits a copy of the rtw89 sources and
links `src/rtw89_mon.c` into `rtw89_core.ko`:

- It detects the driver generation (`struct rtw89_vif_link` present or not) and whether
  `pure_monitor_mode_vif` exists. Kernels without that field use a small table in
  `rtw89_mon.c` instead.
- Changes match the v5 behaviour, including routing only **injected** control frames
  through the management path.
- Every edit is anchored on exact source text. When an anchor is not found, that step is
  skipped and missing from the `[APPLY]` output, so check the output on older kernels.
- `--revert` restores the original files from `.orig` backups.

CI applies it to the series base, builds with `W=1 C=1`, and checks that `--revert`
restores the tree exactly. Older kernels are not covered by CI.
