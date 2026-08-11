#!/usr/bin/env python3
"""Authenticated single-writer gateway for ENDMEMEX remote mutations.

One designated writer host owns the memory database. Remote callers enqueue an
exact CLI request in a host-specific outbox and submit it over HTTPS. The server
persists an idempotency receipt before dispatch, so a retry never executes the
same key twice. A receipt left ``processing`` after a crash is deliberately
not retried automatically; an operator must inspect it and submit a new key.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import http.server
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from config import ROOT

HERE = Path(__file__).resolve().parent
DB_SCRIPT = HERE / "endeavor_db.py"
TOKEN_ENV = "ENDMEMEX_GATEWAY_TOKEN"
URL_ENV = "ENDMEMEX_GATEWAY_URL"
DEFAULT_RECEIPTS = HERE / ".write_gateway" / "receipts.sqlite3"
DEFAULT_OUTBOX_DIR = HERE / ".write_gateway" / "outbox"
MAX_BODY_BYTES = 256 * 1024
COMMAND_TIMEOUT_S = 90
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
ALLOWED_COMMANDS = {
    "checkpoint", "pin-checkpoint", "unpin-checkpoint", "record-add",
    "record-update", "record-link", "session-start", "session-close",
    "feedback", "event-add", "event-ack", "presence-start",
    "presence-heartbeat", "presence-stop",
}


def now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def required_token() -> str:
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < 32:
        raise ValueError(f"{TOKEN_ENV} must contain at least 32 characters")
    return token


def connect_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def initialize_receipts(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS gateway_receipts (
        idempotency_key TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        arguments TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
        exit_code INTEGER,
        response TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    conn.commit()


def initialize_outbox(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS gateway_outbox (
        idempotency_key TEXT PRIMARY KEY,
        request TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued', 'delivered')),
        response TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    conn.commit()


def has_option(arguments: list[str], option: str) -> bool:
    """Match a long option in either argparse spelling without prefix confusion."""
    return any(argument == option or argument.startswith(f"{option}=") for argument in arguments)


def validate_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    unknown = set(payload) - {"idempotency_key", "command", "arguments"}
    if unknown:
        raise ValueError(f"unknown request fields: {', '.join(sorted(unknown))}")
    key, command, arguments = (
        payload.get("idempotency_key"), payload.get("command"), payload.get("arguments"),
    )
    if not isinstance(key, str) or not IDEMPOTENCY_RE.fullmatch(key):
        raise ValueError("idempotency_key must be 8-128 safe characters")
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"command is not gateway-allowlisted: {command}")
    if not isinstance(arguments, list) or len(arguments) > 100 or not all(
        isinstance(item, str) and len(item) <= 10_000 for item in arguments
    ):
        raise ValueError("arguments must be an array of at most 100 bounded strings")
    if has_option(arguments, "--db"):
        raise ValueError("--db is forbidden; the gateway always writes the Main-Mac database")
    if command == "record-add" and has_option(arguments, "--content-file"):
        raise ValueError("--content-file is forbidden at the gateway boundary; send --content")
    if command == "checkpoint" and has_option(arguments, "--payload"):
        raise ValueError("checkpoint --payload is forbidden at the gateway boundary; send explicit fields")
    return {"idempotency_key": key, "command": command, "arguments": arguments}


def request_hash(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dispatch(request: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(DB_SCRIPT), request["command"], *request["arguments"]],
        cwd=ROOT, text=True, capture_output=True, timeout=COMMAND_TIMEOUT_S,
    )
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def process_request(conn: sqlite3.Connection, payload: object) -> tuple[int, dict[str, Any]]:
    request = validate_request(payload)
    digest = request_hash(request)
    timestamp = now_utc()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM gateway_receipts WHERE idempotency_key = ?",
            (request["idempotency_key"],),
        ).fetchone()
        if existing is not None:
            conn.commit()
            if existing["request_hash"] != digest:
                return 409, {"error": "idempotency key reused with a different request"}
            response = json.loads(existing["response"]) if existing["response"] else None
            code = 202 if existing["status"] == "processing" else 200
            return code, {
                "idempotency_key": existing["idempotency_key"],
                "status": existing["status"], "replayed": True, "response": response,
            }
        conn.execute(
            """INSERT INTO gateway_receipts(
                   idempotency_key, command, arguments, request_hash, status, created_at, updated_at
               ) VALUES(?, ?, ?, ?, 'processing', ?, ?)""",
            (
                request["idempotency_key"], request["command"],
                json.dumps(request["arguments"], ensure_ascii=False), digest, timestamp, timestamp,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    try:
        response = dispatch(request)
    except (OSError, subprocess.TimeoutExpired) as exc:
        response = {"exit_code": 124 if isinstance(exc, subprocess.TimeoutExpired) else 126,
                    "stdout": "", "stderr": str(exc)}
    status = "completed" if response["exit_code"] == 0 else "failed"
    conn.execute(
        """UPDATE gateway_receipts SET status = ?, exit_code = ?, response = ?, updated_at = ?
           WHERE idempotency_key = ?""",
        (status, response["exit_code"], json.dumps(response, ensure_ascii=False), now_utc(), request["idempotency_key"]),
    )
    conn.commit()
    return 200, {
        "idempotency_key": request["idempotency_key"], "status": status,
        "replayed": False, "response": response,
    }


class GatewayHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ENDMEMEXWriteGateway/1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.gateway_token}"  # type: ignore[attr-defined]
        return hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "writer": getpass.getuser()})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/write":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("invalid request body size")
            payload = json.loads(self.rfile.read(length))
            conn = connect_store(self.server.receipts_path)  # type: ignore[attr-defined]
            try:
                initialize_receipts(conn)
                status, response = process_request(conn, payload)
            finally:
                conn.close()
            self._json(status, response)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"gateway failure: {exc}"})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[write-gateway] {self.address_string()} {fmt % args}", file=sys.stderr)


def outbox_path() -> Path:
    machine = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())
    return DEFAULT_OUTBOX_DIR / f"{machine}.sqlite3"


def enqueue(conn: sqlite3.Connection, request: dict[str, Any]) -> None:
    request = validate_request(request)
    timestamp = now_utc()
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
    with conn:
        existing = conn.execute(
            "SELECT request FROM gateway_outbox WHERE idempotency_key = ?",
            (request["idempotency_key"],),
        ).fetchone()
        if existing and existing["request"] != encoded:
            raise ValueError("idempotency key already exists with a different outbox request")
        conn.execute(
            """INSERT INTO gateway_outbox(idempotency_key, request, status, created_at, updated_at)
               VALUES(?, ?, 'queued', ?, ?)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            (request["idempotency_key"], encoded, timestamp, timestamp),
        )


