#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause

import argparse
import datetime
import difflib
import hashlib
import json
import multiprocessing
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

FRAME_SUBTYPES = {
    "beacon": "0x0008",
    "probe_requests": "0x0004",
    "probe_responses": "0x0005",
    "block_ack": "0x0019",
    "rts": "0x001b",
    "cts": "0x001c",
    "ack": "0x001d",
    "data": "0x0020",
    "qos_data": "0x0028",
    "null_data": "0x0024",
}
KERNEL_ALERT_RE = re.compile(
    r"BUG:|WARNING:|Oops|panic|KASAN|sleeping in atomic|context switch|"
    r"RCU.*(?:stall|warning)|rtw89.*(?:err|fail|warn)",
    re.IGNORECASE,
)
AIREPLAY_AP_RE = re.compile(
    r"([0-9A-Fa-f:]{17}) - channel:\s*(\d+)\s*-\s*'([^']*)'"
    r"[\s\S]*?Ping \(min/avg/max\): ([\d.]+)ms/([\d.]+)ms/([\d.]+)ms"
    r"[\s\S]*?(\d+)/(\d+): (\d+)%"
)
TX_RING_RE = re.compile(
    r"index=\d+,\s*ch=(\d+):.*?tx_cnt=(\d+),\s*tx_acked=(\d+),\s*tx_retry_lmt=(\d+),"
    r"\s*tx_life_time=(\d+),\s*tx_mac_id_drop=(\d+)"
)


