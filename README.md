# Realtek RTL8852AE / rtw89 Monitor Mode & Packet Injection (`PATCH v4`)

[![Series PATCH v4](https://img.shields.io/badge/Series-PATCH%20v4-blue.svg)](patches/v4/)
[![Linux Kernel Upstream: v3 Submitted / v4 Ready](https://img.shields.io/badge/linux--wireless-v3%20Submitted%20%7C%20v4%20Ready-blue.svg)](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/)
[![checkpatch.pl: strict 0/0/0](https://img.shields.io/badge/checkpatch.pl--strict-0%20errors%20%7C%200%20warnings-brightgreen.svg)](patches/v4/)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)
[![Linux Kernel](https://img.shields.io/badge/Linux-6.1%20--%207.x%2B-success.svg)](docs/ARCHITECTURE.md)
[![Kali Linux](https://img.shields.io/badge/Kali%20Linux-Verified-darkviolet.svg)](README.md#quickstart-building-as-override-module-on-kali-linux--ubuntu)
[![Packet Injection](https://img.shields.io/badge/Packet%20Injection-IEEE80211__TX__CTL__INJECTED-brightgreen.svg)](docs/ARCHITECTURE.md)
[![Firmware Safety](https://img.shields.io/badge/Firmware-100%25%20Stock%20Signed-orange.svg)](README.md#firmware-regulatory--legal-compliance)

**[English](README.md) | [Русская документация](README_RU.md) | [Architecture Reference](docs/ARCHITECTURE.md) | [Validation Report](docs/PREFLIGHT_VALIDATION.md)**

> [!IMPORTANT]
> ### `PATCH v4` — Monitor RX Filtering, RCU `pure_monitor_mode_vif`, Raw Injection & Channel Restoration
> * **Current verified series**: [`patches/v4/`](patches/v4/) (four patches against upstream `torvalds/linux` commit `d48723711bae9b8cdc56719c448ba42fef28cbee`, `checkpatch.pl --strict`: `0/0/0`, `W=1`: `0` warnings).
> * **Mailing list status**: [`PATCH v3`](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) (`Lore #290240–#290243`) was submitted to `linux-wireless@vger.kernel.org` on `2026-11-05` and is awaiting maintainer review before `PATCH v4` is sent `--in-reply-to`.
>
> **Four-patch breakdown (`patches/v4/`):**
> 1. **`v4 1/4`**: `wifi: rtw89: fix RX filter configuration for monitor mode`
> 2. **`v4 2/4`**: `wifi: rtw89: protect pure_monitor_mode_vif with RCU`
> 3. **`v4 3/4`**: `wifi: rtw89: support raw frame injection on standalone monitor interface`
> 4. **`v4 4/4`**: `wifi: rtw89: restore channel after monitor restart on non-chanctx chips`
>
> **Key improvements in `v4` over `v3`:**
> * Splits `pure_monitor_mode_vif` `__rcu` conversion into a dedicated preparatory patch (`2/4`) and introduces `rtw89_recalc_pure_monitor_vif()` so `pure_monitor_mode_vif` is maintained across both interface addition and removal (including when a second non-monitor interface is brought down).
> * Gates both `!vif` and `vif->type == NL80211_IFTYPE_MONITOR` through `rcu_dereference(rtwdev->pure_monitor_mode_vif)` in `rtw89_ops_tx()` so concurrent TX cannot access a monitor `rtwvif` while `ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` prior to clearing `local->monitor_sdata`.
> * Removes redundant `rtw89_fw_h2c_join_info()` calls from `mac80211.c`: setting `RTW89_NET_TYPE_NO_LINK` and `RTW89_SELF_ROLE_AP` in `rtw89_vif_type_mapping()` allows `rtw89_mac_vif_init()` / `rtw89_mac_vif_deinit()` to manage the firmware `CCTL` role lifecycle cleanly.
> * Protects short (`10`–`16` byte) 802.11 Control frames (`ACK`, `RTS`) against `hdr->seq_ctrl` reads, skips `rtw89_traffic_stats_accu()`, `rtw89_wow_parse_akm()`, `rtw89_core_tx_btc_spec_pkt_notify()`, `rtw89_core_tx_update_he_qos_htc()`, and `rtw89_core_tx_update_ampdu_info()` on injected frames, preserves user-supplied sequence numbers (`upd_wlan_hdr = false`, `hw_ssn_sel = 0`, `hw_seq_mode = 0`) when `IEEE80211_TX_CTL_ASSIGN_SEQ` is unset, and fixes signed default rate fallback (`s8 idx < 0`).
> * Moves non-chanctx monitor restart channel restoration from `rtw89_ops_add_interface()` into `rtw89_ops_config()` (`IEEE80211_CONF_CHANGE_MONITOR && !rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`), eliminating duplicate ~105 ms RF calibration on cold start.

---

Host-side Linux `mac80211` driver bugfix and feature series for the mainline **`rtw89`** wireless subsystem. **Runtime validation was performed on Realtek RTL8852AE `[10ec:8852]` (`fw 0.13.36.2`, Linux `7.1.5+kali-amd64`); other `rtw89` chips and DBCC/MAC 1 setups were not runtime-tested here.**

Enables promiscuous IEEE 802.11 **`NL80211_IFTYPE_MONITOR`** Radiotap packet capture for **Wireshark** / **`tcpdump`**, `airmon-ng` / `airodump-ng` compatibility, and **standalone** monitor raw frame injection (**`IEEE80211_TX_CTL_INJECTED`** / **`aireplay-ng`**).

Uses unmodified stock firmware images from the official `linux-firmware` package. **No firmware binary patching or redistribution is involved.**

---

## Live Hardware Validation on RTL8852AE (`PATCH v4`, Kali Linux `7.1.5`)

| Check | Result |
| :--- | :--- |
| `checkpatch.pl --strict` (`v4 1/4..4/4`) | `0 errors, 0 warnings, 0 checks` |
| Kbuild `W=1` build (`PATCH v4` & `patch_rtw89.py`) | `0` compiler warnings |
| Promiscuous RX (`8s`, CH 5 / `2432 MHz`) | `21,705` frames (`127` Beacons, `2,413` BlockAck, `1,390` RTS, `3,147` CTS, `2,797` ACK, `11,282` QoS Data) |
| Baseline `aireplay-ng -9` injection | `10/10` (`100%`, avg `12.34 ms`) |
| Edge-case raw TX (`10B ACK`, `16B RTS`, `24B AssocReq`, `SN=1337`, `idx=-1`) | All transmitted cleanly; `0` kernel warnings |
| `25` `iw dev mon0 del/add` cycles under concurrent raw TX | `13,579` mixed `NullFunc` / `10B ACK` / `16B RTS` frames sent concurrently |
| Post-stress RX **without** `iw set channel` | `5,248` frames captured immediately on `ip link set mon0 up` |
| Post-stress `aireplay-ng -9` injection | `30/30` (`100%`, avg `15.56 ms`) |
| `ftrace` (`rtw89_set_channel` / `rtw89_chip_rfk_channel`) | Single channel set + single RFK on cold start, `down/up`, and `del/add/up` |
| Kernel health (`dmesg`) & managed STA restore | `0` alerts; `wlan0` re-associated cleanly |

### 1. IEEE 802.11 Radiotap Capture in Wireshark
Live 802.11 packet capture showing promiscuous reception of Management (Beacons, Probe Requests/Responses), Control (ACK, BlockAck, RTS, CTS), and cross-BSS Data frames with per-antenna RSSI and channel frequency metadata:

![Wireshark RTL8852AE Monitor Mode Capture](docs/screenshots/wireshark_monitor_capture.png)

### 2. Packet Injection Benchmark (`aireplay-ng -9`)
IEEE 802.11 packet injection test confirming **100% frame delivery** (`30/30` probe requests acknowledged):

![RTL8852AE aireplay-ng Packet Injection Benchmark](docs/screenshots/aireplay_packet_injection.png)

---

## Root Causes Fixed in `PATCH v4`

1. **Hardware MAC RX Address CAM & BSSID Filtering (`v4 1/4`, `mac80211.c`)**
   * *Problem*: `R_AX_RX_FLTR_OPT` defaults drop frames failing multicast, `ADDR_CAM`, beacon BSSID, or UID checks, and `rtw89_ops_configure_filter()` strips `FIF_CONTROL` and `FIF_PSPOLL` from `*new_flags`.
   * *Fix*: Retains `FIF_CONTROL | FIF_PSPOLL` in `*new_flags` and derives a local `rx_fltr` copy clearing `B_AX_A_MC`, `B_AX_A_UC_CAM_MATCH`, `B_AX_A_BC_CAM_MATCH`, `B_AX_A_MC_LIST_CAM_MATCH`, `B_AX_A_BCN_CHK_EN`, and `B_AX_UID_FILTER_MASK` when `IEEE80211_CONF_MONITOR` is active, preserving `rtwdev->hal.rx_fltr` so normal filtering resumes when exiting monitor mode.
2. **Lockless `pure_monitor_mode_vif` Races During Monitor Teardown (`v4 2/4`, `core.h`, `core.c`, `mac80211.c`)**
   * *Problem*: `rtw89_ops_tx()` runs outside `wiphy_lock` while interface add/remove mutates `pure_monitor_mode_vif`.
   * *Fix*: Annotates `pure_monitor_mode_vif` with `__rcu`, introduces `rtw89_recalc_pure_monitor_vif()` across interface add and remove, updates readers to `rcu_access_pointer()` / `rcu_dereference_protected()`, and clears via `RCU_INIT_POINTER(..., NULL)` + `synchronize_rcu()` prior to tearing down links and CAM/MACID state.
3. **`NULL` / Monitor `control.vif` Races, Firmware `CCTL` Role Drop & Injection Descriptor Bugs (`v4 3/4`, `core.c`, `mac80211.c`)**
   * *Problem*:
     1. With `IEEE80211_HW_WANT_MONITOR_VIF`, `mac80211` passes `info->control.vif == NULL` when `local->monitor_sdata` is absent (causing `vif_to_rtwvif(vif)` to dereference a negative offset from `NULL`), and `ieee80211_del_virtual_monitor()` calls `drv_remove_interface()` before clearing `local->monitor_sdata`.
     2. `rtw89_vif_type_mapping()` left `NL80211_IFTYPE_MONITOR` as `RTW89_SELF_ROLE_CLIENT` (`0`), causing the 32-bit `MIPS32`/`MIPS16e` MCU firmware's `H2C_FUNC_MAC_JOININFO` handler during `rtw89_mac_vif_init()` to treat `RTW89_NET_TYPE_NO_LINK` as a client disconnect and silently drop TX descriptors for that `mac_id`.
     3. Short `10`–`16` byte Control frames lacked `seq_ctrl` at offset `22`, `rtw89_traffic_stats_accu()` and `rtw89_core_tx_btc_spec_pkt_notify()` misread the 802.11 MAC header via `ip_hdr(skb)`/`udp_hdr(skb)`, `rtw89_wow_parse_akm()` inspected injected Association Request frames, `u8 idx` turned default `-1` rate indices into `0xff`, and `upd_wlan_hdr`/`hw_seq_mode` overwrote injected sequence numbers.
   * *Fix*: Resolves `!vif || vif->type == NL80211_IFTYPE_MONITOR` via `rcu_dereference(rtwdev->pure_monitor_mode_vif)` under `rcu_read_lock()`, validates `skb->len >= ieee80211_hdrlen(fc)`, and populates `info->control.vif`; maps `NL80211_IFTYPE_MONITOR` to `RTW89_NET_TYPE_NO_LINK` + `RTW89_SELF_ROLE_AP`; routes non-Data frames to `RTW89_CORE_TX_TYPE_MGMT`; guards `< 24B` Control headers and skips `traffic_stats_accu`, `wow_parse_akm`, `btc_spec_pkt_notify`, `update_he_qos_htc`, and `update_ampdu_info` on injected frames; uses signed `s8 idx` with `data_retry_lowest_rate` / `OFDM6` (`IEEE80211_TX_CTL_NO_CCK_RATE`) fallback; clears `upd_wlan_hdr = false`, `hw_ssn_sel = 0`, and `hw_seq_mode = 0` unless `IEEE80211_TX_CTL_ASSIGN_SEQ` is set; and bounds `NO_ACK` retries (`tx_cnt_lmt = 1`).
4. **Channel Loss After Monitor Stop/Restart on Non-Chanctx Chips (`v4 4/4`, `mac80211.c`)**
   * *Problem*: Removing or bringing down the monitor VIF calls `rtw89_ops_stop()`, clearing `RTW89_PHY_0` entity state. Bringing a monitor VIF back up on the same `chandef` toggles `IEEE80211_CONF_CHANGE_MONITOR` without `IEEE80211_CONF_CHANGE_CHANNEL`, leaving RF/BB unconfigured.
   * *Fix*: Restores `hw->conf.chandef` and calls `rtw89_set_channel()` in `rtw89_ops_config()` when `IEEE80211_CONF_CHANGE_MONITOR` activates monitor mode while `!rtwdev->chip->support_chanctx_num` and `!rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`.

> [!NOTE]
> **Standalone Monitor Requirement for Injection**: Raw packet injection requires a **standalone** monitor interface (bring `wlan0` down before injecting on `mon0`, or switch `wlan0` itself to `type monitor`). When a managed station or AP VIF is active concurrently, `pure_monitor_mode_vif` is `NULL` and raw monitor TX frames are safely dropped without crashing.

---

## Kernel & Distro Compatibility

| Method | Target Environments | Status |
| :--- | :--- | :--- |
| **Upstream Kernel Series (`patches/v4/`)** | Mainline / `wireless-next` (`Linux 7.2+`) | ✅ Verified (`checkpatch.pl --strict` `0/0/0`, `W=1` `0` warnings) |
| **Submitted Mailing List Series (`patches/v3/`)** | `linux-wireless` (`Lore #290240–#290243`) | ⏳ Submitted `2026-11-05`; awaiting maintainer review |
| **Brace-Aware C Source Integrator (`scripts/patch_rtw89.py`)** | `Linux 6.1 – 7.x+` (API Gen 1 & Gen 2) | ✅ Idempotent `--apply` & `--revert` verified |
| **Out-of-Tree Kbuild / DKMS (`scripts/build_module.sh`, `dkms.conf`)** | Kali Linux, Debian 12+, Ubuntu 22.04 / 24.04+ | ✅ Auto-detects loaded `rtw89_*` modules & installs into `updates/rtw89` |

---

## Repository Structure

```text
rtw89-monitor-injection/
├── patches/
│   ├── v2/                               # Historical v2 series
│   ├── v3/                               # Submitted v3 series (Lore #290240-#290243)
│   └── v4/                               # Current verified 4-patch series + cover letter
├── src/
│   ├── rtw89_mon.h                       # Cross-kernel helper declarations (Linux 6.1 - 7.x+)
│   └── rtw89_mon.c                       # Cross-kernel helper matching PATCH v4 semantics
├── scripts/
│   ├── patch_rtw89.py                    # Comment/string-aware C source integrator
│   ├── build_module.sh                   # Kbuild compiler and /lib/modules/.../updates installer
│   ├── verify_monitor_diag.py            # Single-frame Radiotap RX/TX diagnostic tool
│   └── verify_hardware.py                # End-to-end AF_PACKET stress, recreate & PCAP validator
├── tests/
│   └── test_patch_rtw89.py               # Gen 1 / Gen 2 integrator & v4 hygiene unit tests
├── docs/
│   ├── ARCHITECTURE.md                   # Register, H2C, RCU, TX descriptor & config flow
│   ├── PREFLIGHT_VALIDATION.md           # Hardware, ftrace & static validation report
│   ├── README.ru.md                      # Concise Russian overview
│   └── screenshots/                      # Anonymized Wireshark and aireplay-ng captures
├── dkms.conf                             # DKMS configuration for kernel upgrades
├── LICENSE                               # GPL-2.0-only text (dual-licensed with BSD-3-Clause)
├── LICENSES/                             # Full GPL-2.0-only and BSD-3-Clause texts
├── README.md                             # English documentation
└── README_RU.md                          # Full Russian documentation
```

---

## Quickstart: Applying `PATCH v4` via `git am`

Apply patches `0001` through `0004` (`v4-0000-cover-letter.patch` is the mailing-list cover letter and is not passed to `git am`):

```bash
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd /path/to/linux-source-tree

git am /path/to/rtw89-monitor-injection/patches/v4/v4-000[1-4]-*.patch

# Verify clean application:
git log -n 4 --oneline
```

---

## Quickstart: Building as Override Module on Kali Linux / Ubuntu

### Step 1: Install Kernel Headers and Build Tools
```bash
sudo apt update
sudo apt install -y linux-headers-$(uname -r) linux-source build-essential python3
```

### Step 2: Build and Install the Patched Module
```bash
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd rtw89-monitor-injection

# Extract matching distribution rtw89 source code
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz \
  --strip-components=6 -C ~/rtw89_src \
  --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

# Compile and install into /lib/modules/$(uname -r)/updates/rtw89
chmod +x scripts/*.sh scripts/*.py
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

### Step 3: Standalone Monitor Mode (`airmon-ng` or `iw`)

#### Method A: Using `airmon-ng`
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan0
sudo airodump-ng wlan0mon
```

#### Method B: Native `iw` and `tcpdump` / `Wireshark`
```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412

# Capture full 802.11 frames with Radiotap headers:
sudo tcpdump -i wlan0 -I -n -e -c 30
```

### Step 4: Packet Injection Verification (`aireplay-ng`)
```bash
sudo aireplay-ng -9 wlan0mon
```

### Uninstalling Override Modules
```bash
./scripts/build_module.sh --uninstall
python3 scripts/patch_rtw89.py --rtw89-dir ~/rtw89_src --revert
```

---

## Firmware, Regulatory & Legal Compliance

* **Zero Firmware Modification or Redistribution**: This repository contains **no** firmware binaries (`*.bin`), microcode images, or proprietary vendor code. Runtime operation uses the unmodified, cryptographically signed firmware from `linux-firmware` (`/lib/firmware/rtw89/`).
* **Regulatory Compliance (`cfg80211` Preserved)**: Country regulatory domains (`wireless-regdb`), disabled channel flags (`IEEE80211_CHAN_DISABLED`), `NO_IR` / Dynamic Frequency Selection (`IEEE80211_CHAN_RADAR` / DFS), SAR limits, and hardware TX power tables in `regd.c`, `sar.c`, and `phy.c` are **not modified** by this patchset. Users are responsible for operating wireless equipment in compliance with applicable spectrum regulations.
* **Dual License**: Original code and scripts are dual-licensed under `GPL-2.0-only OR BSD-3-Clause` (see [LICENSE](LICENSE), [LICENSES/GPL-2.0-only.txt](LICENSES/GPL-2.0-only.txt), and [LICENSES/BSD-3-Clause.txt](LICENSES/BSD-3-Clause.txt)), matching mainline `drivers/net/wireless/realtek/rtw89/`.
* **Trademark Attribution**: Realtek and RTL8852AE are trademarks or registered trademarks of Realtek Semiconductor Corp. This project is an independent community Linux kernel driver patchset and is not affiliated with, endorsed by, or sponsored by Realtek Semiconductor Corp.
