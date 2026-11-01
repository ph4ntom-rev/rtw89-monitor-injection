#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
"""
verify_monitor_diag.py

IEEE 802.11 diagnostic verification utility for rtw89 NL80211_IFTYPE_MONITOR:
  1. Places the target interface into mac80211 monitor mode on a
     cfg80211-permitted channel (default: 2412 MHz / Channel 1).
  2. Captures Radiotap + IEEE 802.11 frames via AF_PACKET and summarizes
     Management, Control, and Data subtype reception to verify R_AX_RX_FLTR_OPT
     and R_AX_*_FLTR configuration.
  3. Optionally (--diag-tx) transmits a single locally-administered (RFC 7042
     local bit) IEEE 802.11 Null-Data test frame (02:00:00:88:52:ae) at
     6.0 Mbps OFDM with IEEE80211_RADIOTAP_F_TX_NOACK to validate unassociated
     TX descriptor construction.
  4. Optionally (--restore-managed) returns the interface to managed station
     mode after diagnostics complete.
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import time


def run_cmd(cmd: list[str], check: bool = True):
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def setup_monitor_iface(iface: str, freq_mhz: int):
    print(f"=== [1] Configuring {iface} as NL80211_IFTYPE_MONITOR ({freq_mhz} MHz) ===")
    subprocess.run(
        ["nmcli", "device", "set", iface, "managed", "no"],
        check=False,
        capture_output=True,
    )
    run_cmd(["ip", "link", "set", iface, "down"])
    run_cmd(["iw", "dev", iface, "set", "type", "monitor"])
    run_cmd(["ip", "link", "set", iface, "up"])
    run_cmd(["iw", "dev", iface, "set", "freq", str(freq_mhz)])
    info = run_cmd(["iw", "dev", iface, "info"])
    for line in info.stdout.splitlines():
        print("   ", line)


def restore_managed_iface(iface: str):
    print(f"\n=== [Restore] Returning {iface} to managed mode ===")
    run_cmd(["ip", "link", "set", iface, "down"], check=False)
    run_cmd(["iw", "dev", iface, "set", "type", "managed"], check=False)
    run_cmd(["ip", "link", "set", iface, "up"], check=False)
    subprocess.run(
        ["nmcli", "device", "set", iface, "managed", "yes"],
        check=False,
        capture_output=True,
    )


def capture_radiotap_summary(iface: str, duration_sec: float) -> dict:
    print(f"\n=== [2] Capturing Radiotap IEEE 802.11 Frames ({duration_sec:.1f}s) ===")
    eth_p_all = 0x0003
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(eth_p_all))
    sock.bind((iface, 0))
    sock.settimeout(0.5)

    counts = {"mgmt": 0, "ctrl": 0, "data": 0, "other": 0, "total": 0}
    bssid_set = set()
    deadline = time.monotonic() + duration_sec

    try:
        while time.monotonic() < deadline:
            try:
                pkt = sock.recv(4096)
            except TimeoutError:
                continue
            if len(pkt) < 14:
                continue

            ver, _pad, rt_len = struct.unpack_from("<BBH", pkt, 0)
            if ver != 0 or rt_len < 8 or len(pkt) < rt_len + 2:
                continue

            fc0 = pkt[rt_len]
            ftype = (fc0 >> 2) & 0x03
            counts["total"] += 1
            if ftype == 0:
                counts["mgmt"] += 1
                if len(pkt) >= rt_len + 22:
                    bssid_set.add(pkt[rt_len + 16:rt_len + 22].hex(":"))
            elif ftype == 1:
                counts["ctrl"] += 1
            elif ftype == 2:
                counts["data"] += 1
                if len(pkt) >= rt_len + 22:
                    bssid_set.add(pkt[rt_len + 16:rt_len + 22].hex(":"))
            else:
                counts["other"] += 1
    finally:
        sock.close()

    print(f"  Total Radiotap frames : {counts['total']}")
    print(f"    Management (type 0) : {counts['mgmt']}")
    print(f"    Control    (type 1) : {counts['ctrl']}")
    print(f"    Data       (type 2) : {counts['data']}")
    print(f"    Distinct BSSID/RA   : {len(bssid_set)}")
    return counts


def send_diagnostic_null_frame(iface: str):
    """
    Transmit a single locally-administered IEEE 802.11 Null-Data test frame
    (02:00:00:88:52:ae -> 02:00:00:88:52:ae) with Radiotap 6.0 Mbps & NO_ACK.
    """
    print("\n=== [3] Transmitting Single Diagnostic Null-Data Frame ===")
    # 12-byte Radiotap header:
    #   it_present = BIT(2) RATE | BIT(15) TX_FLAGS = 0x00008004
    #   rate       = 12 (6.0 Mbps in 500 kbps units)
    #   pad        = 0
    #   tx_flags   = 0x0008 (IEEE80211_RADIOTAP_F_TX_NOACK)
    radiotap = struct.pack("<BBHI BBH", 0, 0, 12, 0x00008004, 12, 0, 0x0008)

    local_test_mac = bytes.fromhex("0200008852ae")
    dot11_null = struct.pack(
        "<HH6s6s6sH",
        0x0048,          # Frame Control: Data / Null (no payload)
        0x0000,          # Duration
        local_test_mac,  # Addr1 (RA - locally administered diagnostic MAC)
        local_test_mac,  # Addr2 (TA - locally administered diagnostic MAC)
        local_test_mac,  # Addr3 (BSSID - locally administered diagnostic MAC)
        0x0010,          # Sequence=1, Fragment=0
    )

    frame = radiotap + dot11_null
    eth_p_all = 0x0003
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(eth_p_all))
    try:
        sock.bind((iface, 0))
        sent = sock.send(frame)
        print(f"  [OK] Submitted {sent} bytes (12B Radiotap + 24B IEEE 802.11 Null-Data).")
    finally:
        sock.close()


def main():
    if os.name != "posix" or os.geteuid() != 0:
        print("[ERROR] Run this diagnostic tool on Linux with root privileges.", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="IEEE 802.11 Radiotap monitor & diagnostic TX verifier for rtw89"
    )
    ap.add_argument("--iface", required=True, help="Wireless interface name (e.g. wlan0)")
    ap.add_argument("--freq", type=int, default=2412, help="Channel center frequency in MHz (default: 2412)")
    ap.add_argument("--duration", type=float, default=5.0, help="RX capture duration in seconds (default: 5.0)")
    ap.add_argument("--skip-setup", action="store_true", help="Assume interface is already in monitor mode")
    ap.add_argument("--diag-tx", action="store_true", help="Transmit 1 local-MAC IEEE 802.11 Null-Data test frame")
    ap.add_argument("--restore-managed", action="store_true", help="Restore interface to managed mode on exit")
    args = ap.parse_args()

    try:
        if not args.skip_setup:
            setup_monitor_iface(args.iface, args.freq)
        capture_radiotap_summary(args.iface, args.duration)
        if args.diag_tx:
            send_diagnostic_null_frame(args.iface)
    finally:
        if args.restore_managed:
            restore_managed_iface(args.iface)


if __name__ == "__main__":
    main()
