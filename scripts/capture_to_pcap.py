#!/usr/bin/env python3
"""
capture_to_pcap.py
Capture raw 802.11 frames from a wireless interface in monitor mode and write them
to a .pcap file (linktype = IEEE802_11_RADIOTAP).

Usage example on the target machine (Kali Linux):
    sudo python3 /root/rtw89_monitor_rawtx/scripts/capture_to_pcap.py \
        --iface wlan0 --duration 20 --out /root/capture.pcap

The script performs the same monitor‑mode setup as verify_stage4_monitor.py, but
instead of just counting frames it stores every captured packet in a pcap file
that can be opened with Wireshark.
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import time

PCAP_GLOBAL_HEADER = struct.pack(
    "<IHHIIII",
    0xa1b2c3d4,  # magic number
    2,           # major version
    4,           # minor version
    0,           # thiszone (GMT)
    0,           # sigfigs
    65535,       # snaplen
    127,         # network (LINKTYPE_IEEE802_11_RADIOTAP: 127)
)


def run_cmd(cmd: list[str], check: bool = True):
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def setup_monitor_iface(iface: str, freq_mhz: int):
    print(f"=== [1] Configuring {iface} as NL80211_IFTYPE_MONITOR ({freq_mhz} MHz) ===")
    subprocess.run(["nmcli", "device", "set", iface, "managed", "no"], check=False, capture_output=True)
    run_cmd(["ip", "link", "set", iface, "down"]) 
    run_cmd(["iw", "dev", iface, "set", "type", "monitor"]) 
    run_cmd(["ip", "link", "set", iface, "up"]) 
    run_cmd(["iw", "dev", iface, "set", "freq", str(freq_mhz)])
    info = run_cmd(["iw", "dev", iface, "info"]) 
    for line in info.stdout.splitlines():
        print("   ", line)


def capture_to_pcap(iface: str, duration: float, out_path: str):
    print(f"\n=== [2] Capturing to pcap ({duration:.1f}s) ===")
    ETH_P_ALL = 0x0003
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    sock.bind((iface, 0))
    sock.settimeout(0.5)

    with open(out_path, "wb") as f:
        f.write(PCAP_GLOBAL_HEADER)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                pkt = sock.recv(4096)
            except TimeoutError:
                continue
            if len(pkt) < 14:
                continue
            ts = time.time()
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            f.write(struct.pack("<IIII", sec, usec, len(pkt), len(pkt)))
            f.write(pkt)
    sock.close()
    print(f"=== [3] Capture saved to {out_path} ===")


def main():
    if os.name != "posix" or os.geteuid() != 0:
        print("[ERROR] Run this script on Linux with sudo/root privileges.", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(description="Capture raw 802.11 frames to a pcap file.")
    ap.add_argument("--iface", required=True, help="Wireless interface name (e.g. wlan0)")
    ap.add_argument("--freq", type=int, default=2412, help="Channel center frequency in MHz (default 2412)")
    ap.add_argument("--duration", type=float, default=10.0, help="Capture duration in seconds (default 10)")
    ap.add_argument("--out", required=True, help="Output pcap file path on the target machine")
    ap.add_argument("--skip-setup", action="store_true", help="Assume interface already in monitor mode")
    args = ap.parse_args()

    if not args.skip_setup:
        setup_monitor_iface(args.iface, args.freq)

    capture_to_pcap(args.iface, args.duration, args.out)

if __name__ == "__main__":
    main()
