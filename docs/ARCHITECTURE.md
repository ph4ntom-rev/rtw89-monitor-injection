# Technical Architecture (`PATCH v4` & `rtw89_mon`)

This document describes the architecture of the **`PATCH v4`** upstream `rtw89` patch series (`patches/v4/`) and its cross-kernel out-of-tree counterpart (`src/rtw89_mon.{c,h}` + `scripts/patch_rtw89.py` for Linux `6.1`–`7.x+`).

All changes operate strictly on the host `mac80211` driver side using public hardware register definitions (`reg.h`), standard Host-to-Card (`H2C`) lifecycle calls (`mac.c` / `fw.h`), and `txwd` descriptor fields (`txrx.h`). The stock signed firmware image (`/lib/firmware/rtw89/rtw8852a_fw.bin`) is unmodified.

---

## 1. Monitor Mode MAC RX Filtering (`Patch 1/4`)

On `RTW89_CHIP_AX` / `RTW89_CHIP_BE` hardware, `rtwdev->hal.rx_fltr` defaults to `R_AX_RX_FLTR_OPT_MODE0`, which drops unicast frames not matching `ADDR_CAM`, drops beacons failing BSSID checks, and applies UID filtering.

In `rtw89_ops_configure_filter()` (`mac80211.c`):
1. `FIF_CONTROL | FIF_PSPOLL` are retained in `*new_flags` (matching the hardware `R_AX_CTRL_FLTR` / `R_BE_CTRL_FLTR` initialization to `RTW89_FWD_TO_HOST`), and `rtwdev->hal.rx_fltr` is updated from `FIF_*` flags as usual so baseline station/AP filter bits remain intact when leaving monitor mode.
2. A local `u32 rx_fltr = rtwdev->hal.rx_fltr` copy is derived before programming `rtw89_mac_set_rx_fltr()`.
3. When `hw->conf.flags & IEEE80211_CONF_MONITOR` is set, the local `rx_fltr` clears:

| Bit / Mask (`reg.h`) | Bit Position | Effect When Cleared in Monitor Mode |
| :--- | :--- | :--- |
| `B_AX_A_MC` | `BIT(3)` | Passes multicast frames even when `IFF_PROMISC` (`FIF_ALLMULTI`) is unset |
| `B_AX_A_UC_CAM_MATCH` | `BIT(4)` | Passes cross-BSS unicast frames without `ADDR_CAM` match |
| `B_AX_A_BC_CAM_MATCH` | `BIT(5)` | Passes broadcast frames across BSSs |
| `B_AX_A_MC_LIST_CAM_MATCH` | `BIT(6)` | Disables multicast list CAM filtering |
| `B_AX_A_BCN_CHK_EN` | `BIT(7)` | Disables hardware beacon BSSID filtering |
| `B_AX_UID_FILTER_MASK` | `GENMASK(31, 24)` | Disables per-UID hardware frame drops |

`rx_fltr` is written to `RTW89_MAC_0` (and `RTW89_MAC_1` when `rtwdev->dbcc_en` is active). When monitor mode exits (`!(hw->conf.flags & IEEE80211_CONF_MONITOR)`), `rtwdev->hal.rx_fltr` restores normal filtering automatically.

---

## 2. RCU Protection for `pure_monitor_mode_vif` (`Patch 2/4`)

`rtw89_ops_tx()` runs in the `mac80211` TX fastpath without holding `wiphy_lock` (`hw->wiphy->mtx`), whereas `rtw89_ops_add_interface()` and `rtw89_ops_remove_interface()` mutate `rtwdev->pure_monitor_mode_vif` under `wiphy_lock`.

To eliminate data races and Use-After-Free windows during concurrent raw TX and `iw dev mon0 del`:
* `core.h`: annotates `struct rtw89_vif __rcu *pure_monitor_mode_vif;`.
* `mac80211.c` (`rtw89_recalc_pure_monitor_vif`): called from both `rtw89_ops_add_interface()` and `rtw89_ops_remove_interface()` (at `bottom:` after `list_del_init(&rtwvif->list)`). Publishes the sole `NL80211_IFTYPE_MONITOR` entry when `list_is_singular(&rtwdev->rtwvifs_list)` (including when a second non-monitor VIF is brought down), or clears `pure_monitor_mode_vif` via `RCU_INIT_POINTER(..., NULL)` and runs `synchronize_rcu()`.
* `mac80211.c` (`rtw89_ops_remove_interface`): when `rcu_access_pointer(rtwdev->pure_monitor_mode_vif) == rtwvif`, clears `pure_monitor_mode_vif` via `RCU_INIT_POINTER(..., NULL)` and calls `synchronize_rcu()` **before** `__rtw89_ops_remove_iface_link()` releases hardware `mac_id`, port, and CAM entries.
* `core.c`: uses `rcu_access_pointer()` in `rtw89_chip_rfk_channel()` and `rcu_dereference_protected(..., lockdep_is_held(&wiphy->mtx))` in `rtw89_chip_rfk_channel_for_pure_mon_vif()`.

