# Technical notes (`PATCH v5` and `rtw89_mon`)

This document explains the changes in [`patches/v5/`](../patches/v5/) and how the
out-of-tree integrator (`scripts/patch_rtw89.py` + `src/rtw89_mon.{c,h}`) maps to them.
All changes are in the host driver; the firmware image is not modified.

---

## 1. NULL vif in `rtw89_ops_tx()` (patch 1)

rtw89 sets `IEEE80211_HW_WANT_MONITOR_VIF` but not `IEEE80211_HW_QUEUE_CONTROL`. For a
frame sent on a passive monitor interface, `ieee80211_tx_frags()` passes the virtual
monitor vif if `local->monitor_sdata` exists, and `NULL` otherwise (`net/mac80211/tx.c`).
mac80211 only creates the virtual monitor vif while no non-monitor interface is open
(`net/mac80211/iface.c`), so injecting on a passive `mon0` while `wlan0` is up as a
station gives `info->control.vif == NULL`.

Since `a0e97ae3f332` ("wifi: rtw89: add ieee80211::remain_on_channel ops"),
`rtw89_ops_tx()` reads `rtwvif->offchan` from `vif->drv_priv` unconditionally, which
dereferences memory near `NULL`. Patch 1 drops such frames (`Fixes:` + `Cc: stable`).

## 2. Radiotap "no rate" index (patch 2)

Without a rate in the radiotap header, `info->control.rates[0].idx` is `-1`.
`rtw89_core_tx_update_injection()` (added in `e96abe4fd7cd`) stored it in an unsigned `u8`,
writing `0xff` + base rate into `desc_info->data_rate`. Patch 2 switches `idx` to `s8`;
management/control frames keep the rate from `rtw89_core_tx_update_mgmt_info()`, while
injected data frames fall back to `data_retry_lowest_rate` (or `OFDM6` with
`IEEE80211_TX_CTL_NO_CCK_RATE`). Includes `Fixes:` + `Cc: stable`.

## 3. Monitor RX filter & `FIF_OTHER_BSS` across scan/ROC/MAC reinit (patch 3)

`rtw89_ops_configure_filter()` sets `B_AX_SNIFFER_MODE` (`BIT(0)`) in `rtwdev->hal.rx_fltr`
while `IEEE80211_CONF_MONITOR` is active, but leaves hardware target-address, CAM, beacon,
and UID drop conditions enabled. In addition, `rtw89_hw_scan_complete_cb()`,
`rtw89_roc_end()`, `rx_fltr_init_ax()`, and `rx_fltr_init_be()` rewrite `rtwdev->hal.rx_fltr`
directly to `R_AX_RX_FLTR_OPT` (`0xCE20`) / `R_BE_RX_FLTR_OPT`.

Patch 3 retains `FIF_CONTROL | FIF_PSPOLL | FIF_OTHER_BSS`, tracks `FIF_OTHER_BSS` in
`rtwdev->monitor_other_bss`, and introduces `rtw89_mac_refine_rx_fltr()` in `mac.h`,
clearing the following drop bits whenever `(rx_fltr & B_AX_SNIFFER_MODE)` **or**
`READ_ONCE(rtwdev->monitor_other_bss)` is true without mutating baseline
`rtwdev->hal.rx_fltr`:

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

`B_AX_SNIFFER_MODE` remains gated strictly on `IEEE80211_CONF_MONITOR` (`0x00004401` in
standalone passive monitor vs `0x00004400` with concurrent `wlan0` + `MONITOR_FLAG_ACTIVE`
`otherbss`), preserving hardware Auto-ACK/BA/decryption on associated station interfaces.

## 4. Monitor VIF lifecycle, RCU, and `MONITOR_FLAG_ACTIVE` (patches 4 and 6)

`rtw89_ops_tx()` runs without the wiphy lock, while interface add/remove updates
`rtwdev->pure_monitor_mode_vif` under the wiphy lock. Furthermore,
`ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` **before** clearing
`local->monitor_sdata`, so mac80211 can briefly pass a monitor vif whose MAC ID is already
being torn down.

- **Patch 4** annotates `pure_monitor_mode_vif` with `__rcu`, converts readers to
  `rcu_access_pointer()` / `rcu_dereference_protected()`, and updates the pointer via
  `rtw89_recalc_pure_monitor_vif()` + `rtw89_set_pure_monitor_vif()`. Walking
  `rtwdev->rtwvifs_list` guarantees the pointer is published if and only if a single
  monitor vif is the sole open interface, and waits for an RCU grace period before
  releasing the link, port, and MAC ID on teardown.
