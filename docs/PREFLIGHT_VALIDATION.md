# Validation report

## 1. Submission status

| Version | Status |
| :--- | :--- |
| v3 | Posted to linux-wireless on 2026-11-05 ([lore](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/)), waiting for review. |
| v4 | Never sent. Kept in `patches/v4/` for reference. |
| v5 | `patches/v5/`, 10 patches against `torvalds/linux` `d48723711bae`. Firmware-verified and runtime-validated on RTL8852AE; ready to send after v3 review. |

## 2. Static & CI checks for v5

Run locally against `v7.4-rc1` (`d48723711bae9b8cdc56719c448ba42fef28cbee`) and repeated
automatically by [CI](../.github/workflows/ci.yml):

| Check | Result |
| :--- | :--- |
| `git am` of all 10 patches | applies cleanly |
| Build after each patch (bisectability) | all 10 steps build cleanly (PCIe, USB, AX, and BE modules enabled) |
| `make W=1 C=1` (`sparse` + `CONFIG_PROVE_LOCKING=y`) | 0 new compiler or sparse warnings across the series |
| `checkpatch.pl --strict` | 0 errors, 0 warnings, 0 checks across `v5-0001`..`v5-0010` |
| `patch_rtw89.py --apply` across `v6.1`, `v6.6`, `v6.12` (Gen 1), `v6.15`, `v6.18` (Gen 2), and `v7.4-rc1` | hooks apply cleanly without warnings; `--revert` restores trees byte-for-byte |
| Out-of-tree Kbuild (`make W=1 C=1 modules`) on LTS `v6.6`, `v6.12` (Gen 1) and `v6.18` (Gen 2) | compiles all AX/BE PCIe & USB modules cleanly with 0 new compiler or sparse warnings |
| DKMS flow (`dkms_prepare.sh`, `dkms_pre_build.sh`) | handles stable, `-rc`, and cross-series `.config` lookup (`BUILD_EXCLUSIVE_KERNEL`), rejects mismatched series |

## 3. Firmware verification (`RTL8852AE` `0.13.36.2`, commit `52acc807`)

All firmware-facing assumptions in patches 3, 5, and 10 were verified statically against
unmodified `linux-firmware` (`rtw89/rtw8852a_fw-1.bin`, SHA-256
`fdb5513cf7372440d7980a61aa4b753b192b056734a978458ae4a4ef7f4b2c1e`):

- **JoinInfo H2C role handling (patch 5):** `H2C_FUNC_MAC_JOININFO_Handler` (`0xb89949c0`)
  routes `(self_role != RTW89_SELF_ROLE_AP && op == 1)` (`net_type == RTW89_NET_TYPE_NO_LINK`)
  into `HAL8852A_Role_Disconnect` (`0xb897ddec`), which deactivates the role entry just
  created by `ROLE_MAINTAIN` and skips `CCTL_ResetByMacid` (`0xb89bddac`). Using
  `RTW89_SELF_ROLE_AP` with `RTW89_NET_TYPE_NO_LINK` initializes the MAC ID's CMAC control
  entry (`CCTL_ResetByMacid` + `HAL8852A_Role_Connect` at `0xb897dc78`) while skipping
  client TBTT tracking and AP beacon transmission.
- **RX filter across scan/ROC/MAC init (patch 3):** Firmware scan offload pauses/restores
  `0xCE06`, while host `rtw89_hw_scan_complete_cb()`, `rtw89_roc_end()`, `rx_fltr_init_ax()`,
  and `rx_fltr_init_be()` rewrite `rtwdev->hal.rx_fltr` to `R_AX_RX_FLTR_OPT` (`0xCE20`) /
  `R_BE_RX_FLTR_OPT`. Refining `rx_fltr` whenever `B_AX_SNIFFER_MODE` (`BIT(0)`) is set
  preserves monitor reception across all filter restores without altering non-monitor state.
