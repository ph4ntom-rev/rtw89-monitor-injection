# Validation report

## 1. Submission status

| Version | Status |
| :--- | :--- |
| v3 | Posted to linux-wireless on 2026-11-05 ([lore](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/)), waiting for review. |
| v4 | Never sent. Kept in `patches/v4/` for reference. |
| v5 | `patches/v5/`, 10 patches against `torvalds/linux` `d48723711bae`. To be sent after the v3 review and after the hardware checks in section 3 pass on v5. |

## 2. Static checks for v5

Run locally against `v7.4-rc1`, whose rtw89 sources are identical to the series base,
and repeated by [CI](../.github/workflows/ci.yml) against the base commit:

| Check | Result |
| :--- | :--- |
| `git am` of all 10 patches | applies cleanly |
| Build after each patch (bisectability) | all 10 steps build |
| `make W=1 C=1` (gcc 15, sparse) on `drivers/net/wireless/realtek/rtw89` | 0 warnings before and after the series |
| `checkpatch.pl --strict` | 0 errors, 0 warnings, 0 checks (apart from "unknown commit id" in a shallow clone; the ids in the `Fixes:` tags exist upstream) |
| `patch_rtw89.py --apply` on the base, external build with `W=1 C=1` | builds without warnings, `--revert` restores the tree |
| DKMS flow (`dkms_prepare.sh`, `dkms_pre_build.sh`) with stubbed `dkms` | correct module list, `BUILD_EXCLUSIVE_KERNEL` matches only the source series, mismatched kernels are rejected |

## 3. Hardware results (obtained with v4)

> [!WARNING]
> These results were measured with the **v4** code. v5 differs from v4 in how
> non-injected control frames are classified (patch 7), in dropping
> `rtw89_recalc_pure_monitor_vif()` (patch 4), and in the NULL vif fix being a separate
> patch. Re-run the reproducer below on v5 and update this table before sending v5.

- Hardware: Realtek RTL8852AE PCIe (`10ec:8852`)
- Firmware: `rtw89/rtw8852a_fw.bin` 0.13.36.2 (unmodified `linux-firmware`)
- Kernel: Linux 7.1.5 (Kali)

| Check | v4 result |
| :--- | :--- |
| Promiscuous RX, 8 s on channel 5 (2432 MHz) | 21,705 frames (127 beacons, 2,413 BlockAck, 1,390 RTS, 3,147 CTS, 2,797 ACK, 11,282 QoS data, 211 null data) |
| `aireplay-ng -9` | 10/10, average 12.34 ms |
| Raw TX: 10-byte ACK, 16-byte RTS, 24-byte association request, SN 1337, no radiotap rate | all transmitted, no kernel warnings |
| 25 `iw dev mon0 del/add` cycles with concurrent raw TX | 13,579 frames sent, no warnings |
| RX after the stress test without `iw set channel` | 5,248 frames right after `ip link set mon0 up` |
| `aireplay-ng -9` after the stress test | 30/30, average 15.56 ms |
| ftrace of `rtw89_set_channel` / `rtw89_chip_rfk_channel` | one call each for cold start, `down/up` and `del/add/up` |
| `dmesg` (WARN, oops, lockdep) and station reconnect | no new messages, `wlan0` reconnected |

Additional checks that should be done on v5, because v5 changes this behaviour:

- Normal station traffic with A-MPDU (BlockAckReq frames are sent by mac80211 through
  the data path again, as upstream).
- Injection of control frames still uses the management queue.

## 4. Reproducer

Run on an isolated test machine as root:

```bash
sudo python3 scripts/verify_hardware.py \
  --series v5 \
  --channel 5 \
  --duration 8 \
  --cycles 25 \
  --settle 1.5 \
  --output /tmp/rtw89-v5-validation
```

The script brings managed interfaces down, checks RX of control, management and data
frames, runs `aireplay-ng -9`, sends raw `AF_PACKET` frames (null data, 10-byte ACK,
16-byte RTS) during 25 delete/add cycles, checks that RX works after `ip link set mon0 up`
without `iw set channel`, and restores the previous interface state at the end.

## 5. Scope

- Injection needs a standalone monitor interface. While a station or AP interface is up,
  injected frames are dropped.
- Only RTL8852AE was runtime-tested. RTL8852BE/CE, RTL8851BE, RTL8922AE, USB devices and
  DBCC (`RTW89_MAC_1`) use the same code paths but were not tested on hardware.