- **Patch 6** advertises `NL80211_FEATURE_ACTIVE_MONITOR` in `wiphy->features` and handles
  both monitor TX modes safely under `rcu_read_lock()` in `rtw89_ops_tx()`:
  - **Passive monitor (`info->control.vif == NULL`):** resolved through
    `rcu_dereference(rtwdev->pure_monitor_mode_vif)` (dropped cleanly if any non-monitor
    interface is open).
  - **Active monitor (`MONITOR_FLAG_ACTIVE`, `info->control.vif == &sdata->vif`):**
    transmitted concurrently via its own driver `rtwvif` while `READ_ONCE(rtwvif->mon_tx_ready)`
    is set; `rtw89_ops_remove_interface()` clears `mon_tx_ready` and coalesces a single
    `synchronize_rcu()` before releasing resources.
  - Tracks open driver monitor VIFs in `rtwdev->monitor_vifs` (`rtw89_is_monitoring()`) so
    concurrent active monitors reserve `RTW89_RADIOTAP_ROOM`, enable PHYSTS parsing, and
    parse scan channel IEs before pushing radiotap headers; validates linear 802.11 header
    lengths via `skb_headlen(skb)` centrally in `rtw89_core_tx_write_link()`.

## 5. Firmware role for the monitor MAC ID (patch 5)

`rtw89_vif_type_mapping()` left `net_type`/`self_role` at zero for monitor interfaces
(`RTW89_NET_TYPE_NO_LINK`, `RTW89_SELF_ROLE_CLIENT`). Static inspection of RTL8852AE
firmware `0.13.36.2` (`H2C_FUNC_MAC_JOININFO_Handler` at `0xb89949c0`) confirmed that
`(self_role != RTW89_SELF_ROLE_AP && op == 1)` (`RTW89_NET_TYPE_NO_LINK`) enters
`HAL8852A_Role_Disconnect` (`0xb897ddec`), which deactivates the role entry just created by
`ROLE_MAINTAIN` (`active_mask &= ~(1 << idx)`) and skips initializing its CMAC control entry
via `CCTL_ResetByMacid` (`0xb89bddac`). Setting `self_role = RTW89_SELF_ROLE_AP` while
keeping `net_type = RTW89_NET_TYPE_NO_LINK` runs `CCTL_ResetByMacid` and
`HAL8852A_Role_Connect` (`0xb897dc78`) without enabling client TBTT tracking or AP beacon
transmission.

## 6. Injected frames on the TX & completion paths (patches 7–9)

- **Patch 7:** injected non-data frames (including 10-byte ACK and 16-byte RTS) are routed
  through `RTW89_CORE_TX_TYPE_MGMT`, while mac80211-generated control frames (such as
  BlockAckReq) retain the normal data path. `hdr->seq_ctrl` is only read on non-control
  frames with at least 24 linear bytes.
- **Patch 8:** for `IEEE80211_TX_CTL_INJECTED`, skips `rtw89_core_txq_check_agg()` (so
  station TXQ pushes cannot start/stop BA sessions or set `AMPDU`),
  `rtw89_traffic_stats_accu()`, `rtw89_wow_parse_akm()`, `rtw89_core_tx_btc_spec_pkt_notify()`,
  `rtw89_core_tx_update_he_qos_htc()`, and `rtw89_core_tx_update_ampdu_info()`.
- **Patch 9:**
  - Sets `hw->max_rate_tries = 63` (`6`-bit `DATA_TXCNT_LMT` ceiling; mac80211 otherwise
    clamps `rates[0].count` to `1` when `max_rate_tries == 0`, silently discarding
    `IEEE80211_RADIOTAP_DATA_RETRIES`).
  - Clamps positive radiotap HT/VHT/legacy rate indices to hardware limits (`umin`).
  - Disables `upd_wlan_hdr`, `hw_ssn_sel`, and `hw_seq_mode` whenever
    `!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ) || ieee80211_is_ctl(hdr->frame_control)`,
    and assigns management sequence numbers on non-control injected frames when `ASSIGN_SEQ`
    is set.
  - Clears `hw_rts_en` on injected frames while gating `rts_en` on
    `IEEE80211_TX_RC_USE_RTS_CTS && !is_bmc && !NO_ACK`.
  - Programs `tx_cnt_lmt = 1` on `IEEE80211_TX_CTL_NO_ACK` (plus `BE_TXD_BODY6_NO_ACK` on
    Wi-Fi 7 BE) or `umin(rates[0].count, 63)` when `NO_ACK` is clear. Because AX TXWD v0
    lacks a per-packet `NO_ACK` bit and reports `RTW89_TX_RETRY_LIMIT` in PCIe RPP / USB
    TXRPT completions after the single unacknowledged transmission, sets
    `IEEE80211_TX_STAT_NOACK_TRANSMITTED` (`mac.h` / `pci.c`) and suppresses false
    `tx_ring->tx_retry_lmt` increments on `NO_ACK` frames.

