"""
Cortex MCP Bridge Server

Connects to Cortex Link (ESP32) via USB serial and provides MCP tools
for AI agents to communicate with Cortex Core (Pi Zero 2 W) over BLE.

Data flow:
    AI Agent -> MCP Server -> USB Serial -> Cortex Link -> BLE -> Cortex Core

Protocol:
    Commands:  CMD:<command>:<json_payload>
    Responses: RSP:<command>:<json_payload>
    Acks:      ACK:<command>:<id>
    Errors:    ERR:<command>:<message>

Configuration (environment variables):
    KEYMASTER_PORT    Serial port (e.g. COM5, /dev/ttyACM0). Auto-detects if unset.
    KEYMASTER_BAUD    Baud rate (default: 115200)
    KEYMASTER_TIMEOUT Response timeout in seconds (default: 5)
"""

import os
import sys
import json
import time
import socket
import platform
import threading
from collections import deque

import serial
import serial.tools.list_ports
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PORT = os.environ.get("KEYMASTER_PORT", "")
_BAUD = int(os.environ.get("KEYMASTER_BAUD", "115200"))
_TIMEOUT = float(os.environ.get("KEYMASTER_TIMEOUT", "5"))

# ESP32-S3 USB-CDC vendor ID
_ESP32_S3_VID = 0x303A

# ---------------------------------------------------------------------------
# Serial bridge (computer <-> ESP32 USB)
# ---------------------------------------------------------------------------


class _SerialBridge:
    """Manages the serial connection to the ESP32 and background reading."""

    def __init__(self):
        self._ser = None
        self._lock = threading.Lock()
        self._rx_queue = deque(maxlen=500)
        self._reader_thread = None

    @property
    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    def connect(self, port=None, baud=None):
        """Open the serial port. Auto-detects ESP32 if port is None."""
        if self.is_connected:
            return

        port = port or _PORT or self._find_port()
        baud = baud or _BAUD
        if not port:
            raise ConnectionError(
                "Cortex Link (ESP32) not found. Set KEYMASTER_PORT env var "
                "(e.g. COM5 or /dev/ttyACM0) or plug in the device."
            )

        self._ser = serial.Serial(port, baud, timeout=0.1)
        # Give ESP32 bridge a moment to settle after port open
        time.sleep(0.5)
        self._ser.reset_input_buffer()

        # Start background reader
        if self._reader_thread is None or not self._reader_thread.is_alive():
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def send(self, message):
        """Send a newline-delimited message (no size limit — ESP32 handles chunking)."""
        self._ensure_connected()
        if not message.endswith("\n"):
            message += "\n"
        encoded = message.encode("utf-8")
        try:
            with self._lock:
                self._ser.write(encoded)
        except (serial.SerialException, PermissionError, OSError):
            # Stale handle after device reboot — reconnect and retry
            self._reconnect()
            with self._lock:
                self._ser.write(encoded)

    def send_and_wait(self, message, timeout=None, settle=0.4):
        """Send a message and collect response lines.

        After the first response line arrives, waits an additional `settle`
        seconds for more lines before returning.
        """
        timeout = timeout or _TIMEOUT
        t0 = time.time()

        # Drain stale messages
        self._rx_queue.clear()

        self.send(message)

        lines = []
        deadline = t0 + timeout
        first_at = None

        while time.time() < deadline:
            # Collect new messages
            while self._rx_queue:
                ts, text = self._rx_queue.popleft()
                if ts >= t0:
                    lines.append(text)
                    if first_at is None:
                        first_at = time.time()

            # If we have responses and settle window elapsed, done
            if first_at is not None and (time.time() - first_at) >= settle:
                # Final drain
                while self._rx_queue:
                    ts, text = self._rx_queue.popleft()
                    if ts >= t0:
                        lines.append(text)
                break

            time.sleep(0.05)

        return lines

    def read_pending(self):
        """Return all buffered messages without sending anything."""
        lines = []
        while self._rx_queue:
            _ts, text = self._rx_queue.popleft()
            lines.append(text)
        return lines

    @property
    def port_name(self):
        if self._ser:
            return self._ser.port
        return None

    @property
    def buffered_count(self):
        return len(self._rx_queue)

    # -- internals --

    def _reconnect(self):
        """Close stale connection and reopen."""
        port = self.port_name
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        time.sleep(1.0)
        self.connect(port=port)

    def _ensure_connected(self):
        if not self.is_connected:
            self.connect()

    def _find_port(self):
        """Auto-detect an ESP32-S3 USB-CDC port."""
        for p in serial.tools.list_ports.comports():
            if p.vid == _ESP32_S3_VID:
                return p.device
            if p.description and "ESP32" in p.description.upper():
                return p.device
        return None

    def _reader_loop(self):
        """Background thread: read serial lines into the queue."""
        buf = b""
        while True:
            try:
                if self._ser and self._ser.is_open:
                    chunk = self._ser.read(512)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            idx = buf.index(b"\n")
                            line = buf[:idx].decode("utf-8", errors="replace").strip()
                            buf = buf[idx + 1:]
                            if line:
                                self._rx_queue.append((time.time(), line))
                else:
                    time.sleep(0.2)
            except (serial.SerialException, PermissionError, OSError):
                # Port disconnected or device rebooted — try to reconnect
                buf = b""
                try:
                    self._reconnect()
                except Exception:
                    time.sleep(2.0)
            except Exception:
                time.sleep(0.5)