def post_request(url: str, token: str, request: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(
        url.rstrip("/") + "/v1/write", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(http_request, timeout=COMMAND_TIMEOUT_S + 10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway HTTP {exc.code}: {detail}") from exc


def flush_outbox(conn: sqlite3.Connection, url: str, token: str) -> dict[str, int]:
    counts = {"delivered": 0, "remaining": 0}
    rows = conn.execute(
        "SELECT * FROM gateway_outbox WHERE status = 'queued' ORDER BY created_at, idempotency_key"
    ).fetchall()
    for row in rows:
        request = json.loads(row["request"])
        try:
            response = post_request(url, token, request)
        except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError):
            counts["remaining"] += 1
            continue
        terminal_status = response.get("status")
        if terminal_status == "completed":
            with conn:
                conn.execute(
                    "UPDATE gateway_outbox SET status = 'delivered', response = ?, updated_at = ? WHERE idempotency_key = ?",
                    (json.dumps(response, ensure_ascii=False), now_utc(), row["idempotency_key"]),
                )
            counts["delivered"] += 1
            continue
        if terminal_status in {"failed", "processing"}:
            # The request reached the writer host, but the mutation is not known to
            # be complete. Keep it visible and make the CLI return nonzero;
            # a later retry is safe because the server receipt is idempotent
            # and a crash-left processing key never dispatches again.
            with conn:
                conn.execute(
                    "UPDATE gateway_outbox SET response = ?, updated_at = ? WHERE idempotency_key = ?",
                    (json.dumps(response, ensure_ascii=False), now_utc(), row["idempotency_key"]),
                )
            counts["remaining"] += 1
            continue
        counts["remaining"] += 1
    # Re-derive from the table rather than trust the loop tally: a concurrent
    # `enqueue()` during this flush would otherwise be missing from "remaining".
    counts["remaining"] = conn.execute(
        "SELECT COUNT(*) FROM gateway_outbox WHERE status = 'queued'"
    ).fetchone()[0]
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    serve = sub.add_parser("serve", help="Run the authenticated writer service")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8781)
    serve.add_argument("--cert")
    serve.add_argument("--key")
    submit = sub.add_parser("submit", help="Queue then attempt one gateway request")
    submit.add_argument("--idempotency-key", default=None)
    submit.add_argument("command", choices=sorted(ALLOWED_COMMANDS))
    submit.add_argument("arguments", nargs=argparse.REMAINDER)
    sub.add_parser("flush", help="Retry every queued request")
    sub.add_parser("status", help="Show local machine-specific outbox counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "serve":
        token = required_token()
        non_loopback = args.bind not in {"127.0.0.1", "::1", "localhost"}
        if non_loopback and not (args.cert and args.key):
            raise SystemExit("non-loopback gateway binding requires --cert and --key TLS files")
        server = http.server.ThreadingHTTPServer((args.bind, args.port), GatewayHandler)
        server.gateway_token = token  # type: ignore[attr-defined]
        server.receipts_path = DEFAULT_RECEIPTS  # type: ignore[attr-defined]
        if args.cert and args.key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.cert, args.key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        print(json.dumps({"listening": f"{args.bind}:{args.port}", "tls": bool(args.cert)}))
        server.serve_forever()
        return 0

    conn = connect_store(outbox_path())
    try:
        initialize_outbox(conn)
        if args.mode == "submit":
            key = args.idempotency_key or f"{socket.gethostname()}:{uuid.uuid4().hex}"
            enqueue(conn, {"idempotency_key": key, "command": args.command, "arguments": args.arguments})
            result = flush_outbox(conn, os.environ.get(URL_ENV, ""), required_token()) if os.environ.get(URL_ENV) else {
                "delivered": 0, "remaining": 1,
            }
            print(json.dumps({"idempotency_key": key, **result}, ensure_ascii=False))
            return 0 if result["remaining"] == 0 else 1
        if args.mode == "flush":
            url = os.environ.get(URL_ENV, "")
            if not url:
                raise SystemExit(f"{URL_ENV} is required")
            result = flush_outbox(conn, url, required_token())
            print(json.dumps(result))
            return 0 if result["remaining"] == 0 else 1
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM gateway_outbox GROUP BY status"
        ).fetchall())
        print(json.dumps({"outbox": str(outbox_path()), "counts": counts}))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