- **Channel context emulation on RTL8852AE (patch 10):** `rtw8852a_chip_info` sets
  `support_chanctx_num = 1`, but firmware `0.13.36.2` only advertises `SCAN_OFFLOAD` and
  `TX_WAKE` (no `BEACON_FILTER`), so `rtw89_alloc_ieee80211_hw()` leaves
  `rtwdev->ops->assign_vif_chanctx == NULL` (`no_chanctx = true`). Gating channel
  restoration on `!rtwdev->ops->assign_vif_chanctx` properly covers RTL8852AE.

## 4. Hardware results on `v5` (measured 2026-11-10)

- **Hardware:** Realtek RTL8852AE PCIe (`10ec:8852`)
- **Firmware:** `rtw89/rtw8852a_fw-1.bin` `0.13.36.2` (`52acc807`, unmodified `linux-firmware`)
- **Kernel:** Linux `7.1.5+kali-amd64` (`SMP PREEMPT_DYNAMIC x86_64`)

| Check | `v5` result |
| :--- | :--- |
| Monitor restart channel restore (`down` -> `up` on ch 7 without `iw set channel`) | `PASS`: all captured beacons carry DS channel `7`; `ftrace` confirms 1 call each to `rtw89_set_channel` and `rtw89_chip_rfk_channel` |
| Promiscuous RX, 8 s on channel 7 (2442 MHz) right after `down/up` | `PASS`: 612 frames with Radiotap (202 beacons, 8 probe req, 12 probe resp, 98 BlockAck, 90 CTS, 30 ACK, 157 QoS data) |
| Concurrent `wlan0` (managed UP) + `mon0` NULL-vif guard & standalone promotion | `PASS`: 5/5 frames dropped safely (`rtw89_ops_tx = 5`, `rtw89_core_tx_write = 0`) while `wlan0` UP; 5/5 transmitted (`rtw89_core_tx_write = 5`) immediately after `ip link set wlan0 down` |
| Raw TX descriptors (`02:00:00:88:52:ae`): 10B ACK & 16B RTS (`idx = -1`, default flags), 24B Data & AssocReq (`SN = 1337`, `NOSEQ \| NOACK`) | `PASS`: all DMA completions echoed intact (`10B 0x00d4`, `16B 0x00b4`, exact `SN = 1337` preserved on Data and AssocReq) |
| 25 `iw dev mon0 del/add` RCU stress cycles with concurrent raw TX | `PASS`: 11,628 raw TX frames sent across 25/25 cycles, 0 kernel alerts |
| Post-stress promiscuous RX, 8 s on channel 7 | `PASS`: 1,398 frames (203 beacons, 389 BlockAck, 249 CTS, 13 data, 537 QoS data) |
| `dmesg` (`BUG`, `WARNING`, `Oops`, RCU stall, `rtw89` errors) | `PASS`: 0 new alerts |

## 5. Reproducer

Run on an isolated test machine as root:

```bash
sudo python3 scripts/verify_hardware.py \
  --series v5 \
  --channel 7 \
  --duration 8 \
  --cycles 25 \
  --settle 2.0 \
  --output /tmp/rtw89-v5-validation
```

Pass `--skip-aireplay` to omit active broadcast/AP `aireplay-ng -9` probing and validate
passive RX, `down/up` channel restoration, local raw `AF_PACKET` TX (`02:00:00:88:52:ae`:
null data, 10-byte ACK, 16-byte RTS), and 25 delete/add RCU teardown cycles only.

## 6. Scope

- Injection requires a standalone monitor interface. While a station or AP interface is up,
  injected frames are dropped cleanly.
- Only RTL8852AE was runtime-tested. RTL8852BE/CE/BTE, RTL8851BE, RTL8922AE/DE, USB devices,
  and DBCC (`RTW89_MAC_1`) compile cleanly and share the same code paths, but were not
  tested on hardware.
