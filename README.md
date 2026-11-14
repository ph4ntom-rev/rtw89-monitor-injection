# rtw89: monitor mode and raw frame injection fixes (`PATCH v5`)

[![CI](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml/badge.svg)](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)

**[English](README.md) | [Русский](README_RU.md) | [Architecture](docs/ARCHITECTURE.md) | [Validation & Evidence](docs/PREFLIGHT_VALIDATION.md)**

Patches for the mainline Linux `rtw89` driver (Realtek RTL8851B/8852A/8852B/8852C/8922A)
that fix monitor mode reception across scan/ROC/MAC restores, enable raw 802.11 frame
injection (`aireplay-ng`, radiotap TX, `DATA_RETRIES`, `NO_ACK`) on standalone and
`MONITOR_FLAG_ACTIVE` monitor interfaces, and restore the operating channel when leaving
idle across monitor restarts. Firmware is not modified.

## Status

| | |
| :--- | :--- |
| Current series | [`patches/v5/`](patches/v5/), 10 patches (`[PATCH rtw-next v5]`), base `pkshih/rtw.git` `rtw-next` [`61a5eb87b2d9`](https://github.com/pkshih/rtw/commit/61a5eb87b2d9b4d5e353780b781cf691f39ba9e1) (Linux 7.4-rc1) |
| Mailing list | [`[PATCH rtw-next v5 00/10]`](https://lore.kernel.org/linux-wireless/20261113235211.1911-1-owner@ph4ntom-rev.uz/) posted on `linux-wireless` (2026-11-13); supersedes [v3](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) (v4 was never sent). |
| Build & static checks | Each patch builds on its own; `W=1` and sparse (`C=1`) with `CONFIG_PROVE_LOCKING=y` show `0` new warnings; `checkpatch.pl --strict` reports `0` errors/warnings/checks; `patch_rtw89.py` verified across `v6.1`, `v6.6`, `v6.12` (Gen 1), `v6.15`, `v6.18` (Gen 2), and `v7.4-rc1` with Kbuild `make W=1 C=1 modules` on LTS `v6.6`, `v6.12`, `v6.18`, and `v7.4-rc1` in [CI](.github/workflows/ci.yml). |
| Runtime & FW verification | **RTL8852AE** (`10ec:8852`, firmware `rtw89/rtw8852a_fw-1.bin` `0.13.36.2`, Linux `7.1.5+kali-amd64`) — verified in IDA Pro 9.5 and live-tested via `tracefs`/kprobes + `verify_hardware.py` (`PASS`), see [validation & screenshots](docs/PREFLIGHT_VALIDATION.md). |
| Not runtime-tested | RTL8852B/C/BT, RTL8851B, RTL8922A/D (Wi-Fi 7), USB devices, DBCC. |

## The v5 series

| # | Patch | Kind |
| :-- | :--- | :--- |
| 1 | `wifi: rtw89: avoid NULL vif dereference in rtw89_ops_tx()` | fix, `Fixes:` + `Cc: stable` |
| 2 | `wifi: rtw89: handle negative rate index for injected frames` | fix, `Fixes:` + `Cc: stable` |
| 3 | `wifi: rtw89: fix RX filter configuration for monitor mode` | fix (`B_AX_SNIFFER_MODE` + `FIF_OTHER_BSS`) |
| 4 | `wifi: rtw89: protect pure_monitor_mode_vif with RCU` | RCU + sole-monitor tracking |
| 5 | `wifi: rtw89: use AP self role for monitor interfaces` | injection (FW `0.13.36.2` verified) |
| 6 | `wifi: rtw89: transmit frames from monitor interfaces` | injection (standalone + `MONITOR_FLAG_ACTIVE`) |
| 7 | `wifi: rtw89: send injected control frames through the management path` | injection |
| 8 | `wifi: rtw89: skip host-side TX hooks for injected frames` | injection |
| 9 | `wifi: rtw89: honor ASSIGN_SEQ, retries, and NO_ACK for injected frames` | injection (`max_rate_tries`, `NO_ACK` status) |
| 10 | `wifi: rtw89: restore channel when leaving idle on non-chanctx devices` | fix (`CONF_CHANGE_IDLE \| MONITOR`) |

Details of every patch and the `rtw89_mon` out-of-tree layer are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the
[cover letter](patches/v5/v5-0000-cover-letter.patch), and
[docs/PREFLIGHT_VALIDATION.md](docs/PREFLIGHT_VALIDATION.md).

![End-to-End Hardware Validation Summary](docs/evidence/screenshots/04_hardware_validation_summary.png)

> [!NOTE]
> **Passive vs Active Monitor TX:** A passive monitor interface (`iw ... type monitor`
> without `flags active`) transmits only when it is the sole open interface on the card; if
> a station or AP interface is up, passive monitor TX is dropped cleanly. To inject frames
> concurrently while a station or AP interface remains open (preserving hardware Auto-ACK),
> create an active monitor interface (`NL80211_FEATURE_ACTIVE_MONITOR`):
> `sudo iw dev wlan0 interface add mon0act type monitor flags active otherbss`.

## Usage

### A. Mainline / wireless-next kernel tree

```bash
cd linux
git am /path/to/rtw89-monitor-injection/patches/v5/v5-00*-wifi-*.patch
```

To run local read-only submission preflight checks (`checkpatch.pl --strict`, trailer
checks, and dry-run command generation without sending mail):

```bash
bash scripts/prepare_submission.sh --kernel-dir /path/to/linux
```

### B. Distribution kernel, one-off build

`scripts/patch_rtw89.py` applies the `v5` changes to the `rtw89` sources of a distribution
kernel (`Linux 6.1 - 7.x`) and fails fast if any mandatory hook anchor does not match.

```bash
sudo apt install linux-headers-$(uname -r) linux-source build-essential python3
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz --strip-components=6 -C ~/rtw89_src \
    --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

bash scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

`build_module.sh` asks before unloading the running driver (Wi-Fi drops briefly); use
`--yes` to skip the prompt. All `rtw89` modules are installed together into
`/lib/modules/$(uname -r)/updates/rtw89`, so core, bus, and chip modules always come from
the same source tree. Undo with `bash scripts/build_module.sh --uninstall`.

**Secure Boot:** unsigned modules are rejected by the kernel. The script detects Secure
Boot via `mokutil` and stops before unloading anything. Sign with an enrolled MOK:

```bash
bash scripts/build_module.sh --rtw89-dir ~/rtw89_src --install \
    --sign-key /var/lib/shim-signed/mok/MOK.priv --sign-cert /var/lib/shim-signed/mok/MOK.der
```

### C. Distribution kernel with DKMS

```bash
bash scripts/dkms_prepare.sh --source /usr/src/linux-source-7.1.tar.xz
sudo dkms install rtw89-monitor-injection/5.0
```

`dkms_prepare.sh` scopes `BUILD_EXCLUSIVE_KERNEL` to the source kernel's `MAJOR.MINOR`
series (supporting both stable and `-rc` kernel strings). After upgrading to a new kernel
series, re-run `dkms_prepare.sh` with the matching `linux-source` archive. Remove with
`bash scripts/dkms_prepare.sh --remove`.

### Monitor mode and injection

```bash
# Standalone monitor interface
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412
sudo tcpdump -i wlan0 -e -c 30

# Or concurrent active monitor alongside managed wlan0
sudo iw dev wlan0 interface add mon0act type monitor flags active otherbss
sudo ip link set dev mon0act address 02:00:00:88:52:10
sudo ip link set mon0act up
```

## Repository layout

```text
patches/v5/            current 10-patch series + cover letter
patches/v2..v4/        earlier versions, kept for reference
src/rtw89_mon.{c,h}    helper code linked into rtw89_core by patch_rtw89.py
scripts/               patch_rtw89.py, build_module.sh, dkms_prepare.sh, dkms_pre_build.sh,
                       prepare_submission.sh, verify_hardware.py, verify_monitor_diag.py
dkms/dkms.conf.in      DKMS template used by dkms_prepare.sh
tests/                 unit tests for patch_rtw89.py and patch series hygiene
docs/                  architecture notes, hardware validation report, and evidence/
```

## Legal and regulatory

- No firmware binaries or vendor code are included; the stock signed firmware from
  `linux-firmware` is used.
- Regulatory domain handling, TX power limits, DFS and `NO_IR` flags are not modified.
  You are responsible for using radio equipment in line with local regulations, and for
  only transmitting on networks you are authorized to test.
- Dual-licensed `GPL-2.0-only OR BSD-3-Clause` like the upstream driver
  ([LICENSE](LICENSE), [LICENSES/](LICENSES/)).
- Realtek is a trademark of Realtek Semiconductor Corp. This project is not affiliated
  with Realtek.