# Singleton bridge instance
_bridge = _SerialBridge()

# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------


def _cmd(command, payload=None):
    """Build a Cortex protocol command string."""
    if payload is None:
        return "CMD:{}".format(command)
    if isinstance(payload, dict):
        payload = json.dumps(payload)
    return "CMD:{}:{}".format(command, payload)


def _parse_response(lines):
    """Parse Cortex protocol response lines into a structured result.

    Returns a dict with 'type' (ACK/RSP/ERR/raw), 'command', 'data', and 'raw'.
    """
    if not lines:
        return None

    raw = "\n".join(lines)

    for line in lines:
        if line.startswith("ACK:"):
            parts = line.split(":", 2)
            return {
                "type": "ACK",
                "command": parts[1] if len(parts) > 1 else "",
                "data": parts[2] if len(parts) > 2 else "",
                "raw": raw,
            }
        if line.startswith("RSP:"):
            parts = line.split(":", 2)
            data_str = parts[2] if len(parts) > 2 else ""
            # Try to parse JSON payload
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                data = data_str
            return {
                "type": "RSP",
                "command": parts[1] if len(parts) > 1 else "",
                "data": data,
                "raw": raw,
            }
        if line.startswith("ERR:"):
            parts = line.split(":", 2)
            return {
                "type": "ERR",
                "command": parts[1] if len(parts) > 1 else "",
                "data": parts[2] if len(parts) > 2 else "",
                "raw": raw,
            }

    # No recognized prefix — return raw
    return {"type": "raw", "command": "", "data": raw, "raw": raw}


def _send_cmd(command, payload=None, timeout=None):
    """Send a Cortex command and return parsed response."""
    msg = _cmd(command, payload)
    lines = _bridge.send_and_wait(msg, timeout=timeout)
    resp = _parse_response(lines)
    if resp is None:
        return "No response (timeout). Check Cortex Link and Core are connected."
    if resp["type"] == "ERR":
        return "Error from Core: {}".format(resp["data"])
    if resp["type"] == "ACK":
        return "ACK (id: {})".format(resp["data"])
    if resp["type"] == "RSP":
        data = resp["data"]
        if isinstance(data, dict):
            return json.dumps(data, indent=2)
        return str(data)
    return resp["raw"]


# ---------------------------------------------------------------------------
# MCP server and tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Cortex Bridge",
    instructions=(
        "Bridge to Cortex Core (Pi Zero 2 W wearable) via Cortex Link (ESP32 BLE). "
        "Use these tools to log activities, notes, searches, and manage sessions. "
        "The Core stores all data in a local SQLite database. "
        "Start each conversation with get_context to load previous context."
    ),
)