---

## 3. Standalone Monitor Raw Frame Injection (`Patch 3/4`)

### 3.1. `NULL` and Monitor `control.vif` Resolution in `rtw89_ops_tx()`
Because `rtw89` sets `IEEE80211_HW_WANT_MONITOR_VIF` without `IEEE80211_HW_QUEUE_CONTROL`:
* `mac80211`'s `__ieee80211_tx()` leaves `info->control.vif == NULL` whenever `local->monitor_sdata` is absent (for example, when a non-monitor VIF is also open and the injected frame's `addr2` does not match any active BSS, or during teardown/reconfig).
* Furthermore, `ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` (`rtw89_ops_remove_interface()`) **before** clearing `RCU_INIT_POINTER(local->monitor_sdata, NULL)`.

`rtw89_ops_tx()` enters `rcu_read_lock()` and gates `if (!vif || vif->type == NL80211_IFTYPE_MONITOR)` through `rcu_dereference(rtwdev->pure_monitor_mode_vif)` -> `rtwvif_to_vif(rtwvif)`, validates `skb->len >= sizeof(struct ieee80211_cts)` and `skb->len >= ieee80211_hdrlen(hdr->frame_control)`, populates `info->control.vif = vif`, and drops the `skb` cleanly via `ieee80211_free_txskb()` if no standalone monitor VIF is active or the frame header is truncated.

### 3.2. Firmware Role Initialization (`RTW89_SELF_ROLE_AP`)
`RTL8852AE` (`8852A`) runs a 32-bit Little-Endian `MIPS32` / `MIPS16e` MCU firmware (`rtw8852a_fw.bin`). Analysis of the firmware `H2C_FUNC_MAC_JOININFO` handler shows why unpatched monitor TX descriptors were silently dropped:
* Unpatched `rtw89_vif_type_mapping()` left `NL80211_IFTYPE_MONITOR` with `net_type = 0` and `self_role = 0` (`RTW89_SELF_ROLE_CLIENT`).
* During `rtw89_mac_vif_init()`, `rtw89_fw_h2c_join_info(..., dis_conn=true)` sent a disconnect `JOININFO` H2C, causing firmware to skip `CCTL` SRAM initialization (`0xb8840000 + macid * 32`) and mark the `mac_id` disconnected.
* Setting `rtwvif_link->net_type = RTW89_NET_TYPE_NO_LINK` and `rtwvif_link->self_role = RTW89_SELF_ROLE_AP` in `rtw89_vif_type_mapping()` lets the standard `rtw89_mac_vif_init()` / `rtw89_mac_vif_deinit()` (`ROLE_MAINTAIN(REMOVE)`) lifecycle initialize the `mac_id` `CCTL` entry without enabling client-mode TBTT/beacon-loss tracking or requiring extra `rtw89_fw_h2c_join_info()` calls in `mac80211.c`.

### 3.3. Short Control Frames, Injected Data Guards, Rate Fallback & Sequence Preservation (`core.c`)
* **TX Type Classification (`rtw89_core_get_tx_type`)**: `!ieee80211_is_data(fc) || ieee80211_is_any_nullfunc(fc)` routes Management, Control (`ACK` 10B, `RTS` 16B), and NullFunc frames to `RTW89_CORE_TX_TYPE_MGMT` (`RTW89_TX_QSEL_B0_MGMT`).
* **Short Header & Injected Frame Guards**:
  * `rtw89_core_tx_update_desc_info()` only reads `hdr->seq_ctrl` when `!ieee80211_is_ctl(hdr->frame_control) && skb->len >= sizeof(struct ieee80211_hdr_3addr)` (`24` bytes).
  * Because `ieee80211_monitor_start_xmit()` sets `skb_network_header` and `skb_transport_header` to the 802.11 MAC header offset, `rtw89_core_tx_write_link()` skips `rtw89_traffic_stats_accu()` and `rtw89_wow_parse_akm()` when `IEEE80211_TX_CTL_INJECTED` is set, and `rtw89_core_tx_update_desc_info()` skips `rtw89_core_tx_btc_spec_pkt_notify()`, `rtw89_core_tx_update_he_qos_htc()`, and `rtw89_core_tx_update_ampdu_info()`.
