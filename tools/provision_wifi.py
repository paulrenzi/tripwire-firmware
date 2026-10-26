#!/usr/bin/env python3
"""Provision detector Wi-Fi over USB without printing/storing the password."""

import argparse
import getpass
from pathlib import Path
import re
import time
import serial


def quote(value):
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("credentials must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def command(device, text, expected):
    device.reset_input_buffer()
    device.write((text + "\n").encode())
    device.flush()
    end = time.monotonic() + 8
    received = bytearray()
    while time.monotonic() < end:
        received.extend(device.read(4096))
        if expected.encode() in received:
            return
        if b"Error:" in received:
            break
    # Serial contains echoed credentials. Never include it in exceptions/logs.
    raise RuntimeError(
        "Device did not confirm the setting; check firmware and USB connection."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM1")
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--address-file")
    args = parser.parse_args()
    password = getpass.getpass(f"Wi-Fi password for {args.ssid}: ")
    if not 1 <= len(args.ssid.encode()) <= 32:
        raise SystemExit("SSID must be 1–32 bytes")
    if password and not 8 <= len(password.encode()) <= 63:
        raise SystemExit(
            "WPA2 password must be 8–63 bytes (or empty for an open network)"
        )
    with serial.Serial(args.port, 115200, timeout=0.2) as device:
        # Wake the REPL; discard all echoed output, including the password.
        device.write(b"\n")
        time.sleep(0.5)
        command(
            device,
            "set wifi_ssid " + quote(args.ssid),
            "Wi-Fi setting wifi_ssid: ESP_OK",
        )
        command(
            device,
            "set wifi_pass " + quote(password),
            "Wi-Fi setting wifi_pass: ESP_OK",
        )
        password = None
        device.reset_input_buffer()
        device.write(b"reboot\n")
        print(
            "Credentials stored on the XIAO. Waiting for its LAN address…", flush=True
        )
        received = bytearray()
        end = time.monotonic() + 60
        while time.monotonic() < end:
            received.extend(device.read(4096))
            match = re.search(rb"Receiver address: ([0-9.]+):8765", received)
            if match:
                address = match[1].decode()
                print(f"Connected. Device IP: {address}", flush=True)
                if args.address_file:
                    Path(args.address_file).write_text(address + "\n")
                return
            if len(received) > 32768:
                del received[:-16384]
        raise SystemExit(
            "No LAN address received. Check the SSID/password and 2.4 GHz availability."
        )


if __name__ == "__main__":
    main()