@mcp.tool()
def ping() -> str:
    """Ping the Pi Zero to test round-trip connectivity.

    Sends CMD:ping through the ESP32 BLE bridge and waits for CMD:pong.
    Use this to verify the full chain: Computer -> ESP32 -> BLE -> Pi.
    """
    try:
        lines = _bridge.send_and_wait("CMD:ping", timeout=5)
        if lines:
            return "Response: " + " | ".join(lines)
        return "No response (timeout). Check Cortex Link and Core are connected."
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def get_status() -> str:
    """Get the Pi Zero's current status.

    Returns uptime, connection info, storage stats, and recording state.
    """
    try:
        return _send_cmd("status", timeout=5)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def send_note(content: str, tags: str = "", project: str = "", note_type: str = "note") -> str:
    """Send a text note to the Pi Zero for storage.

    Notes are timestamped and stored on the Pi's SD card for future analysis.

    Args:
        content: The note text to store.
        tags: Optional comma-separated tags for categorization
              (e.g. "idea,project,urgent").
        project: Optional project tag (e.g. "cortex", "bewell").
        note_type: Note type: note, decision, bug, reminder, idea, todo, context.
    """
    try:
        payload = {"content": content}
        if tags:
            payload["tags"] = tags
        if project:
            payload["project"] = project
        if note_type and note_type != "note":
            payload["type"] = note_type
        return _send_cmd("note", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def log_activity(program: str, details: str = "", file_path: str = "", project: str = "") -> str:
    """Log what the user is currently working on.

    Records the program, optional file path, and details to the Pi for
    building an activity timeline.

    Args:
        program: Program name (e.g. "VS Code", "Chrome", "Terminal").
        details: Optional description of the activity.
        file_path: Optional file path being worked on.
        project: Optional project tag.
    """
    try:
        payload = {"program": program}
        if details:
            payload["details"] = details
        if file_path:
            payload["file_path"] = file_path
        if project:
            payload["project"] = project
        return _send_cmd("activity", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def log_search(query: str, url: str = "", source: str = "web", project: str = "") -> str:
    """Log a web search or research query.

    Records searches for building a research history on the Pi.

    Args:
        query: The search query text.
        url: Optional URL of the search or result page.
        source: Search engine or source (e.g. "google", "github", "stackoverflow").
        project: Optional project tag.
    """
    try:
        payload = {"query": query, "source": source}
        if url:
            payload["url"] = url
        if project:
            payload["project"] = project
        return _send_cmd("search", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def session_start(ai_platform: str = "claude") -> str:
    """Start a new Cortex session.

    Call this at the beginning of a conversation to register the session
    with Cortex Core. Returns a session_id for use in subsequent calls.

    Args:
        ai_platform: The AI platform name (e.g. "claude", "chatgpt").
    """
    try:
        payload = {
            "ai_platform": ai_platform,
            "hostname": socket.gethostname(),
            "os_info": "{} {}".format(platform.system(), platform.release()),
        }
        return _send_cmd("session_start", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def session_end(session_id: str, summary: str, projects: str = "") -> str:
    """End a Cortex session.

    Call this before a conversation ends to record what was accomplished.

    Args:
        session_id: The session ID from session_start.
        summary: Brief summary of what was accomplished in this session.
        projects: Comma-separated project tags that were touched.
    """
    try:
        payload = {
            "session_id": session_id,
            "summary": summary,
        }
        if projects:
            payload["projects"] = projects
        return _send_cmd("session_end", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def get_context() -> str:
    """Get full context for starting an informed AI session.

    Returns active projects, recent sessions, pending reminders,
    recent decisions, open bugs, and computer info. Call this at the
    start of every conversation to understand what the user is working on.
    """
    try:
        return _send_cmd("get_context", timeout=20)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def query(table: str, filters: str = "", limit: int = 10, order_by: str = "created_at DESC") -> str:
    """Query the Cortex database on the Pi.

    Generic query interface for retrieving stored data.

    Args:
        table: Table to query (notes, activities, searches, sessions, projects, computers, people).
        filters: JSON string of filters, e.g. '{"project":"cortex","type":"bug"}'.
        limit: Max results to return (default 10).
        order_by: SQL ORDER BY clause (default "created_at DESC").
    """
    try:
        payload = {"table": table, "limit": limit, "order_by": order_by}
        if filters:
            try:
                payload["filters"] = json.loads(filters)
            except (json.JSONDecodeError, ValueError):
                return "Error: 'filters' must be valid JSON (e.g. '{\"project\":\"cortex\"}')"
        return _send_cmd("query", payload, timeout=10)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def register_computer() -> str:
    """Register this computer with Cortex Core.

    Auto-detects hostname, OS, platform, and Python version.
    Useful for tracking which machines the user works on.
    """
    try:
        payload = {
            "hostname": socket.gethostname(),
            "os_info": "{} {} {}".format(
                platform.system(), platform.release(), platform.version()
            ),
            "platform": platform.machine(),
        }
        return _send_cmd("computer_reg", payload)
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def send_message(message: str) -> str:
    """Send an arbitrary message to the Pi Zero through the bridge.

    Use for custom commands or data not covered by other tools.
    Messages are newline-delimited UTF-8, max 512 bytes.

    Args:
        message: The message to send.
    """
    try:
        lines = _bridge.send_and_wait(message, timeout=5)
        if lines:
            return "\n".join(lines)
        return "Sent (no response)."
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def read_responses() -> str:
    """Read any pending messages from the Pi Zero.

    Returns buffered messages that arrived without a preceding request.
    Useful for checking unsolicited data or async responses.
    """
    try:
        _bridge._ensure_connected()
        lines = _bridge.read_pending()
        if lines:
            return "\n".join(lines)
        return "No pending messages."
    except Exception as e:
        return "Error: {}".format(e)


@mcp.tool()
def connection_info() -> str:
    """Show current serial connection status and available ports.

    Lists detected serial ports and the active connection details.
    """
    try:
        # List available ports
        ports = serial.tools.list_ports.comports()
        port_list = []
        for p in ports:
            desc = p.description or "unknown"
            vid = "VID={:04X}".format(p.vid) if p.vid else ""
            port_list.append("{} - {} {}".format(p.device, desc, vid).strip())

        info = "Available ports:\n"
        if port_list:
            info += "\n".join("  " + p for p in port_list)
        else:
            info += "  (none detected)"

        info += "\n\n"

        if _bridge.is_connected:
            info += "Connected: {}\n".format(_bridge.port_name)
            info += "Baud: {}\n".format(_BAUD)
            info += "Buffered messages: {}".format(_bridge.buffered_count)
        else:
            info += "Status: Not connected"
            auto = _bridge._find_port()
            if auto:
                info += "\nAuto-detected ESP32: {}".format(auto)

        return info
    except Exception as e:
        return "Error: {}".format(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