## 7. Channel restoration when leaving idle on non-chanctx devices (patch 10)

`rtw89_alloc_ieee80211_hw()` leaves `rtwdev->ops->assign_vif_chanctx == NULL` whenever
`chip->support_chanctx_num == 0` **or** firmware lacks `SCAN_OFFLOAD` or `BEACON_FILTER`
(on RTL8852AE with firmware `0.13.36.2`, `support_chanctx_num == 1` but `BEACON_FILTER` is
absent, so `no_chanctx` is always true). When the last monitor interface goes down,
`rtw89_ops_stop()` clears `RTW89_PHY_0` while mac80211 keeps `hw.conf.chandef`. On restart
on the same channel, `rtw89_ops_start()` sets `RTW89_FLAG_POWERON` (so `rtw89_leave_ips()`
returns early) and `ieee80211_hw_conf_chan()` omits `IEEE80211_CONF_CHANGE_CHANNEL`,
emitting `IEEE80211_CONF_CHANGE_IDLE` (and `IEEE80211_CONF_CHANGE_MONITOR` for passive
monitors).

Patch 10 restores `hw->conf.chandef` in `rtw89_ops_config()` on
`IEEE80211_CONF_CHANGE_IDLE | IEEE80211_CONF_CHANGE_MONITOR` when
`!(hw->conf.flags & IEEE80211_CONF_IDLE)`, `rtw89_is_monitoring(rtwdev)`,
`!rtwdev->ops->assign_vif_chanctx`, and `!rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`.

---

## 8. Out-of-tree integrator (`scripts/patch_rtw89.py` + `src/rtw89_mon.{c,h}`)

For distribution kernels (`Linux 6.1 - 7.x`), `scripts/patch_rtw89.py` patches a copy of
the `rtw89` sources and links `src/rtw89_mon.c` into `rtw89_core.ko` (exporting
`rtw89_mon_is_monitoring` for `rtw89_pci.ko` / `rtw89_usb.ko`):

- Detects API Gen 1 (`6.1 - 6.12`, legacy `rtw89_vif`) vs Gen 2 (`6.13+`, MLO
  `rtw89_vif_link`) alongside granular per-symbol capability flags in
  `rtw89_mon_autoconf.h` (`rtw89_mac_set_rx_fltr`, `mac_def->rx_fltr`,
  `rtw89_get_entity_state` arity, `RTW89_CHANCTX_0`, `rtw89_chip_get_ch_dma`,
  `RTW89_CHIP_BE` V1 HT/VHT rate strides, `rtw89_physts_parsing_init`, `wiphy_lock` vs
  `rtwdev->mutex`, and `struct rtw89_tx_desc_info` fields).
- Tracks per-device standalone and active monitor VIFs with dynamically allocated
  RCU-freed nodes (`list_add_rcu` / `list_del_rcu` / `kfree_rcu`) and `lockdep_is_held()`
  assertions.
- Hooks `core.h`, `core.c`, `mac80211.c`, `mac.c`, `mac_be.c`, `pci.c`, `mac.h`, `phy.c`,
  and `fw.c` to match `PATCH v5`, failing fast (`RuntimeError`) if any mandatory hook fails
  to match.
- `--revert` restores all modified files byte-for-byte from `.orig` backups.
- Verified in CI: `--apply`/`--revert` across `v6.1`, `v6.6`, `v6.12` (Gen 1), `v6.15`,
  `v6.18` (Gen 2), and `v7.4-rc1`, plus Kbuild `make W=1 C=1 modules` compilation on LTS
  `v6.6`, LTS `v6.12`, LTS `v6.18`, and `v7.4-rc1` (`CONFIG_PROVE_LOCKING=y`).
