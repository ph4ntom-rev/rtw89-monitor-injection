# Realtek RTL8852AE / rtw89 Monitor Mode & Packet Injection (Raw TX) Patch

[![Linux Kernel Upstream: Submitted (v2)](https://img.shields.io/badge/Linux%20Kernel%20Upstream-v2%20Submitted-blue.svg)](https://lore.kernel.org/linux-wireless/179371849554.20972.16232816164394690660@ph4ntom-rev.uz/)
[![Lore Archive: linux-wireless](https://img.shields.io/badge/lore.kernel.org-linux--wireless-success.svg)](https://lore.kernel.org/linux-wireless/179371849554.20972.16232816164394690660@ph4ntom-rev.uz/)
[![checkpatch.pl: strict 0/0/0](https://img.shields.io/badge/checkpatch.pl--strict-0%20errors%20%7C%200%20warnings-brightgreen.svg)](patches/v2/)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)
[![Linux Kernel](https://img.shields.io/badge/Linux-6.1%20--%207.x%2B-success.svg)](docs/ARCHITECTURE.md)
[![Kali Linux](https://img.shields.io/badge/Kali%20Linux-Verified-darkviolet.svg)](README.md#kali-linux--penetration-testing-quickstart)
[![Packet Injection](https://img.shields.io/badge/Packet%20Injection-IEEE80211__TX__CTL__INJECTED-brightgreen.svg)](docs/ARCHITECTURE.md)
[![Firmware Safety](https://img.shields.io/badge/Firmware-100%25%20Stock%20Signed-orange.svg)](README.md#firmware-regulatory--legal-compliance)

**[English](README.md) | [Русская документация](README_RU.md) | [Architecture Reference](docs/ARCHITECTURE.md)**

> [!IMPORTANT]
> ### 🚀 Official Linux Kernel Upstream Patchset (Series v2)
> This patch series has been audited, verified with `checkpatch.pl --strict` (**0 errors, 0 warnings, 0 checks**), and submitted to the official **Linux Wireless Mailing List (`linux-wireless@vger.kernel.org`)** and mainline maintainers:
> * **Patch 1/2**: `[PATCH v2 1/2] wifi: rtw89: fix RX filter configuration for monitor mode`  
>   🔗 [lore.kernel.org/linux-wireless (Message-ID: `<179371849554.20972.16232816164394690660@ph4ntom-rev.uz>`)](https://lore.kernel.org/linux-wireless/179371849554.20972.16232816164394690660@ph4ntom-rev.uz/)
> * **Patch 2/2**: `[PATCH v2 2/2] wifi: rtw89: support raw frame injection on standalone monitor interface`  
>   🔗 [lore.kernel.org/linux-wireless (Message-ID: `<179371849554.20972.4737532522625772373@ph4ntom-rev.uz>`)](https://lore.kernel.org/linux-wireless/179371849554.20972.4737532522625772373@ph4ntom-rev.uz/)
>
> **Quick Apply via Git (`git am`)**:
> ```bash
> # Apply official upstream patches directly to any Linux kernel tree:
> git am patches/v2/v2-0001-wifi-rtw89-fix-RX-filter-configuration-for-monito.patch
> git am patches/v2/v2-0002-wifi-rtw89-support-raw-frame-injection-on-standal.patch
> ```

> ### v3 monitor-mode series
> `patches/v3/` adds the updated RX filter and raw-injection series, plus channel restoration and rollback handling for monitor interface recreation. The series was submitted to `linux-wireless` on 2026-11-05; maintainer review is pending. See the [preflight validation summary](docs/PREFLIGHT_VALIDATION.md) and the [raw-socket reproducer](scripts/verify_hardware.py).

---

Host-side Linux `mac80211` driver extension and bugfix patchset for the mainline **`rtw89`** wireless driver (**Realtek RTL8852AE** PCIe `[10ec:8852]`, with architectural compatibility for **RTL8852BE** and **RTL8852CE**).

Enables full IEEE 802.11 **`NL80211_IFTYPE_MONITOR`** Radiotap packet capture for **Wireshark** / **`tcpdump`**, full **`airmon-ng`** / **`airodump-ng`** compatibility, and standards-compliant raw frame transmission / packet injection (**`IEEE80211_TX_CTL_INJECTED`** / **`aireplay-ng`**).

Works **100% on the host kernel driver side** using unmodified, cryptographically signed `rtw89` firmware images from the official distribution `linux-firmware` package. **No firmware reverse-patching or binary modification required.**

---

## Real-World Proof of Concept (Kali Linux Verification)

> [!TIP]
> **100% Functional in Kali Linux**: The screenshots below show actual execution on a live system running **Kali Linux (Kernel 7.1.5+kali-amd64)** with a **Realtek RTL8852AE PCIe Wireless Adapter (`[10ec:8852]`)**.

### 1. IEEE 802.11 Radiotap Capture in Wireshark
Live 802.11 packet capture showing full promiscuous reception of Management (Beacons, Probe Requests/Responses), Control (ACK, Block Ack, RTS, CTS), and Data frames across BSSIDs, with complete per-antenna RSSI (`-18 dBm`, `-24 dBm`) and channel frequency (2462 MHz / CH 11) metadata:

![Wireshark RTL8852AE Monitor Mode Capture](docs/screenshots/wireshark_monitor_capture.png)

### 2. High-Performance Packet Injection Benchmark (`aireplay-ng -9`)
Successful IEEE 802.11 diagnostic packet injection test showing **100% frame delivery** (30/30 probe requests acknowledged with sub-2ms ping times) against active APs, confirmed by `verify_monitor_diag.py` and `aireplay-ng`:

![RTL8852AE aireplay-ng Packet Injection Benchmark](docs/screenshots/aireplay_packet_injection.png)

---

## Why RTL8852AE Failed Monitor Mode & Injection Upstream

Thousands of Linux and Kali Linux users report that modern Wi-Fi 6 laptops with Realtek RTL8852AE adapters fail when running `airmon-ng` or attempting packet injection. Common upstream issues in `lwfinger/rtw89` and mainline Linux kernels:
* *"Does RTL8852AE support monitor mode or packet injection?"*
* *"Firmware blocks TX on monitor vif"*
* *"airodump-ng sees no beacons or control frames"*
* *"Kernel crash or NULL pointer dereference when injecting 802.11 frames"*

Through detailed hardware register reverse engineering and analysis of the `rtw89` firmware communication protocol, this project resolves the **four root causes** entirely in the host kernel driver:

1. **Hardware MAC RX Subtype & Address CAM Filtering (`mac80211.c`)**
   * *Problem*: Band 0 MAC registers drop 802.11 Control frames (ACK, RTS, CTS, BlockAck) and cross-BSS unicast Data frames that do not match the hardware `ADDR_CAM`.
   * *Fix (v2)*: Integrated directly into `rtw89_ops_configure_filter()`. When `IEEE80211_CONF_MONITOR` is active, clears `B_AX_A_UC_CAM_MATCH`, `B_AX_A_BC_CAM_MATCH`, `B_AX_A_MC_LIST_CAM_MATCH`, `B_AX_A_BCN_CHK_EN`, and `B_AX_UID_FILTER_MASK`, while asserting `B_AX_A_PWR_MGNT`. Automatically configures both MAC 0 and MAC 1 (when DBCC is enabled) and cleanly restores standard filter state upon exiting monitor mode.
2. **`NULL` `vif` Kernel Panic on Monitor TX & Safe RCU Teardown (`mac80211.c` & `core.c`)**
   * *Problem*: In `rtw89_ops_tx()`, the macro `vif_to_rtwvif(vif)` was evaluated unconditionally in local variable declarations before checking whether `info->control.vif` is `NULL` (which is standard behavior for `mac80211` Radiotap injection). Furthermore, tearing down monitor interfaces while injecting could trigger Use-After-Free conditions.
   * *Fix (v2)*: `pure_monitor_mode_vif` is protected with RCU (`__rcu` in `core.h`). `rtw89_ops_tx()` safely dereferences within an `rcu_read_lock()` block and resolves the active monitor VIF via official helper `rtwvif_to_vif()`. In `rtw89_ops_remove_interface()`, `rcu_assign_pointer(..., NULL)` and `synchronize_rcu()` are placed at the very top of the teardown path before releasing links, CAM entries, and MACID resources.
3. **Invalid Rate Index & Missing `TXWD_INFO` in `rtw89_core_tx_update_injection()` (`core.c`)**
   * *Problem*: When Radiotap headers omit an explicit bitrate, `mac80211` passes signed `s8 rates[0].idx = -1`. Upstream cast this to `u8` (`255`), writing an invalid hardware rate index, and omitted `desc_info->en_wd_info` on non-management frames.
   * *Fix (v2)*: Rate index type is declared as signed `s8 idx`. When `idx < 0`, it defaults to valid `RTW89_HW_RATE_OFDM6` (6.0 Mbps) with `use_rate = true` and `dis_data_fb = true`. Enables `en_wd_info = true` unconditionally on injected frames so `txwd_info` is properly generated into hardware descriptors, and bounds retries (`tx_cnt_lmt = 1`) when `IEEE80211_TX_CTL_NO_ACK` is set.
4. **Uninitialized Monitor `mac_id` Scheduler Context & 4-Bank Hardware Sleep Lock (`mac.c` & `mac80211.c`)**
   * *Problem*: Standalone monitor interfaces (`IEEE80211_HW_WANT_MONITOR_VIF`) allocate a `mac_id` and hardware port, but never configure CMAC/DMAC tables or register the link with the firmware scheduler. The firmware keeps the TX queue paused, and hardware MAC ID sleep registers freeze transmission.
   * *Fix (v2)*: `rtw89_ops_add_interface()` programs `h2c_default_cmac_tbl` / `h2c_default_dmac_tbl`, registers the role via `rtw89_fw_h2c_join_info()` using `RTW89_SELF_ROLE_AP` with `RTW89_NET_TYPE_NO_LINK`, and unpauses hardware queues via `rtw89_mac_unpause_macid()`, clearing all 4 MACID sleep banks (`R_AX_MACID_SLEEP_0..3`) on `RTW89_CHIP_AX`. In `rtw89_chip_rfk_channel_for_pure_mon_vif()`, `lockdep_assert_wiphy` and `rcu_dereference_protected()` completely eliminate sleeping-in-atomic context switch warnings during RF calibration.

---

## Kernel & Distro Compatibility

| Method | Target Environments | Status |
| :--- | :--- | :--- |
| **Upstream Kernel Series (`v2`)** | `Linux 6.11 \u2013 7.x+` (Mainline, Kali, Arch, Fedora) | ✅ Submitted to `linux-wireless` (lore.kernel.org) |
| **AST Source Integrator (`scripts/patch_rtw89.py`)** | `Linux 6.1 \u2013 7.x+` (API Gen 1 & Gen 2) | ✅ Idempotent patch & revert supported |
| **Out-of-tree Kbuild (`scripts/build_module.sh`)** | Kali Linux, Debian 12+, Ubuntu 22.04 / 24.04+ | ✅ Automatic header detection & `/lib/modules/.../updates` installer |

---

## Repository Structure

```text
rtw89-monitor-injection/
├── patches/
│   ├── v2/
│   │   ├── v2-0001-wifi-rtw89-fix-RX-filter-configuration-for-monito.patch  # Patch 1/2: Promiscuous RX CAM filtering
│   │   └── v2-0002-wifi-rtw89-support-raw-frame-injection-on-standal.patch  # Patch 2/2: RCU monitor TX, H2C & MACID unpause
│   └── linux-7.1-rtw89-monitor.diff                                         # Reference monolithic diff
├── src/
│   ├── rtw89_mon.h                  # Standalone extension declarations
│   └── rtw89_mon.c                  # Standalone MAC RX filter & TX fixups
├── scripts/
│   ├── patch_rtw89.py               # AST/brace-aware source integrator (--detect-only, --apply, --revert)
│   ├── build_module.sh              # Kbuild build, live reload, /lib/modules/.../updates installer
│   └── verify_monitor_diag.py       # IEEE 802.11 Radiotap RX & single Null-Data diagnostic verifier
├── tests/
│   └── test_patch_rtw89.py          # Automated Gen 1 / Gen 2 patch & revert unit tests
├── docs/
│   ├── ARCHITECTURE.md              # Register, H2C, RCU, and mac80211 flow documentation
│   └── screenshots/                 # Wireshark and aireplay verification captures
├── README.md                        # English documentation
└── README_RU.md                     # Russian documentation
```

---

## Quickstart: Applying Upstream Patches via `git am`

If you are compiling your own kernel or packaging for a distribution:

```bash
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd /path/to/linux-source-tree

# Apply both v2 patches cleanly:
git am /path/to/rtw89-monitor-injection/patches/v2/*.patch

# Verify clean application:
git log -n 2 --oneline
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

# Extract distribution rtw89 source code
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz \
  --strip-components=6 -C ~/rtw89_src \
  --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

# Compile and install into /lib/modules/$(uname -r)/updates/rtw89
chmod +x scripts/*.sh scripts/*.py
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

---

### Step 3: Monitor Mode with `airmon-ng` / `iw`

#### Method A: Using `airmon-ng` (Kali Linux / Aircrack-ng)
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan0

# Verify monitor interface
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

---

### Step 4: Packet Injection Verification (`aireplay-ng`)

```bash
sudo aireplay-ng -9 wlan0mon
```
Expected output:
```text
Trying broadcast probe requests...
Injection is working!
Ping (Ack) answered by AP ...
```

---

## Frequently Asked Questions (FAQ)

### Q: Why does stock `rtw89` drop Control frames and cross-BSS Data?
**A:** On Realtek 802.11ax hardware (`RTW89_CHIP_AX`), MAC hardware registers default to discarding frames whose destination does not match the device MAC or `ADDR_CAM`. Our patch adjusts `rx_fltr` dynamically in `rtw89_ops_configure_filter()` whenever `IEEE80211_CONF_MONITOR` is active.

### Q: Does this require modifying or flashing the firmware?
**A:** **No.** The official Realtek firmware image from `/lib/firmware/rtw89/` remains 100% untouched and cryptographically valid. All hooks and table initializations (`h2c_default_cmac_tbl`, `rtw89_fw_h2c_join_info`) occur through the standard host driver command queue.

### Q: Does this work on RTL8852BE / RTL8852CE?
**A:** Yes. RTL8852BE and RTL8852CE share the same `RTW89_CHIP_AX` core MAC architecture, H2C command layouts, and register maps in mainline `rtw89`.

---

## Firmware, Regulatory & Legal Compliance

* **Zero Firmware Modification or Redistribution**: This repository contains **no** firmware binaries (`*.bin`), microcode images, or proprietary vendor code. The driver relies exclusively on the unmodified, signed firmware provided by your Linux distribution (`linux-firmware`, `/lib/firmware/rtw89/`).
* **Regulatory Compliance (`cfg80211` Preserved)**: Country regulatory domains (`wireless-regdb`), disabled channel flags (`IEEE80211_CHAN_DISABLED`), `NO_IR` / Dynamic Frequency Selection (`IEEE80211_CHAN_RADAR` / DFS), SAR limits, and hardware TX power tables in `regd.c`, `sar.c`, and `phy.c` are **not modified** by this patchset. Users are responsible for operating wireless equipment in compliance with local spectrum regulations.
* **Intended Scope**: Designed strictly for IEEE 802.11 Wi-Fi 6 protocol debugging, academic research, Radiotap packet inspection in Wireshark, and wireless driver development.
* **Dual License**: Distributed under `GPL-2.0-only OR BSD-3-Clause` (see [LICENSE](LICENSE)), matching mainline `drivers/net/wireless/realtek/rtw89/`.
* **Trademark Attribution**: Realtek and RTL8852AE are trademarks or registered trademarks of Realtek Semiconductor Corp. This project is an independent community Linux kernel driver patchset and is not affiliated with, endorsed by, or sponsored by Realtek Semiconductor Corp.
