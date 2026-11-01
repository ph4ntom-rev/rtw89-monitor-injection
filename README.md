# Realtek RTL8852AE / rtw89 Monitor Mode & Packet Injection (Raw TX) Patch

[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)
[![Linux Kernel](https://img.shields.io/badge/Linux-6.1%20--%207.x%2B-success.svg)](docs/ARCHITECTURE.md)
[![Kali Linux](https://img.shields.io/badge/Kali%20Linux-Verified-darkviolet.svg)](README.md#kali-linux--penetration-testing-quickstart)
[![Packet Injection](https://img.shields.io/badge/Packet%20Injection-IEEE80211__TX__CTL__INJECTED-brightgreen.svg)](docs/ARCHITECTURE.md)
[![Firmware Safety](https://img.shields.io/badge/Firmware-100%25%20Stock%20Signed-orange.svg)](README.md#firmware-regulatory--legal-compliance)

**[English](README.md) | [Русская документация](README_RU.md) | [Architecture Reference](docs/ARCHITECTURE.md)**

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

Through detailed hardware register reverse engineering and analysis of the `rtw89` RISC-V firmware communication protocol, this project resolves the **four root causes** entirely in the host kernel driver:

1. **Hardware MAC RX Subtype & Address CAM Filtering (`reg.h` / `mac.c`)**
   * *Problem*: Band 0 MAC registers drop 802.11 Control frames (ACK, RTS, CTS, BlockAck) and cross-BSS unicast Data frames that do not match the hardware `ADDR_CAM`. Hardware scan offload can also leave the Band 0 RX packet filter paused (`R_AX_PKT_CTRL_B0_PAUSE`).
   * *Fix*: `rtw89_mon_program_mac_rx_tx_regs()` reconfigures `R_AX_RX_FLTR_OPT` (`0xCE20`), `R_AX_CTRL_FLTR` (`0xCE24`), `R_AX_MGNT_FLTR` (`0xCE28`), and `R_AX_DATA_FLTR` (`0xCE2C`) with promiscuous mask `0x55555555`, honors `FIF_FCSFAIL`, and automatically re-asserts filter state across channel switches (`rtw89_set_channel()`).
2. **`NULL` `vif` Kernel Panic on Monitor TX (`mac80211.c` & `core.c`)**
   * *Problem*: In `rtw89_ops_tx()` and `rtw89_core_tx_write()`, the macro `vif_to_rtwvif(vif)` was evaluated unconditionally in local variable declarations before checking whether `info->control.vif` is `NULL` (which is standard behavior for `mac80211` Radiotap injection).
   * *Fix*: `rtw89_mon_resolve_tx_vif()` resolves the active monitor VIF before dereference, preventing kernel crashes.
3. **Invalid Rate Index & Missing `TXWD_INFO` in `rtw89_core_tx_update_injection()` (`core.c`)**
   * *Problem*: When Radiotap headers omit an explicit bitrate, `mac80211` passes signed `s8 rates[0].idx = -1`. Upstream cast this to `u8` (`255`), writing an invalid hardware rate index, and omitted `desc_info->en_wd_info` on non-management frames.
   * *Fix*: `rtw89_mon_fixup_tx_desc()` selects a valid hardware rate (defaulting to `RTW89_HW_RATE_OFDM6` / 6.0 Mbps), asserts `en_wd_info` / `use_rate` / `dis_data_fb`, and bounds hardware retries (`tx_cnt_lmt = 1`) when `IEEE80211_TX_CTL_NO_ACK` is set.
4. **Uninitialized Monitor `mac_id` Scheduler Context & Sleep Lock (`fw.h`)**
   * *Problem*: Standalone monitor interfaces (`IEEE80211_HW_WANT_MONITOR_VIF`) allocate a `mac_id` and hardware port, but never configure CMAC/DMAC tables or register the link with the firmware scheduler. The firmware keeps the TX queue paused.
   * *Fix*: `rtw89_mon_add_vif_post()` programs `h2c_default_cmac_tbl` / `h2c_default_dmac_tbl`, registers the role via `rtw89_fw_h2c_join_info()` using `RTW89_SELF_ROLE_AP` with `RTW89_NET_TYPE_NO_LINK` (bypassing client-role beacon timeout checks while leaving beacon TX disabled), unpauses the MACID via `rtw89_fw_h2c_macid_pause()`, and clears `R_AX_MACID_SLEEP_0` (`0xC2C0`).

---

## Kernel & Distro Compatibility

The AST/brace-aware patcher (`scripts/patch_rtw89.py`) inspects `core.h` and `mac80211.c` at build time and automatically adapts to both upstream driver generations:

| Driver Generation | Kernel Versions | Locking & VIF Architecture | Distro Examples |
| :--- | :--- | :--- | :--- |
| **API Gen 1** | `Linux 6.1 – 6.10` | `struct rtw89_vif`, `rtwdev->mutex` | Debian 12 (Bookworm), Ubuntu 22.04 LTS (HWE) |
| **API Gen 2** | `Linux 6.11 – 7.x+` & `lwfinger/rtw89` | MLO `struct rtw89_vif_link`, `pure_monitor_mode_vif`, `wiphy_lock` | Kali Linux 2024+, Ubuntu 24.04+, Arch Linux, Fedora |

---

## Repository Structure

```text
rtw89-monitor-injection/
├── src/
│   ├── rtw89_mon.h                  # Extension API declarations & autoconf bridge
│   └── rtw89_mon.c                  # MAC RX filter, H2C monitor context & TX descriptor fixups
├── scripts/
│   ├── patch_rtw89.py               # AST/brace-aware source integrator (--detect-only, --apply, --revert)
│   ├── build_module.sh              # Kbuild build, live reload, /lib/modules/.../updates installer
│   └── verify_monitor_diag.py       # IEEE 802.11 Radiotap RX & single Null-Data diagnostic verifier
├── patches/
│   └── linux-7.1-rtw89-monitor.diff # Reference unified diff for Linux 6.11 - 7.x+
├── tests/
│   └── test_patch_rtw89.py          # Automated Gen 1 / Gen 2 patch & revert unit tests
├── docs/
│   ├── ARCHITECTURE.md              # Register, H2C, and mac80211 flow documentation
│   └── README.ru.md                 # Russian documentation
├── README.md                        # English documentation
└── README_RU.md                     # Russian documentation (root link)
```

---

## Kali Linux & Penetration Testing Quickstart

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
*(Tip: Use `./scripts/build_module.sh --rtw89-dir ~/rtw89_src --reload` if you want to test the module in memory without permanent disk installation).*

---

### Step 3: Monitor Mode with `airmon-ng` / `iw`

#### Method A: Using `airmon-ng` (Kali Linux / Aircrack-ng)
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan0

# Verify monitor interface (usually wlan0mon or wlan0)
sudo airodump-ng wlan0mon
```

#### Method B: Native `iw` and `tcpdump` / `Wireshark`
```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412

# Check kernel logs:
# rtw89_8852ae ...: monitor mode initialized on mac_id=0 port=0 (ret=0)
sudo dmesg | grep rtw89

# Capture full 802.11 frames with Radiotap headers:
sudo tcpdump -i wlan0 -I -n -e -c 30
```

---

### Step 4: Packet Injection Verification (`aireplay-ng` & Diagnostic Tool)

#### Testing with `aireplay-ng`:
```bash
sudo aireplay-ng -9 wlan0mon
```
Expected output:
```text
Trying broadcast probe requests...
Injection is working!
Ping (Ack) answered by AP ...
```

#### Running the Built-in Diagnostic Tool:
The repository includes an automated verification script that tests promiscuous reception across all 802.11 subtypes (Management, Control, Data) and performs an isolated single Null-Data diagnostic frame injection:
```bash
sudo python3 scripts/verify_monitor_diag.py \
  --iface wlan0 \
  --freq 2412 \
  --duration 5 \
  --diag-tx \
  --restore-managed
```

Sample diagnostic summary:
```text
[+] Interface wlan0 set to MONITOR on freq 2412 MHz
[+] Captured 342 frames in 5.0 seconds:
    - Beacon: 184
    - Probe Request: 42
    - Probe Response: 19
    - Control (ACK / RTS / CTS / BlockAck): 82
    - Data: 15
[+] Diagnostic TX test frame injected successfully (6.0 Mbps OFDM, NO_ACK).
[+] Interface restored to MANAGED mode.
```

---

## Uninstalling & Restoring Stock Driver

To remove the override kernel module and revert to your distribution's stock driver:
```bash
./scripts/build_module.sh --uninstall
```

To revert patched `rtw89` source code back to byte-exact originals:
```bash
python3 scripts/patch_rtw89.py --rtw89-dir ~/rtw89_src --revert
```

---

## Running Automated Unit Tests

Automated tests verify that both Gen 1 (Linux 6.1–6.10) and Gen 2 (Linux 6.11–7.x+) patches apply idempotently, compile cleanly, revert without leaving artifacts, and contain no firmware binaries:

```bash
python3 -m unittest discover -s tests -v
```

---

## Frequently Asked Questions (FAQ)

### Q: Why does stock `rtw89` drop Control frames and cross-BSS Data?
**A:** On Realtek 802.11ax hardware (`RTW89_CHIP_AX`), MAC hardware registers `R_AX_RX_FLTR_OPT` (`0xCE20`), `R_AX_CTRL_FLTR` (`0xCE24`), and `R_AX_DATA_FLTR` (`0xCE2C`) default to discarding frames whose destination does not match the device MAC or `ADDR_CAM`. Our patch unlocks these registers (`0x55555555`) whenever monitor mode is active.

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
