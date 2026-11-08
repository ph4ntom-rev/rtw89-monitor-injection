# rtw89: monitor mode and raw frame injection fixes (`PATCH v5`)

[![CI](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml/badge.svg)](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)

**[English](README.md) | [Русский](README_RU.md) | [Architecture](docs/ARCHITECTURE.md) | [Validation](docs/PREFLIGHT_VALIDATION.md)**

Patches for the mainline Linux `rtw89` driver (Realtek RTL8851B/8852A/8852B/8852C/8922A)
that fix monitor mode reception and make raw 802.11 frame injection (`aireplay-ng`,
radiotap TX) work on a standalone monitor interface. Firmware is not modified.

## Status

| | |
| :--- | :--- |
| Current series | [`patches/v5/`](patches/v5/), 10 patches, base `torvalds/linux` [`d48723711bae`](https://github.com/torvalds/linux/commit/d48723711bae9b8cdc56719c448ba42fef28cbee) (7.4-rc1 merge window) |
| Mailing list | v3 [posted](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) on 2026-11-05, waiting for review. v4 was never sent. v5 will be sent after v3 review and hardware re-validation. |
| Build | Each patch builds on its own; `W=1` and sparse (`C=1`) show no new warnings; `checkpatch.pl --strict` is clean. Checked in [CI](.github/workflows/ci.yml). |
| Runtime testing | **RTL8852AE only** (`10ec:8852`, firmware 0.13.36.2, Linux 7.1.5), done with the **v4** code. v5 has not been re-tested on hardware yet, see [validation](docs/PREFLIGHT_VALIDATION.md). |
| Not tested | RTL8852B/C, RTL8851B, RTL8922A (Wi-Fi 7), USB devices, DBCC. |

## The v5 series

| # | Patch | Kind |
| :-- | :--- | :--- |
| 1 | `wifi: rtw89: avoid NULL vif dereference in rtw89_ops_tx()` | fix, `Fixes:` + `Cc: stable` |
| 2 | `wifi: rtw89: handle negative rate index for injected frames` | fix, `Fixes:` |
| 3 | `wifi: rtw89: fix RX filter configuration for monitor mode` | fix |
| 4 | `wifi: rtw89: protect pure_monitor_mode_vif with RCU` | preparation, no functional change |
| 5 | `wifi: rtw89: use AP self role for monitor interfaces` | injection, needs confirmation from Realtek |
| 6 | `wifi: rtw89: transmit frames from a standalone monitor interface` | injection |
| 7 | `wifi: rtw89: send injected control frames through the management path` | injection |
| 8 | `wifi: rtw89: skip host-side TX hooks for injected frames` | injection |
| 9 | `wifi: rtw89: honor ASSIGN_SEQ and NO_ACK for injected frames` | injection |
| 10 | `wifi: rtw89: restore channel after monitor restart on non-chanctx chips` | fix |

What changed since v4 is listed in the [cover letter](patches/v5/v5-0000-cover-letter.patch).
The most important point: v4 sent **all** control frames through the management queue,
including BlockAckReq frames built by mac80211. v5 only does this for injected frames.

> [!NOTE]
> Injection requires a **standalone** monitor interface: no station or AP interface may be
> up on the same card. Otherwise the frames are dropped (patch 6), as before.

## Usage

### A. Mainline / wireless-next kernel tree

```bash
cd linux
git am /path/to/rtw89-monitor-injection/patches/v5/v5-00*-wifi-*.patch
```

### B. Distribution kernel, one-off build

`scripts/patch_rtw89.py` applies the same changes to the rtw89 sources of an older
kernel. It edits the C sources with pattern matching, so it is **best effort**: CI checks
it only against the series base. Check the output of each `[APPLY]` step.

```bash
sudo apt install linux-headers-$(uname -r) linux-source build-essential python3
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz --strip-components=6 -C ~/rtw89_src \
    --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

`build_module.sh` asks before unloading the running driver (Wi-Fi drops briefly), use
`--yes` to skip the question. All rtw89 modules are installed together into
`/lib/modules/$(uname -r)/updates/rtw89`, so the core, bus and chip modules always come
from the same source tree. Undo with `./scripts/build_module.sh --uninstall`.

**Secure Boot:** unsigned modules are rejected by the kernel. The script detects Secure
Boot with `mokutil` and stops before unloading anything. Sign with an enrolled MOK:

```bash
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install \
    --sign-key /var/lib/shim-signed/mok/MOK.priv --sign-cert /var/lib/shim-signed/mok/MOK.der
```

### C. Distribution kernel with DKMS

```bash
./scripts/dkms_prepare.sh --source /usr/src/linux-source-7.3.tar.xz
sudo dkms install rtw89-monitor-injection/5.0
```

The rtw89 sources come from one kernel series, so DKMS only rebuilds for kernels of that
series (`BUILD_EXCLUSIVE_KERNEL`). Other kernels keep the stock driver. After a major
kernel upgrade, run `dkms_prepare.sh` again with the new `linux-source` package. Remove
with `./scripts/dkms_prepare.sh --remove`.

### Monitor mode and injection

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412
sudo tcpdump -i wlan0 -e -c 30
sudo aireplay-ng -9 wlan0
```

## Repository layout

```text
patches/v5/            current series (+ cover letter)
patches/v2..v4/        earlier versions, kept for reference
src/rtw89_mon.{c,h}    helper code linked into rtw89_core by patch_rtw89.py
scripts/               patch_rtw89.py, build_module.sh, dkms_prepare.sh, dkms_pre_build.sh,
                       verify_hardware.py, verify_monitor_diag.py
dkms/dkms.conf.in      DKMS template used by dkms_prepare.sh
tests/                 unit tests for the integrator and the patch files
docs/                  architecture notes, validation report
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