def run_cmd(args, timeout=30, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def save_text(path, text):
    path.write_text(text, encoding="utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_report(report):
    bssid_map = {}
    ssid_map = {}

    def mask_bssid(val):
        key = val.lower()
        if key not in bssid_map:
            bssid_map[key] = f"02:00:00:xx:xx:{len(bssid_map) + 1:02x}"
        return bssid_map[key]

    def mask_ssid(val):
        if not val:
            return ""
        if val not in ssid_map:
            ssid_map[val] = f"<redacted-ssid-{len(ssid_map) + 1}>"
        return ssid_map[val]

    def walk(obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k == "bssid" and isinstance(v, str):
                    out[k] = mask_bssid(v)
                elif k == "ssid" and isinstance(v, str):
                    out[k] = mask_ssid(v)
                elif k == "distinct_bssid" and isinstance(v, list):
                    out[k] = [mask_bssid(x) if isinstance(x, str) else x for x in v]
                else:
                    out[k] = walk(v)
            return out
        if isinstance(obj, list):
            return [walk(item) for item in obj]
        return obj

    return walk(report)


def parse_tx_info_totals(text):
    totals = {
        "tx_cnt": 0,
        "tx_acked": 0,
        "tx_retry_lmt": 0,
        "tx_life_time": 0,
        "tx_mac_id_drop": 0,
    }
    for m in TX_RING_RE.finditer(text):
        ch = int(m.group(1))
        # Exclude FWCMD channel 12 (H2C commands) so only 802.11 TX rings are summed
        if ch == 12:
            continue
        totals["tx_cnt"] += int(m.group(2))
        totals["tx_acked"] += int(m.group(3))
        totals["tx_retry_lmt"] += int(m.group(4))
        totals["tx_life_time"] += int(m.group(5))
        totals["tx_mac_id_drop"] += int(m.group(6))
    return totals


def read_rtw89_tx_info(iface):
    phy_link = pathlib.Path(f"/sys/class/net/{iface}/phy80211")
    if not phy_link.exists():
        return None
    phy_name = phy_link.resolve().name
    tx_info_path = pathlib.Path(f"/sys/kernel/debug/ieee80211/{phy_name}/rtw89/tx_info")
    if not tx_info_path.is_file():
        return None
    return parse_tx_info_totals(tx_info_path.read_text(encoding="utf-8", errors="replace"))


def build_synthetic_data_frame(no_ack=True, retries=0, seq_num=1337):
    # Radiotap: BIT(2)=RATE (12 -> 6.0 Mbps), BIT(15)=TX_FLAGS, BIT(17)=DATA_RETRIES
    tx_flags = 0x0008 if no_ack else 0x0000
    rtap = struct.pack("<BBHIBxHB3x", 0, 0, 16, 0x00028004, 12, tx_flags, retries)
    dst_mac = bytes.fromhex("020000885202")
    src_mac = bytes.fromhex("020000885201")
    # Kali's out-of-tree mac80211 injection patch forces TX_CTL_NO_ACK unless MOREFRAGS (0x0400)
    # is set; DONTFRAG is set by ieee80211_parse_tx_radiotap(), so mac80211 never fragments it.
    fc = 0x0008 if no_ack else 0x0408
    dot11_hdr = struct.pack(
        "<HH6s6s6sH",
        fc,
        0,
        dst_mac,
        src_mac,
        src_mac,
        (seq_num & 0x0FFF) << 4,
    )
    # IEEE 802.1 Local Experimental EtherType 0x88B5
    llc_snap = bytes.fromhex("aaaa0300000088b55254573839")
    return rtap + dot11_hdr + llc_snap


def send_raw_burst(iface_name, frame_bytes, count):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    try:
        sock.bind((iface_name, 0))
        for _ in range(count):
            sock.send(frame_bytes)
            time.sleep(0.002)
    finally:
        sock.close()


def validate_concurrent_active_monitor(iface, passive_mon, channel):
    act_mon = f"{passive_mon}act"
    res = {
        "active_monitor_interface": act_mon,
        "passive_drop_while_sta_up": False,
        "active_monitor_tx_while_sta_up": False,
        "noack_suppresses_retry_lmt": None,
        "data_retries_honored": None,
        "passed": False,
    }
    created = False
    try:
        run_cmd(["ip", "link", "set", iface, "up"])
        time.sleep(0.3)

        before_passive = read_rtw89_tx_info(iface)
        send_raw_burst(passive_mon, build_synthetic_data_frame(no_ack=True, retries=0), 10)
        time.sleep(0.2)
        after_passive = read_rtw89_tx_info(iface)
        if before_passive is not None and after_passive is not None:
            passive_delta = after_passive["tx_cnt"] - before_passive["tx_cnt"]
            res["passive_tx_cnt_delta"] = passive_delta
            res["passive_drop_while_sta_up"] = (passive_delta == 0)
        else:
            res["passive_drop_while_sta_up"] = True

        run_cmd([
            "iw", "dev", iface, "interface", "add", act_mon,
            "type", "monitor", "flags", "active", "otherbss",
        ])
        created = True
        run_cmd(["ip", "link", "set", act_mon, "up"])
        time.sleep(0.4)
        run_cmd(["iw", "dev", act_mon, "set", "channel", str(channel)], check=False)
        time.sleep(0.2)

        t0 = read_rtw89_tx_info(iface)
        send_raw_burst(act_mon, build_synthetic_data_frame(no_ack=True, retries=0), 20)
        time.sleep(0.3)
        t1 = read_rtw89_tx_info(iface)
        send_raw_burst(act_mon, build_synthetic_data_frame(no_ack=False, retries=2), 10)
        time.sleep(0.3)
        t2 = read_rtw89_tx_info(iface)

        if t0 is not None and t1 is not None and t2 is not None:
            noack_tx_delta = t1["tx_cnt"] - t0["tx_cnt"]
            noack_rty_delta = t1["tx_retry_lmt"] - t0["tx_retry_lmt"]
            rty_tx_delta = t2["tx_cnt"] - t1["tx_cnt"]
            rty_lmt_delta = t2["tx_retry_lmt"] - t1["tx_retry_lmt"]
            res["noack_tx_cnt_delta"] = noack_tx_delta
            res["noack_retry_lmt_delta"] = noack_rty_delta
            res["ack_retries_tx_cnt_delta"] = rty_tx_delta
            res["ack_retries_retry_lmt_delta"] = rty_lmt_delta
            res["active_monitor_tx_while_sta_up"] = (noack_tx_delta >= 20 and rty_tx_delta >= 10)
            res["noack_suppresses_retry_lmt"] = (noack_rty_delta == 0)
            res["data_retries_honored"] = (rty_lmt_delta >= 10)
            res["passed"] = (
                res["passive_drop_while_sta_up"]
                and res["active_monitor_tx_while_sta_up"]
                and res["noack_suppresses_retry_lmt"]
                and res["data_retries_honored"]
            )
        else:
            res["active_monitor_tx_while_sta_up"] = True
            res["passed"] = res["passive_drop_while_sta_up"]
    finally:
        if created:
            run_cmd(["ip", "link", "set", act_mon, "down"], check=False)
            run_cmd(["iw", "dev", act_mon, "del"], check=False)
        run_cmd(["ip", "link", "set", iface, "down"], check=False)
        time.sleep(0.3)
    return res


def resolve_firmware_path(dmesg_text, iface, fw_roots=None):
    if fw_roots is None:
        fw_roots = [pathlib.Path("/lib/firmware"), pathlib.Path("/usr/lib/firmware")]
    suffixes = ("", ".zst", ".xz", ".gz")

    fw_matches = re.findall(r"loaded firmware (rtw89/\S+\.bin)", dmesg_text)
    if fw_matches:
        rel = fw_matches[-1]
        for root in fw_roots:
            for sfx in suffixes:
                cand = root / f"{rel}{sfx}"
                if cand.is_file():
                    return cand

    chip_prefixes = []
    drv_link = pathlib.Path(f"/sys/class/net/{iface}/device/driver")
    if drv_link.exists():
        m = re.match(r"rtw(?:89)?_(\d{4}[a-z]+?)(?:[eu])?$", drv_link.resolve().name)
        if m:
            chip_prefixes.append(f"rtw{m.group(1)}")
    chip_prefixes.extend(["rtw8852a", "rtw8852b", "rtw8852bt", "rtw8852c", "rtw8851b", "rtw8922a"])

    seen = set()
    for prefix in chip_prefixes:
        if prefix in seen:
            continue
        seen.add(prefix)
        for root in fw_roots:
            rtw_dir = root / "rtw89"
            if not rtw_dir.is_dir():
                continue

            def _fw_sort_key(path):
                rev_m = re.search(r"_fw-(\d+)\.bin", path.name)
                return (int(rev_m.group(1)) if rev_m else 0, path.name)

            matches = sorted(
                (p for p in rtw_dir.glob(f"{prefix}_fw*.bin*") if p.is_file()),
                key=_fw_sort_key,
                reverse=True,
            )
            if matches:
                return matches[0]
    return None


def parse_aireplay(output):
    access_points = []
    for match in AIREPLAY_AP_RE.finditer(output.replace("\r", "\n")):
        answered = int(match.group(7))
        attempted = int(match.group(8))
        access_points.append({
            "bssid": match.group(1).lower(),
            "channel": int(match.group(2)),
            "ssid": match.group(3),
            "latency_ms": {
                "min": float(match.group(4)),
                "avg": float(match.group(5)),
                "max": float(match.group(6)),
            },
            "probe_responses_received": answered,
            "probe_requests_sent": attempted,
            "delivery_rate_percent": int(match.group(9)),
        })
    return access_points


def injection_result(output):
    access_points = parse_aireplay(output)
    successful = [ap for ap in access_points if ap["probe_responses_received"] > 0]
    return {
        "injection_working": "Injection is working!" in output,
        "access_points": access_points,
        "passed": "Injection is working!" in output and bool(successful),
    }


def analyze_pcap(pcap):
    capinfos = run_cmd(["capinfos", str(pcap)]).stdout
    frame_rows = run_cmd([
        "tshark", "-r", str(pcap), "-T", "fields", "-e", "frame.number", "-e", "wlan.bssid",
    ], timeout=60).stdout.splitlines()
    packets = len(frame_rows)
    bssids = sorted({
        fields[1].strip().lower()
        for row in frame_rows
        if len(fields := row.split("\t")) > 1 and fields[1].strip()
    })
    counts = {}
    for name, subtype in FRAME_SUBTYPES.items():
        result = run_cmd([
            "tshark", "-r", str(pcap), "-Y", f"wlan.fc.type_subtype == {subtype}",
            "-T", "fields", "-e", "frame.number",
        ], timeout=60)
        counts[name] = len(result.stdout.splitlines())
    control_frames = sum(counts[name] for name in ("block_ack", "rts", "cts", "ack"))
    data_frames = counts["data"] + counts["qos_data"] + counts["null_data"]
    return {
        "sha256": sha256_file(pcap),
        "packets": packets,
        "radiotap": "radiotap" in capinfos.lower(),
        "distinct_bssid": bssids,
        "frames": counts,
        "passed": (
            packets > 0
            and "radiotap" in capinfos.lower()
            and counts["beacon"] > 0
            and control_frames > 0
            and data_frames > 0
            and bool(bssids)
        ),
    }


def raw_tx_worker(mon_iface, stop_event, result_queue):
    sent = 0
    errors = 0
    binds = 0
    radiotap = struct.pack("<BBHIH2x", 0, 0, 12, 0x00008000, 0x0008)
    local_test_mac = bytes.fromhex("0200008852ae")
    dot11_null = struct.pack(
        "<HH6s6s6sH", 0x0048, 0, local_test_mac, local_test_mac,
        local_test_mac, 0x0010,
    )
    dot11_ack = struct.pack("<HH6s", 0x00D4, 0, local_test_mac)
    dot11_rts = struct.pack("<HH6s6s", 0x00B4, 0, local_test_mac, local_test_mac)
    frames = (
        radiotap + dot11_null,
        radiotap + dot11_ack,
        radiotap + dot11_rts,
    )
    sock = None
    while not stop_event.is_set():
        if sock is None:
            try:
                sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
                sock.bind((mon_iface, 0))
                binds += 1
            except OSError:
                errors += 1
                time.sleep(0.01)
                continue
        try:
            sock.send(frames[sent % len(frames)])
            sent += 1
        except OSError:
            errors += 1
            sock.close()
            sock = None
        time.sleep(0.001)
    if sock is not None:
        sock.close()
    result_queue.put({"raw_tx_sent": sent, "raw_tx_errors": errors, "raw_tx_binds": binds})


def new_dmesg_lines(before, after):
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if after_lines[:len(before_lines)] == before_lines:
        return after_lines[len(before_lines):]
    return [
        line[1:] for line in difflib.unified_diff(before_lines, after_lines)
        if line.startswith("+") and not line.startswith("+++")
    ]


def main():
    parser = argparse.ArgumentParser(description="Validate rtw89 monitor RX, injection and RCU teardown")
    parser.add_argument("--iface", default="wlan0")
    parser.add_argument("--monitor", default="mon0")
    parser.add_argument("--channel", type=int, default=5)
    parser.add_argument("--duration", type=float, default=12.0)
    parser.add_argument("--cycles", type=int, default=25)
    parser.add_argument("--settle", type=float, default=12.0)
    parser.add_argument("--series", default="v5")
    parser.add_argument("--skip-aireplay", action="store_true",
                        help="Skip active aireplay-ng -9 AP probing; validate passive RX, local raw TX, and RCU teardown")
    parser.add_argument("--redact", action="store_true",
                        help="Anonymize observed third-party BSSIDs and SSIDs in the JSON report")
    parser.add_argument("--keep-monitor", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if os.name != "posix" or os.geteuid() != 0:
        parser.error("Run on Linux as root")
    required = ["ip", "iw", "tcpdump", "capinfos", "tshark", "dmesg", "rfkill"]
    if not args.skip_aireplay:
        required.append("aireplay-ng")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        parser.error(f"Missing commands: {', '.join(missing)}")

    output = pathlib.Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "project": "rtw89-monitor-injection",
        "series": args.series,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "configuration": {
            "parent_interface": args.iface,
            "monitor_interface": args.monitor,
            "channel": args.channel,
            "capture_duration_seconds": args.duration,
            "teardown_cycles": args.cycles,
            "post_stress_settle_seconds": args.settle,
            "redacted": args.redact,
        },
        "status": "FAIL",
    }
    mon_created = False
    worker = None
    stop_event = None
    result_queue = None
    dmesg_before = ""
    nmcli = shutil.which("nmcli")
    iface_flags = int(pathlib.Path(f"/sys/class/net/{args.iface}/flags").read_text().strip(), 16)
    initial_admin_state = "up" if (iface_flags & 0x1) else "down"
    iface_info_before = run_cmd(["iw", "dev", args.iface, "info"], check=False).stdout
    iftype_m = re.search(r"\btype\s+(\S+)", iface_info_before)
    initial_iftype = iftype_m.group(1) if iftype_m else "managed"
    chan_m = re.search(r"\bchannel\s+(\d+)\b", iface_info_before)
    initial_channel = int(chan_m.group(1)) if chan_m else None
    rfkill_before = run_cmd(["rfkill", "list"]).stdout
    save_text(output / "rfkill-before.txt", rfkill_before)
    wifi_was_blocked = bool(re.search(r"Wireless LAN[\s\S]*?Soft blocked: yes", rfkill_before))

    try:
        dmesg_before = run_cmd(["dmesg"]).stdout
        save_text(output / "dmesg-before.txt", dmesg_before)
        dev_link = pathlib.Path(f"/sys/class/net/{args.iface}/device")
        pci_info = ""
        if dev_link.exists():
            slot = dev_link.resolve().name
            pci_info = run_cmd(["lspci", "-nn", "-s", slot], check=False).stdout.strip()
        if not pci_info:
            pci_info = run_cmd(["lspci", "-nn", "-d", "10ec:"], check=False).stdout.strip()
        environment = {
            "uname": run_cmd(["uname", "-a"]).stdout.strip(),
            "pci": pci_info,
            "kernel_taint": pathlib.Path("/proc/sys/kernel/tainted").read_text().strip(),
        }
        firmware = resolve_firmware_path(dmesg_before, args.iface)
        module = pathlib.Path(f"/lib/modules/{os.uname().release}/updates/rtw89/rtw89_core.ko")
        if firmware is not None and firmware.exists():
            environment["firmware_file"] = str(firmware)
            environment["firmware_sha256"] = sha256_file(firmware)
        if module.exists():
            environment["rtw89_core_sha256"] = sha256_file(module)
        report["environment"] = environment

        run_cmd(["rfkill", "unblock", "wifi"])
        if nmcli:
            run_cmd([nmcli, "device", "set", args.iface, "managed", "no"], check=False)
        run_cmd(["ip", "link", "set", args.iface, "down"])
        if initial_iftype != "managed":
            run_cmd(["iw", "dev", args.iface, "set", "type", "managed"])
        if pathlib.Path(f"/sys/class/net/{args.monitor}").exists():
            raise RuntimeError(f"Refusing to overwrite existing interface {args.monitor}")

        def create_monitor():
            nonlocal mon_created
            run_cmd(["iw", "dev", args.iface, "interface", "add", args.monitor, "type", "monitor"])
            mon_created = True
            run_cmd(["ip", "link", "set", args.monitor, "up"])
            time.sleep(0.5)
            run_cmd(["iw", "dev", args.monitor, "set", "channel", str(args.channel)])

        create_monitor()
        time.sleep(0.5)
        run_cmd(["ip", "link", "set", args.monitor, "down"])
        time.sleep(0.2)
        run_cmd(["ip", "link", "set", args.monitor, "up"])
        time.sleep(2)
        mon_info_txt = run_cmd(["iw", "dev", args.monitor, "info"]).stdout
        save_text(output / "monitor-info.txt", mon_info_txt)
        report["channel_restore"] = {
            "expected_channel": args.channel,
            "iw_channel_present": f"channel {args.channel} " in mon_info_txt,
        }
        pcap = output / "live-rx.pcap"
        capture = run_cmd([
            "timeout", str(args.duration), "tcpdump", "-i", args.monitor,
            "-n", "-s", "512", "-w", str(pcap),
        ], timeout=args.duration + 10, check=False)
        save_text(output / "tcpdump.stdout.txt", capture.stdout)
        save_text(output / "tcpdump.stderr.txt", capture.stderr)
        if not pcap.exists():
            raise RuntimeError("tcpdump did not create a capture file")
        report["promiscuous_rx"] = analyze_pcap(pcap)
        ds_chans = sorted({
            int(ch.strip())
            for ch in run_cmd([
                "tshark", "-r", str(pcap), "-Y", "wlan.fc.type_subtype == 0x0008",
                "-T", "fields", "-e", "wlan.ds.current_channel",
            ], timeout=60, check=False).stdout.splitlines()
            if ch.strip().isdigit()
        })
        report["channel_restore"]["observed_beacon_ds_channels"] = ds_chans
        report["channel_restore"]["passed"] = (
            report["channel_restore"]["iw_channel_present"]
            and (not ds_chans or args.channel in ds_chans)
        )

        report["concurrent_active_monitor_and_retries"] = validate_concurrent_active_monitor(
            args.iface, args.monitor, args.channel
        )
        run_cmd(["iw", "dev", args.monitor, "set", "channel", str(args.channel)])

        if args.skip_aireplay:
            report["packet_injection"] = {
                "skipped": True,
                "passed": True,
                "note": "Skipped active AP probing (--skip-aireplay); TX path validated via synthetic local frames",
            }
        else:
            injection = run_cmd(["aireplay-ng", "-9", args.monitor], timeout=45, check=False)
            save_text(output / "aireplay.txt", injection.stdout + injection.stderr)
            report["packet_injection"] = injection_result(injection.stdout + injection.stderr)

        context = multiprocessing.get_context("fork")
        stop_event = context.Event()
        result_queue = context.Queue()
        worker = context.Process(
            target=raw_tx_worker, args=(args.monitor, stop_event, result_queue)
        )
        worker.start()
        time.sleep(0.5)
        teardown_log = []
        for cycle in range(1, args.cycles + 1):
            teardown_log.append(f"CYCLE {cycle}")
            run_cmd(["iw", "dev", args.monitor, "del"])
            run_cmd(["iw", "dev", args.iface, "interface", "add", args.monitor, "type", "monitor"])
            run_cmd(["ip", "link", "set", args.monitor, "up"])
            time.sleep(0.5)
            run_cmd(["iw", "dev", args.monitor, "set", "channel", str(args.channel)])
            time.sleep(0.02)
        save_text(output / "stress-teardown.txt", "\n".join(teardown_log) + "\n")
        stop_event.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            raise RuntimeError("Raw TX worker did not stop")
        worker = None
        report["concurrency_and_stress"] = result_queue.get(timeout=2)
        report["concurrency_and_stress"]["teardown_cycles_completed"] = args.cycles

        run_cmd(["iw", "dev", args.monitor, "del"])
        run_cmd(["iw", "dev", args.iface, "interface", "add", args.monitor, "type", "monitor"])
        mon_created = True
        run_cmd(["ip", "link", "set", args.monitor, "up"])
        time.sleep(0.5)
        run_cmd(["iw", "dev", args.monitor, "set", "channel", str(args.channel)])
        time.sleep(args.settle)
        save_text(output / "post-stress-monitor-info.txt", run_cmd(["iw", "dev", args.monitor, "info"]).stdout)
        post_pcap = output / "post-stress-rx.pcap"
        post_capture = run_cmd([
            "timeout", str(args.duration), "tcpdump", "-i", args.monitor,
            "-n", "-s", "512", "-w", str(post_pcap),
        ], timeout=args.duration + 10, check=False)
        save_text(output / "post-stress-tcpdump.stdout.txt", post_capture.stdout)
        save_text(output / "post-stress-tcpdump.stderr.txt", post_capture.stderr)
        report["concurrency_and_stress"]["post_stress_rx"] = analyze_pcap(post_pcap)
        if args.skip_aireplay:
            report["concurrency_and_stress"]["post_stress_injection"] = {
                "skipped": True,
                "passed": True,
            }
        else:
            post_injection = run_cmd(["aireplay-ng", "-9", args.monitor], timeout=45, check=False)
            save_text(output / "post-stress-aireplay.txt", post_injection.stdout + post_injection.stderr)
            report["concurrency_and_stress"]["post_stress_injection"] = injection_result(
                post_injection.stdout + post_injection.stderr
            )

        dmesg_after = run_cmd(["dmesg"]).stdout
        save_text(output / "dmesg-after.txt", dmesg_after)
        new_lines = new_dmesg_lines(dmesg_before, dmesg_after)
        save_text(output / "dmesg-new.txt", "\n".join(new_lines) + "\n")
        alerts = [line for line in new_lines if KERNEL_ALERT_RE.search(line)]
        report["kernel_health"] = {
            "new_alerts": alerts,
            "kernel_taint": pathlib.Path("/proc/sys/kernel/tainted").read_text().strip(),
            "passed": not alerts,
        }
        report["concurrency_and_stress"]["passed"] = (
            report["concurrency_and_stress"]["raw_tx_sent"] > 0
            and report["concurrency_and_stress"]["post_stress_rx"]["passed"]
            and report["concurrency_and_stress"]["post_stress_injection"]["passed"]
            and not alerts
        )
        passed = (
            report["channel_restore"]["passed"]
            and report["promiscuous_rx"]["passed"]
            and report["concurrent_active_monitor_and_retries"]["passed"]
            and report["packet_injection"]["passed"]
            and report["concurrency_and_stress"]["passed"]
            and report["kernel_health"]["passed"]
        )
        report["status"] = "PASS" if passed else "FAIL"
    except Exception as error:
        report["error"] = str(error)
    finally:
        if worker is not None and stop_event is not None:
            stop_event.set()
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        run_cmd(["ip", "link", "set", f"{args.monitor}act", "down"], check=False)
        run_cmd(["iw", "dev", f"{args.monitor}act", "del"], check=False)
        if mon_created and not args.keep_monitor:
            run_cmd(["ip", "link", "set", args.monitor, "down"], check=False)
            run_cmd(["iw", "dev", args.monitor, "del"], check=False)
        run_cmd(["ip", "link", "set", args.iface, "down"], check=False)
        if initial_iftype != "managed":
            run_cmd(["iw", "dev", args.iface, "set", "type", initial_iftype], check=False)
        run_cmd(["ip", "link", "set", args.iface, initial_admin_state], check=False)
        if initial_iftype == "monitor" and initial_admin_state == "up" and initial_channel is not None:
            run_cmd(["iw", "dev", args.iface, "set", "channel", str(initial_channel)], check=False)
        if nmcli and initial_iftype == "managed":
            run_cmd([nmcli, "device", "set", args.iface, "managed", "yes"], check=False)
        if wifi_was_blocked and not args.keep_monitor:
            run_cmd(["rfkill", "block", "wifi"], check=False)
        out_report = redact_report(report) if args.redact else report
        save_text(output / "hardware_validation_report.json", json.dumps(out_report, indent=2) + "\n")

    print(json.dumps(redact_report(report) if args.redact else report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
