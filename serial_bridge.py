"""
USB Serial <-> BLE bridge with Cortex chunking protocol.

Reads newline-delimited lines from USB-CDC (sys.stdin) via select.poll()
and forwards them as BLE TX notifications. Receives BLE RX writes and
writes them to USB-CDC (sys.stdout).

Chunking (CHUNK:n/N:data):
  - Outbound (PC -> Core): messages > _CHUNK_PAYLOAD bytes are split into
    numbered chunks with CHUNK:n/N: prefix.
  - Inbound (Core -> PC): chunked BLE notifications are reassembled into
    complete messages before forwarding over USB serial.
"""

import sys
import select
import asyncio
import time

_CHUNK_SIZE = 200        # Safe BLE notification MTU chunk size
_CHUNK_PAYLOAD = _CHUNK_SIZE - 1  # Messages > this use CHUNK:n/N: protocol
_POLL_TIMEOUT_MS = 10    # select.poll() timeout (low to avoid blocking BLE rx)
_BUF_OVERFLOW = 4096     # Clear buffer if this big without \n
_CHUNK_TIMEOUT_MS = 15000 # Discard incomplete chunk sequences after 15s


class SerialBridge:
    def __init__(self, ble_server, on_activity=None):
        """
        Args:
            ble_server: BLEServer instance for BLE TX.
            on_activity: Optional callback(direction, message) for display/LED.
                         direction is "serial_in" or "ble_in".
        """
        self._ble = ble_server
        self._on_activity = on_activity
        self._poll = select.poll()
        self._poll.register(sys.stdin, select.POLLIN)
        self._buf = bytearray()

        # Inbound chunk reassembly state
        self._chunk_buf = {}    # {chunk_num: data_bytes}
        self._chunk_total = 0
        self._chunk_ts = 0      # Timestamp of first chunk received
        print("Bridge: initialized")

    def on_ble_receive(self, server, message, connection):
        """BLE RX -> USB Serial. Wired as BLEServer's on_receive callback."""
        # Check for chunked message from Core
        if message.startswith("CHUNK:"):
            self._handle_inbound_chunk(message)
            return

        line = message if message.endswith("\n") else message + "\n"
        sys.stdout.write(line)
        if self._on_activity:
            try:
                self._on_activity("ble_in", message.rstrip("\n"))
            except Exception:
                pass

    def _handle_inbound_chunk(self, message):
        """Reassemble CHUNK:n/N:data messages from Core."""
        try:
            # Parse CHUNK:n/N:data
            rest = message[6:]  # Strip "CHUNK:"
            header, data = rest.split(":", 1)
            n_str, total_str = header.split("/")
            n = int(n_str)
            total = int(total_str)
        except (ValueError, IndexError):
            # Malformed chunk — forward as raw
            line = "RAW:" + message + "\n"
            sys.stdout.write(line)
            return

        now = time.ticks_ms()

        # New sequence or different total — reset
        if total != self._chunk_total or (self._chunk_ts and
                time.ticks_diff(now, self._chunk_ts) > _CHUNK_TIMEOUT_MS):
            self._chunk_buf = {}
            self._chunk_total = total
            self._chunk_ts = now

        if not self._chunk_ts:
            self._chunk_ts = now

        self._chunk_buf[n] = data

        # Check if we have all chunks
        if len(self._chunk_buf) == total:
            # Reassemble in order
            full_msg = ""
            for i in range(1, total + 1):
                full_msg += self._chunk_buf.get(i, "")
            self._chunk_buf = {}
            self._chunk_total = 0
            self._chunk_ts = 0

            line = full_msg if full_msg.endswith("\n") else full_msg + "\n"
            sys.stdout.write(line)
            if self._on_activity:
                try:
                    self._on_activity("ble_in", full_msg.rstrip("\n")[:40])
                except Exception:
                    pass

    async def run(self):
        """USB Serial -> BLE TX async loop. Add to asyncio.gather()."""
        print("Bridge: serial reader started")
        # Flush any boot output that accumulated in stdin
        await asyncio.sleep_ms(500)
        while self._poll.poll(0):
            try:
                sys.stdin.read(1)
            except Exception:
                break
        self._buf = bytearray()
        print("Bridge: stdin flushed, ready")
        while True:
            events = self._poll.poll(_POLL_TIMEOUT_MS)
            if events:
                self._read_available()
            self._process_buffer()
            # Check for stale inbound chunks
            self._check_chunk_timeout()
            await asyncio.sleep_ms(0)  # Yield to event loop

    def _read_available(self):
        """Read all available bytes from stdin into the buffer."""
        while True:
            try:
                ch = sys.stdin.read(1)
                if ch:
                    if isinstance(ch, str):
                        self._buf.extend(ch.encode("utf-8"))
                    else:
                        self._buf.extend(ch)
                else:
                    break
            except Exception:
                break
            # Check if more data waiting
            if not self._poll.poll(0):
                break

    def _process_buffer(self):
        """Extract complete newline-delimited lines and send via BLE."""
        # Safety: prevent runaway buffer
        if len(self._buf) > _BUF_OVERFLOW and b"\n" not in self._buf:
            print("Bridge: buffer overflow, clearing")
            self._buf = bytearray()
            return

        while b"\n" in self._buf:
            idx = self._buf.index(b"\n")
            line_bytes = bytes(self._buf[:idx])  # Exclude \n for chunking check
            self._buf = self._buf[idx + 1:]

            if len(line_bytes) > _CHUNK_PAYLOAD:
                self._send_chunked(line_bytes)
            else:
                # Send with \n included as single message
                self._send_ble(line_bytes + b"\n")

    def _send_chunked(self, data_bytes):
        """Split a large message into CHUNK:n/N: formatted BLE writes.

        Each chunk is \n-terminated so the Pi processes them individually.
        Each chunk must fit in a single BLE notification (_CHUNK_SIZE bytes)
        to avoid notification coalescing/dropping.
        """
        if not self._ble.connected:
            return

        data_str = data_bytes.decode("utf-8", "replace")
        # Reserve space for header ("CHUNK:nn/nn:" max 12) + \n (1)
        chunk_data_size = _CHUNK_SIZE - 20  # 180 bytes of payload per chunk
        total = (len(data_str) + chunk_data_size - 1) // chunk_data_size

        if self._on_activity:
            try:
                self._on_activity("serial_in", "CHUNK 1/{} {}".format(
                    total, data_str[:30]))
            except Exception:
                pass

        for i in range(total):
            start = i * chunk_data_size
            end = min(start + chunk_data_size, len(data_str))
            # Every chunk gets \n so Pi can process each independently
            chunk_msg = "CHUNK:{}/{}:{}\n".format(i + 1, total, data_str[start:end])
            self._send_ble(chunk_msg.encode("utf-8"))
            # Delay between notifications to avoid BLE stack congestion
            if i < total - 1:
                time.sleep_ms(20)

    def _send_ble(self, data_bytes):
        """Send bytes as a single BLE TX notification.

        Data MUST fit in a single notification (<= _CHUNK_SIZE bytes).
        Messages exceeding this are handled by _send_chunked instead.
        Rapid consecutive notifications can be dropped by the BLE stack.
        """
        if not self._ble.connected:
            return

        if self._on_activity and not data_bytes.startswith(b"CHUNK:"):
            try:
                preview = data_bytes[:40].decode("utf-8", "replace").rstrip("\n")
                self._on_activity("serial_in", preview)
            except Exception:
                pass

        if len(data_bytes) > _CHUNK_SIZE:
            print("Bridge: WARNING notification {} > {} bytes".format(
                len(data_bytes), _CHUNK_SIZE))
        self._ble.send_raw(data_bytes)

    def _check_chunk_timeout(self):
        """Discard incomplete inbound chunk sequences after timeout."""
        if self._chunk_ts and self._chunk_buf:
            now = time.ticks_ms()
            if time.ticks_diff(now, self._chunk_ts) > _CHUNK_TIMEOUT_MS:
                print("Bridge: chunk timeout, discarding {} of {} chunks".format(
                    len(self._chunk_buf), self._chunk_total))
                err = "ERR:CHUNK_TIMEOUT:received {}/{} chunks\n".format(
                    len(self._chunk_buf), self._chunk_total)
                sys.stdout.write(err)
                self._chunk_buf = {}
                self._chunk_total = 0
                self._chunk_ts = 0
