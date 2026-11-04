# RTL8852AE v3 preflight validation

## Scope

The v3 series targets monitor-mode RX filtering, raw frame injection and channel restoration after monitor interface recreation. The upstream base is `dc16902cad9c6b08df9395a510c7be44fbab545d`.

The channel-restoration patch identifies:

```text
Fixes: d9204e99b696 ("wifi: rtw89: do RF calibration once setting channel when running pure monitor mode")
```

The historical commit and its parent were compared statically. No historical runtime test was performed.

## Upstream submission

The three-patch v3 series was submitted to `linux-wireless@vger.kernel.org` on 2026-11-05, with Ping-Ke Shih, Kalle Valo and `linux-kernel@vger.kernel.org` in Cc. SMTP returned `250` for the cover and all three patches.

Message-IDs:

- Cover: `<20261104203051.564-1-owner@ph4ntom-rev.uz>`
- Patch 1/3: `<20261104203051.564-2-owner@ph4ntom-rev.uz>`
- Patch 2/3: `<20261104203051.564-3-owner@ph4ntom-rev.uz>`
- Patch 3/3: `<20261104203051.564-4-owner@ph4ntom-rev.uz>`

SMTP acceptance is not maintainer review or upstream acceptance.

## Verified on RTL8852AE

Runtime: Linux `7.1.5+kali-amd64`.

| Check | Result |
| --- | --- |
| 25 monitor recreate cycles with raw TX | 16,670 post-stress frames, 225 beacons, no new kernel alerts |
| 25 recreate cycles without raw TX | 20,517 post-stress frames |
| Post-stress injection | 30/30 responses from one AP |
| Managed operation | Scanning and association succeed |
| Rollback with forced `-EINVAL` | `join_info(true)` before link cleanup; original error preserved |
| Sequential application of all three patches | Pass; final content matches the built source |
| Linux 7.3.0 full build | Pass, no build warnings |
| `W=1` module build | Pass, no build warnings |
| `sparse` module build | Pass, no build warnings |
| `checkpatch.pl --strict` for all three patches | 0 errors, 0 warnings, 0 checks |

Both access points responded at baseline. In the final post-stress injection run, one AP answered 30/30 probes; the other AP is not listed as answering in the parsed report. No claim is made that both APs answered 30/30 after stress.

One earlier no-TX attempt returned `EMFILE` (`-23`). A subsequent full run passed; no driver cause was established for that transient error.

## Reproducer

`scripts/verify_hardware.py` uses real `AF_PACKET` Radiotap injection, monitor recreation, PCAP and dmesg collection. Example for an isolated test machine:

```bash
python3 scripts/verify_hardware.py \
  --channel 5 \
  --duration 12 \
  --cycles 25 \
  --settle 2 \
  --keep-monitor \
  --output /tmp/rtw89-validation
```

The validator requires post-stress RX as well as injection and kernel-health checks for an overall pass. PCAP files and local diagnostic artifacts are excluded from the repository.

## Limitations

Only RTL8852AE was tested. Other rtw89 chips, DBCC/MAC 1 configurations, long-term operation and over-air injection of every control-frame subtype remain unverified.

Development and validation assistance: Kimi Code (Moonshot AI). The submitter reviewed the final changes and takes responsibility for the submission.
