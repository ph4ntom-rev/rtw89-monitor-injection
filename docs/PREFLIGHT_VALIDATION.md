# RTL8852AE Validation Report (`PATCH v4` & Upstream Submission Status)

## 1. Upstream Submission Status

* **`PATCH v3` (Submitted to `linux-wireless`)**:
  Submitted on `2026-11-05` (`Lore #290240–#290243`), awaiting maintainer review before sending `PATCH v4` `--in-reply-to`:
  * Cover letter: `<20261104203051.564-1-owner@ph4ntom-rev.uz>`
  * Patch 1/3: `<20261104203051.564-2-owner@ph4ntom-rev.uz>`
  * Patch 2/3: `<20261104203051.564-3-owner@ph4ntom-rev.uz>`
  * Patch 3/3: `<20261104203051.564-4-owner@ph4ntom-rev.uz>`
* **`PATCH v4` (`patches/v4/`, Current Verified Series)**:
  Four-patch series generated against upstream `torvalds/linux` base `d48723711bae9b8cdc56719c448ba42fef28cbee` (`Enver Ikromov <owner@ph4ntom-rev.uz>`):
  1. `v4-0001-wifi-rtw89-fix-RX-filter-configuration-for-monito.patch`
  2. `v4-0002-wifi-rtw89-protect-pure_monitor_mode_vif-with-RCU.patch`
  3. `v4-0003-wifi-rtw89-support-raw-frame-injection-on-standal.patch`
  4. `v4-0004-wifi-rtw89-restore-channel-after-monitor-restart-.patch`

### Key Improvements in `PATCH v4` over `v3`
1. Splits the `pure_monitor_mode_vif` `__rcu` conversion into a dedicated preparatory patch (`2/4`) and adds `rtw89_recalc_pure_monitor_vif()` across interface addition and removal (so bringing down a second non-monitor interface promotes the remaining monitor VIF).
2. Gates both `!vif` and `vif->type == NL80211_IFTYPE_MONITOR` through `rcu_dereference(rtwdev->pure_monitor_mode_vif)` in `rtw89_ops_tx()` so concurrent TX cannot access a monitor `rtwvif` while `ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` prior to clearing `local->monitor_sdata`.
3. Removes redundant `rtw89_fw_h2c_join_info()` calls from `mac80211.c`: once `NL80211_IFTYPE_MONITOR` maps to `RTW89_NET_TYPE_NO_LINK` and `RTW89_SELF_ROLE_AP` in `rtw89_vif_type_mapping()`, `rtw89_mac_vif_init()` and `rtw89_mac_vif_deinit()` handle the complete firmware role lifecycle.
4. Guards `hdr->seq_ctrl` reads on short (`10`–`16` byte) 802.11 Control frames, skips `rtw89_traffic_stats_accu()`, `rtw89_wow_parse_akm()`, `rtw89_core_tx_btc_spec_pkt_notify()`, `rtw89_core_tx_update_he_qos_htc()`, and `rtw89_core_tx_update_ampdu_info()` on `IEEE80211_TX_CTL_INJECTED` frames, clears `upd_wlan_hdr = false`, `hw_ssn_sel = 0`, and `hw_seq_mode = 0` when `IEEE80211_TX_CTL_ASSIGN_SEQ` is unset so injected sequence numbers are preserved, and handles negative default rate indices (`s8 idx < 0`).
5. Moves non-chanctx monitor restart channel restoration from `rtw89_ops_add_interface()` into `rtw89_ops_config()` (`IEEE80211_CONF_CHANGE_MONITOR && !rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`), eliminating duplicate ~105 ms RF calibration (`rfk_channel`) on cold start and dropping the inaccurate `Fixes: d9204e99b696` tag.

---

## 2. Live Hardware & Static Verification (`PATCH v4`)

* **Hardware**: Realtek `RTL8852AE` PCIe (`[10ec:8852]`)
* **Stock Firmware**: `rtw89/rtw8852a_fw.bin` version `0.13.36.2` (unmodified `linux-firmware`)
* **Kernel**: `Linux 7.1.5+kali-amd64`

| Check | Result |
| :--- | :--- |
| `scripts/checkpatch.pl --strict --no-tree` (`v4 1/4..4/4`) | **PASS** — `0 errors, 0 warnings, 0 checks` on all 4 patches |
| Kbuild `W=1` module build (`PATCH v4` & `patch_rtw89.py`) | **PASS** — `0` compiler warnings |
| Promiscuous RX (`8s` on Channel 5 / `2432 MHz`) | **PASS** — `21,705` frames (`127` Beacons, `2,413` BlockAck, `1,390` RTS, `3,147` CTS, `2,797` ACK, `11,282` QoS Data, `211` NullData) |
| Baseline `aireplay-ng -9` injection | **PASS** — `10/10` (`100%`, avg `12.34 ms`) |
| Edge-case raw TX (`10B ACK`, `16B RTS`, `24B AssocReq`, `SN=1337`, `idx=-1`) | **PASS** — all transmitted cleanly; zero kernel warnings or OOB reads |
| Concurrent raw TX + `25` `iw dev mon0 del/add` cycles | **PASS** — `13,579` mixed `NullFunc` / `10B ACK` / `16B RTS` frames sent concurrently during teardown/recreate |
| Post-stress RX **without** `iw set channel` | **PASS** — `5,248` frames captured immediately after `ip link set mon0 up` |
| Post-stress `aireplay-ng -9` injection | **PASS** — `30/30` (`100%`, avg `15.56 ms`) |
| `ftrace` (`rtw89_set_channel` / `rtw89_chip_rfk_channel`) | **PASS** — exactly `1` channel set + `1` RFK on cold start, `down/up`, and `del/add/up` |
| Kernel health (`dmesg` warnings/oops/lockdep) & managed STA reconnect | **PASS** — `0` new kernel alerts; `wlan0` re-associated cleanly |

---

## 3. Reproducer (`scripts/verify_hardware.py`)

Run on an isolated Linux test host with root privileges:

```bash
sudo python3 scripts/verify_hardware.py \
  --series v4 \
  --channel 5 \
  --duration 8 \
  --cycles 25 \
  --settle 1.5 \
  --output /tmp/rtw89-v4-validation
```

The validator brings down managed interfaces cleanly (checking `IFF_UP` in `/sys/class/net/<iface>/flags`), verifies promiscuous Control/Management/Data RX, runs `aireplay-ng -9`, stresses concurrent raw `AF_PACKET` TX (`NullFunc` + `10B ACK` + `16B RTS`) across 25 `del/add` cycles, verifies channel restoration on `ip link set mon0 up` without issuing `iw set channel`, and restores the original managed interface state on exit.

---

## 4. Scope & Limitations

* **Standalone Monitor Injection**: Raw frame injection requires a standalone monitor interface (`pure_monitor_mode_vif != NULL`, i.e., bring `wlan0` down before injecting on `mon0`, or set `wlan0` itself to `type monitor`). When a station or AP VIF is active concurrently, raw monitor TX frames are safely dropped.
* **Tested Hardware**: Runtime validation was performed on `RTL8852AE` (`[10ec:8852]`). Other `rtw89` chips (`RTL8852BE`, `RTL8852CE`, `RTL8922AE`) and DBCC (`RTW89_MAC_1`) setups share the same driver paths but were not runtime-tested on physical hardware here.