* **Injection Descriptor (`rtw89_core_tx_update_injection`)**:
  * Declares `s8 idx = info->control.rates[0].idx;` instead of `u8` (`0xff`) and falls back to `desc_info->data_retry_lowest_rate` (or `RTW89_HW_RATE_OFDM6` when `IEEE80211_TX_CTL_NO_CCK_RATE` is set) for Data frames when Radiotap omits an explicit rate (`idx < 0`).
  * Clears `desc_info->upd_wlan_hdr = false`, `desc_info->hw_ssn_sel = 0`, and `desc_info->hw_seq_mode = 0` when `!(info->flags & IEEE80211_TX_CTL_ASSIGN_SEQ)` so user-supplied 802.11 sequence numbers (`IEEE80211_RADIOTAP_F_TX_NOSEQNO`) are preserved over the air.
  * Bounds hardware retries (`tx_cnt_lmt_en = true`, `tx_cnt_lmt = 1`) when `IEEE80211_TX_CTL_NO_ACK` is set.

---

## 4. Non-Chanctx Monitor Restart Channel Restoration (`Patch 4/4`)

Chips with `support_chanctx_num == 0` (such as `RTL8852AE`) configure monitor channels through `rtw89_ops_config()` (`IEEE80211_CONF_CHANGE_CHANNEL`).

When the last monitor VIF goes down or is deleted, `rtw89_ops_stop()` powers down the MAC/BB and clears `RTW89_PHY_0` entity state (`rtw89_set_entity_state(rtwdev, RTW89_PHY_0, false)`). If a monitor VIF is brought back up on the **same** `chandef`, `mac80211` toggles `IEEE80211_CONF_CHANGE_MONITOR` without setting `IEEE80211_CONF_CHANGE_CHANNEL`, leaving RF/BB unconfigured on channel 1 defaults.

In `rtw89_ops_config()` (`mac80211.c`), when `changed & IEEE80211_CONF_CHANGE_MONITOR` is set with `IEEE80211_CONF_MONITOR` active, `!rtwdev->chip->support_chanctx_num`, `!rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`, and `cfg80211_chandef_valid(&hw->conf.chandef)`:
1. Calls `rtw89_config_entity_chandef(rtwdev, RTW89_CHANCTX_0, &hw->conf.chandef)`.
2. Calls `rtw89_set_channel(rtwdev)`.

Confirmed via kernel `function_graph` (`ftrace`) on `RTL8852AE`:
* Cold start (`iw phy phy0 interface add mon0 type monitor` + `ip link set mon0 up`): exactly **1** `rtw89_set_channel()` and **1** `rtw89_chip_rfk_channel()` (zero duplicate RF calibration).
* Same-channel `ip link set mon0 down && ip link set mon0 up`: **1** `rtw89_set_channel()` + **1** `rtw89_chip_rfk_channel()`.
* Same-channel `iw dev mon0 del && iw phy phy0 interface add mon0 type monitor && ip link set mon0 up` (without re-running `iw set channel`): **1** `rtw89_set_channel()` + **1** `rtw89_chip_rfk_channel()`.

---

## 5. Out-of-Tree Cross-Kernel Helper (`src/rtw89_mon.{c,h}` & `scripts/patch_rtw89.py`)

For older distribution kernels (`Linux 6.1`–`7.1`) where `wireless-next` patches have context offsets, `scripts/patch_rtw89.py` detects the driver API generation (`Gen 1`: `Linux 6.1`–`6.10` `struct rtw89_vif` + `rtwdev->mutex`; `Gen 2`: `Linux 6.11+` `struct rtw89_vif_link` + `wiphy_lock`) using a comment/string-aware C brace scanner and links `src/rtw89_mon.c` into `rtw89_core.ko` with identical `v4` semantics.
