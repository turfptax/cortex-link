"""Desktop-side test responder for the Cortex Link BLE bridge.

Sits on the dongle's USB-CDC serial port and answers the protocol lines that
arrive from the phone (phone -> BLE -> ESP32 -> this serial port):

  CMD:ping            -> RSP:ping:{"ok": true, "host": "desktop", "via": "cortex-link"}
  CMD:echo:<json>     -> RSP:echo:<same json>
  anything else       -> logged (and ACK'd) so we can see it arrived

Run:  python tools/serial_ping_responder.py COM5
Stop: Ctrl+C
"""
from __future__ import annotations

import json
import sys
import time

import serial  # pyserial


def main(port: str) -> None:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200  # ignored by USB-CDC but required by pyserial
    ser.timeout = 0.2
    # Avoid toggling the auto-reset lines on open where possible.
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"[responder] listening on {port} — waiting for lines from the dongle…")

    buf = b""
    while True:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] << {line}")
                reply = None
                if line == "CMD:ping" or line.startswith("CMD:ping:"):
                    reply = "RSP:ping:" + json.dumps(
                        {"ok": True, "host": "desktop", "via": "cortex-link"})
                elif line.startswith("CMD:echo:"):
                    reply = "RSP:echo:" + line[len("CMD:echo:"):]
                elif line.startswith("CMD:"):
                    cmd = line.split(":", 2)[1]
                    reply = "ACK:" + cmd + ":received-by-desktop"
                if reply:
                    ser.write((reply + "\n").encode("utf-8"))
                    ser.flush()
                    print(f"[{stamp}] >> {reply}")
        else:
            time.sleep(0.05)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python serial_ping_responder.py <COM port>")
        sys.exit(2)
    try:
        main(sys.argv[1])
    except KeyboardInterrupt:
        print("\n[responder] stopped")
