# Technical Architecture (`rtw89_mon`)

This document describes how `rtw89_mon` integrates with the mainline Linux `drivers/net/wireless/realtek/rtw89/` subsystem (`RTW89_CHIP_AX`) using standard public registers (`reg.h`), Host-to-Card (`H2C`) interfaces (`fw.h`), TX descriptor fields (`txrx.h`), and `mac80211` driver callbacks.

---

## 1. `mac80211` Capabilities & Interface Lifecycle

### Hardware Registration (`rtw89_mon_register_hw_caps`)
Invoked immediately prior to `ieee80211_register_hw()` in `core.c`:
* Advertises `BIT(NL80211_IFTYPE_MONITOR)` in `hw->wiphy->interface_modes`.
* Sets `IEEE80211_HW_WANT_MONITOR_VIF` so `mac80211` instantiates a concrete driver-level `struct ieee80211_vif` even when no station/AP VIF exists, allowing `rtw89_ops_add_interface()` to allocate a hardware `port` and `mac_id`.
* Sets `IEEE80211_HW_SIGNAL_DBM` for Radiotap RSSI reporting.

### Monitor VIF Initialization (`rtw89_mon_add_vif_post`)
When `vif->type == NL80211_IFTYPE_MONITOR`:
1. Calls `rtw89_leave_ps_mode(rtwdev)` so RF/BB blocks remain powered while monitoring.
2. Populates default per-MACID CMAC and DMAC control tables via `chip->ops->h2c_default_cmac_tbl()` and `chip->ops->h2c_default_dmac_tbl()`.
3. Calls `rtw89_fw_h2c_join_info(..., dis_conn=false)` with `self_role = RTW89_SELF_ROLE_AP` and `net_type = RTW89_NET_TYPE_NO_LINK`. This initializes the per-MACID scheduling context without enabling client-mode (`RTW89_SELF_ROLE_CLIENT`) beacon-timeout tracking or transmitting AP beacons.
4. Calls `rtw89_fw_h2c_macid_pause(rtwdev, mac_id & 0x1F, mac_id >> 5, false)` to ensure the `mac_id` is unpaused in the MAC scheduler.

### Monitor VIF Teardown (`rtw89_mon_remove_vif_pre`)
Before `rtw89_ops_remove_interface()` releases the `mac_id` and hardware port, `rtw89_mon_remove_vif_pre()` sends `rtw89_fw_h2c_join_info(..., dis_conn=true)` to cleanly release the MACID context.

---

## 2. Band 0 MAC RX Filter Programming (`reg.h`)

`rtw89_mon_program_mac_rx_tx_regs()` configures the following `RTW89_CHIP_AX` Band 0 MAC registers whenever a monitor VIF is added, `rtw89_ops_configure_filter()` runs, or `rtw89_set_channel()` completes on `RTW89_MAC_0`:

| Register | Offset | Monitor Configuration | Purpose |
| :--- | :--- | :--- | :--- |
| `R_AX_PKT_CTRL_B0_PAUSE` | `0xCE06` | Clear `BIT(0)` | Unpauses Band 0 RX packet filter after scan/channel operations |
| `R_AX_RX_FLTR_OPT` | `0xCE20` | Clear `B_AX_A_UC_CAM_MATCH`, `B_AX_A_BC_CAM_MATCH`, `B_AX_A_MC_LIST_CAM_MATCH`, `B_AX_A_BCN_CHK_EN`, `B_AX_UID_FILTER_MASK`; sync `B_AX_A_CRC32_ERR` with `FIF_FCSFAIL` | Disables hardware `ADDR_CAM` / BSSID drops so cross-BSS frames reach host RX rings |
| `R_AX_CTRL_FLTR` | `0xCE24` | `0x55555555` (`0b01` × 16) | Routes all 16 IEEE 802.11 Control subtypes (RTS, CTS, ACK, BAR, BA, Trigger) to host DMA |
| `R_AX_MGNT_FLTR` | `0xCE28` | `0x55555555` (`0b01` × 16) | Routes all 16 IEEE 802.11 Management subtypes to host DMA |
| `R_AX_DATA_FLTR` | `0xCE2C` | `0x55555555` (`0b01` × 16) | Routes all 16 IEEE 802.11 Data subtypes to host DMA |
| `R_AX_MACID_SLEEP_0` | `0xC2C0` | Clear `BIT(mac_id)` | Ensures monitor `mac_id` is not marked asleep |
| `R_AX_CTN_TXEN` | `0xC348` | Set `0x0000FFFF` | Keeps Band 0 continuous TX AC/management queues enabled |

> **Hook placement note (`rtw89_set_channel` & `rtw89_ops_configure_filter`)**:
> On `RTW89_CHIP_AX` single-band operation (`!rtwdev->dbcc_en`), both `rtw89_set_channel()` and `rtw89_ops_configure_filter()` return early right after programming `RTW89_MAC_0`. Hooks are therefore placed immediately after the `RTW89_MAC_0` call prior to the early return.

---

## 3. Monitor & Diagnostic TX Path (`mac80211.c`, `core.c`, `txrx.h`)

### `NULL` `vif` Resolution (`rtw89_mon_resolve_tx_vif`)
When `mac80211` transmits a Radiotap frame through a monitor interface (`ieee80211_monitor_start_xmit`), `info->control.vif` can be `NULL`. Both `rtw89_ops_tx()` and `rtw89_core_tx_write()` previously dereferenced `vif_to_rtwvif(vif)` unconditionally in their declaration blocks. `rtw89_mon_resolve_tx_vif()` resolves `NULL` to `rtwdev->pure_monitor_mode_vif` (Gen 2) or the tracked `ctx->mon_vif` before `vif_to_rtwvif()` is called.

### TX Descriptor Fixup (`rtw89_mon_fixup_tx_desc`)
Called at the end of `rtw89_core_tx_update_desc_info()` for monitor / `IEEE80211_TX_CTL_INJECTED` frames (strictly skipping `RTW89_CORE_TX_TYPE_FWCMD` H2C packets where `tx_req->vif` is legitimately `NULL`):
* **Explicit Rate Selection**: Sets `en_wd_info = true`, `use_rate = true`, and `dis_data_fb = true` because unassociated monitor frames have no peer Rate Adaptive (`RA`) entry. Maps positive `info->control.rates[0].idx` values to `RTW89_HW_RATE_*` (CCK, OFDM, HT MCS, VHT MCS) and defaults `rate->idx < 0` to `RTW89_HW_RATE_OFDM6` (`6.0 Mbps`).
* **Queue & DMA Channel Mapping**: Routes Management and Null-Data frames via `RTW89_TX_QSEL_B0_MGMT` and other frames via `RTW89_TX_QSEL_BE_0`, resolving `desc_info->ch_dma` through `rtw89_chip_get_ch_dma()` (Gen 2) or `rtw89_core_get_ch_dma()` (Gen 1).
* **`NO_ACK` Retry Bounding**: Sets `is_bmc = true`, `tx_cnt_lmt_en = true`, and `tx_cnt_lmt = 1` when `IEEE80211_TX_CTL_NO_ACK` is set, on Control frames, or on group-addressed destinations so hardware queues never stall awaiting an 802.11 ACK.
* **Sequence Number Preservation**: Clears `upd_wlan_hdr` unless `IEEE80211_TX_CTL_ASSIGN_SEQ` is requested by `mac80211`.
