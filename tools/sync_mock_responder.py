"""Desktop-side MOCK implementing sync contract v2 over the dongle's serial
port — the test target for the phone's sync engine until the real desktop
daemon implements sync forwarding.

Implements (see cortex-desktop/docs/SYNC_CONTRACT_DRAFT.md, v2 RATIFIED):
  CMD:ping         -> RSP:ping:{...}
  CMD:echo:<json>  -> RSP:echo:<json>
  CMD:sync_status  -> counts + newest per kind (from the source DB)
  CMD:sync_pull    -> real rows from a corpus SQLite, opaque cursor <kind>:<id>
  CMD:sync_push    -> uuid-deduped insert into a SANDBOX SQLite (never the
                      real corpus), v2 reply shape

Usage:
  python tools/sync_mock_responder.py COM5 \
      --corpus ../cortex-mobile/.realdata/phone_cortex.db \
      --sandbox ../cortex-mobile/.realdata/sync_sandbox.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time

import serial

PULL_KINDS = {
    "summaries_gist": ("g", ["id", "period_label", "body", "confidence", "created_at"]),
    "temporal_narratives": ("nar", ["id", "kind", "period_label", "period_start",
                                    "period_end", "narrative", "created_at"]),
}
PUSH_KINDS = {
    "human_journal_entries": ["text", "entry_type", "created_at"],
    "notes": ["content", "note_type", "project", "tags", "created_at"],
}

SANDBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_row_map (
  uuid TEXT PRIMARY KEY, kind TEXT, remote_id INTEGER, received_at TEXT);
CREATE TABLE IF NOT EXISTS human_journal_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, entry_type TEXT,
  created_at TEXT, device TEXT, uuid TEXT);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, note_type TEXT,
  project TEXT, tags TEXT, created_at TEXT, device TEXT, uuid TEXT);
"""


class SyncMock:
    def __init__(self, corpus_path: str, sandbox_path: str) -> None:
        self.corpus = sqlite3.connect(corpus_path)
        self.corpus.row_factory = sqlite3.Row
        self.sandbox = sqlite3.connect(sandbox_path)
        self.sandbox.executescript(SANDBOX_SCHEMA)

    def handle(self, command: str, payload: dict) -> str:
        if command == "sync_status":
            counts, newest = {}, {}
            for kind in PULL_KINDS:
                counts[kind] = self.corpus.execute(
                    f"SELECT count(*) FROM {kind}").fetchone()[0]
                row = self.corpus.execute(
                    f"SELECT created_at FROM {kind} ORDER BY id DESC LIMIT 1").fetchone()
                newest[kind] = row[0] if row else None
            return "RSP:sync_status:" + json.dumps(
                {"ok": True, "counts": counts, "newest": newest})

        if command == "sync_pull":
            kind = payload.get("kind", "summaries_gist")
            if kind not in PULL_KINDS:
                return f"ERR:sync_pull:unknown kind {kind}"
            prefix, cols = PULL_KINDS[kind]
            cursor = payload.get("cursor") or ""
            last_id = int(cursor.split(":")[1]) if cursor.startswith(prefix + ":") else 0
            limit = min(int(payload.get("limit", 10)), 50)
            rows = self.corpus.execute(
                f"SELECT {', '.join(cols)} FROM {kind} WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, limit)).fetchall()
            out = [dict(r) for r in rows]
            more = bool(out) and self.corpus.execute(
                f"SELECT 1 FROM {kind} WHERE id > ? LIMIT 1",
                (out[-1]["id"],)).fetchone() is not None
            return "RSP:sync_pull:" + json.dumps({
                "ok": True, "kind": kind, "rows": out, "more": more,
                "next_cursor": f"{prefix}:{out[-1]['id']}" if out else cursor})

        if command == "sync_push":
            kind = payload.get("kind", "")
            if kind not in PUSH_KINDS:
                return f"ERR:sync_push:unknown kind {kind}"
            device = payload.get("device", "unknown")
            cols = PUSH_KINDS[kind]
            accepted, dupes, ids, rejected = 0, 0, {}, []
            for row in payload.get("rows", []):
                uid = row.get("id")
                if not uid:
                    rejected.append({"id": None, "reason": "missing uuid id"})
                    continue
                existing = self.sandbox.execute(
                    "SELECT remote_id FROM sync_row_map WHERE uuid = ?", (uid,)).fetchone()
                if existing:
                    dupes += 1
                    ids[uid] = existing[0]
                    continue
                vals = [row.get(c) for c in cols]
                cur = self.sandbox.execute(
                    f"INSERT INTO {kind} ({', '.join(cols)}, device, uuid) "
                    f"VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                    (*vals, device, uid))
                self.sandbox.execute(
                    "INSERT INTO sync_row_map (uuid, kind, remote_id, received_at) "
                    "VALUES (?,?,?,datetime('now'))", (uid, kind, cur.lastrowid))
                ids[uid] = cur.lastrowid
                accepted += 1
            self.sandbox.commit()
            return "RSP:sync_push:" + json.dumps({
                "ok": True, "kind": kind, "accepted": accepted,
                "dupes": dupes, "rejected": rejected, "ids": ids})

        return f"ACK:{command}:received-by-sync-mock"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--sandbox", required=True)
    args = ap.parse_args()

    mock = SyncMock(args.corpus, args.sandbox)
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    print(f"[sync-mock] v2 contract live on {args.port}")

    buf = b""
    while True:
        chunk = ser.read(512)
        if not chunk:
            time.sleep(0.05)
            continue
        buf += chunk
        while b"\n" in buf:
            raw, _, buf = buf.partition(b"\n")
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] << {line[:120]}")
            if not line.startswith("CMD:"):
                continue  # dongle debug chatter
            parts = line.split(":", 2)
            command = parts[1] if len(parts) > 1 else ""
            try:
                payload = json.loads(parts[2]) if len(parts) > 2 and parts[2] else {}
            except json.JSONDecodeError:
                ser.write(f"ERR:{command}:bad json\n".encode())
                continue
            if command == "ping":
                reply = "RSP:ping:" + json.dumps(
                    {"ok": True, "host": "desktop", "via": "cortex-link", "mock": "sync-v2"})
            elif command == "echo":
                reply = "RSP:echo:" + (parts[2] if len(parts) > 2 else "{}")
            else:
                reply = mock.handle(command, payload)
            ser.write((reply + "\n").encode("utf-8"))
            ser.flush()
            print(f"[{stamp}] >> {reply[:120]}")


if __name__ == "__main__":
    main()
