#!/usr/bin/env python3
"""scb_bridge.py: local HTTPS bridge between SCB statement PDFs and MoneyMoney.

A MoneyMoney extension can only speak HTTP, and MoneyMoney's App Transport
Security accepts HTTPS only. So the extension (SCB.lua) talks to this bridge on
https://127.0.0.1:8766. On every refresh the bridge picks up new statement PDFs
from an inbox folder, reconciles them (scb_statement.py), keeps every booking
in a local store and answers the extension's questions: which accounts, which
balance, which transactions since a date.

Normal operation is a LaunchAgent with socket activation (install.sh): launchd
holds the port, the bridge starts on the first connection and exits after two
minutes without requests. Manual start for testing:

    python3 scb_bridge.py --inbox ~/Downloads

Endpoints, all JSON:
    GET  /__status__                                 alive, version, inbox, accounts
    POST /refresh   header X-Statement-Password      parse and archive new PDFs
    GET  /accounts                                   {"accounts": [balance, balance date, ...]}
    GET  /transactions?account=NNN-NNNNNN-N&since=YYYY-MM-DD   {"balance", "balanceDate", "transactions"}

"balance" is null until a statement with at least one transaction has been read
for the account: SCB prints no balance on a statement without transactions.

Data directory ~/Library/Application Support/SCBBridge/:
    store.json                          every booking ever read, by account and booking id
    statements/<account>/<from>_<to>.pdf processed statements
    config.json                         the start date, see --set-start
    cert.pem, key.pem                   self-signed certificate for 127.0.0.1

Start date: when you switch from accounts you kept by hand, set the first day
the extension is responsible for (`--set-start 2026-09-12`, or install.sh
--start). Bookings before it stay in the bridge's store, count for the balance
and are never delivered, so MoneyMoney does not see them twice. The date lives
in config.json and survives reinstalls.

The PDF password arrives with each refresh request and is never written down.
"""

# Suppress the Dock icon before anything else is imported, otherwise it flashes.
try:
    from AppKit import NSApplication, NSApplicationActivationPolicyProhibited  # type: ignore[import-not-found]
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyProhibited)
except Exception:
    pass

import argparse
import ctypes
import hashlib
import http.server
import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scb_statement import Statement, StatementError, parse_pdf

__version__ = "1.2.0"

PORT = 8766
IDLE_TIMEOUT = 120  # seconds without a request before the bridge exits (launchd restarts it)
DATA_DIR = Path.home() / "Library" / "Application Support" / "SCBBridge"
CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE = DATA_DIR / "key.pem"
STORE_FILE = DATA_DIR / "store.json"
CONFIG_FILE = DATA_DIR / "config.json"
STATEMENTS_DIR = DATA_DIR / "statements"
DEFAULT_INBOX = DATA_DIR / "inbox"
PATTERN = "AcctSt*.pdf"  # how the SCB EASY app names every statement, for every account

_idle_timer: threading.Timer | None = None
_idle_lock = threading.Lock()
_socket_activated = False  # only then does the bridge exit when idle; a manual start stays up


# ── Store ────────────────────────────────────────────────────────────────────

class Store:
    """All bookings ever read, in one JSON file. Written atomically.

    Per account it keeps the statements read (period, number of rows, closing
    balance) and every booking under its stable id. The balance is derived from
    the statements on demand, see balance().
    """

    VERSION = 2

    def __init__(self, path: Path):
        self.path = path
        self.data = {"version": self.VERSION, "accounts": {}}
        if path.exists():
            self.data = self.migrate(json.loads(path.read_text()))

    @staticmethod
    def migrate(data: dict) -> dict:
        """Version 1 (bridge 1.0.0) kept one balance per account instead of one per statement."""
        if data.get("version", 1) < 2:
            for account in data["accounts"].values():
                balance, until = account.pop("balance", None), account.pop("balanceDate", "")
                for record in account["statements"]:
                    known = record["rows"] > 0 and record["to"] == until
                    record.setdefault("closingBalance", balance if known else None)
            data["version"] = 2
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True))
        os.replace(tmp, self.path)

    def absorb(self, statement: Statement, archived: Path) -> int:
        """Merge one reconciled statement. Returns the number of bookings not seen before."""
        account = self.data["accounts"].setdefault(statement.account_number, {
            "owner": "", "type": "savings", "statements": [], "transactions": {},
        })
        if statement.owner:
            account["owner"] = statement.owner
        account["type"] = statement.account_type
        new = 0
        for row in statement.rows:
            entry = row.to_dict(statement.account_number)
            if entry["id"] not in account["transactions"]:
                new += 1
            account["transactions"][entry["id"]] = entry
        closing = statement.closing_balance
        record = {
            "from": statement.period_start.isoformat(),
            "to": statement.period_end.isoformat(),
            "file": archived.name,
            "rows": len(statement.rows),
            "closingBalance": None if closing is None else f"{closing:.2f}",
            "read": datetime.now().isoformat(timespec="seconds"),
        }
        # A byte-identical statement read again lands on the same archive file: replace its entry.
        account["statements"] = [s for s in account["statements"] if s["file"] != archived.name] + [record]
        return new

    @staticmethod
    def balance(account: dict) -> tuple[str | None, str | None]:
        """The account balance and the day it holds for, or (None, None) while unknown.

        The statement that lists transactions and ends last fixes the balance at its
        end. A statement without transactions carries no balance, SCB prints none;
        but if it follows on without a gap it proves that nothing moved, so the
        balance holds until its end. Before any statement with a balance has been
        read, the balance is unknown and nothing is made up.
        """
        known = [s for s in account["statements"] if s.get("closingBalance") is not None]
        if not known:
            return None, None
        anchor = max(known, key=lambda s: (s["to"], s["read"]))
        until = date.fromisoformat(anchor["to"])
        quiet = [(date.fromisoformat(s["from"]), date.fromisoformat(s["to"]))
                 for s in account["statements"] if s["rows"] == 0]
        while later := [end for start, end in quiet if start <= until + timedelta(days=1) and end > until]:
            until = max(later)
        return anchor["closingBalance"], until.isoformat()

    def accounts(self) -> list[dict]:
        result = []
        for number, account in sorted(self.data["accounts"].items()):
            balance, until = self.balance(account)
            result.append({
                "accountNumber": number,
                "owner": account["owner"],
                "type": account["type"],
                "currency": "THB",
                "balance": balance,
                "balanceDate": until,
                "transactions": len(account["transactions"]),
            })
        return result

    def transactions(self, number: str, since: str, start: str | None = None) -> list[dict]:
        """Bookings from `since` on, and never from before the start date."""
        account = self.data["accounts"].get(number)
        if account is None:
            return []
        first = max(since, start or since)
        rows = [t for t in account["transactions"].values() if t["bookingDate"] >= first]
        return sorted(rows, key=lambda t: (t["bookingDate"], t["time"], t["id"]))


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=1) + "\n")
    os.replace(tmp, CONFIG_FILE)


# ── Statement intake ─────────────────────────────────────────────────────────

def archive(pdf: Path, statement: Statement) -> Path:
    """Move the PDF to statements/<account>/<from>_<to>.pdf. A byte-identical copy of a
    statement already archived is simply removed; a differing one for the same period
    (the bank added late bookings) is kept with a counter."""
    folder = STATEMENTS_DIR / statement.account_number
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{statement.period_start:%Y-%m-%d}_{statement.period_end:%Y-%m-%d}"
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    target = folder / f"{stem}.pdf"
    counter = 2
    while target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            pdf.unlink()
            return target
        target = folder / f"{stem}-{counter}.pdf"
        counter += 1
    shutil.move(pdf, target)
    return target


def refresh(store: Store, inbox: Path, password: str) -> dict:
    """Read every statement waiting in the inbox. Files that fail stay where they are."""
    processed, failed = [], []
    for pdf in sorted(inbox.glob(PATTERN)):
        try:
            statement = parse_pdf(pdf, password)
        except StatementError as error:
            failed.append({"file": pdf.name, "error": str(error)})
            continue
        except Exception as error:  # a broken file must not take the bridge down
            failed.append({"file": pdf.name, "error": f"{type(error).__name__}: {error}"})
            continue
        archived = archive(pdf, statement)
        new = store.absorb(statement, archived)
        closing = statement.closing_balance
        processed.append({
            "file": pdf.name,
            "account": statement.account_number,
            "from": statement.period_start.isoformat(),
            "to": statement.period_end.isoformat(),
            "rows": len(statement.rows),
            "new": new,
            "closingBalance": None if closing is None else f"{closing:.2f}",
            "archived": str(archived),
        })
        print(f"Read {pdf.name}: account {statement.account_number}, {statement.period_start} to "
              f"{statement.period_end}, {len(statement.rows)} rows, {new} new.", flush=True)
    if processed:
        store.save()
    for entry in failed:
        print(f"Skipped {entry['file']}: {entry['error']}", flush=True)
    return {"processed": processed, "failed": failed, "accounts": store.accounts()}


# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    store: Store
    inbox: Path
    server_version = f"SCBBridge/{__version__}"

    def log_message(self, _format, *args):
        print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    def reply(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _reset_idle_timer(self.server)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/__status__":
            # macOS guards Downloads, Desktop and Documents: a background process
            # needs the user's consent (System Settings, Privacy, Files and Folders).
            try:
                os.listdir(self.inbox)
                inbox_error = ""
            except OSError as error:
                inbox_error = f"{type(error).__name__}: {error}"
            self.reply(200, {
                "ok": True, "version": __version__, "inbox": str(self.inbox),
                "inboxError": inbox_error, "accounts": len(self.store.data["accounts"]),
                "start": load_config().get("start"),
            })
        elif url.path == "/accounts":
            self.reply(200, {"accounts": self.store.accounts()})
        elif url.path == "/transactions":
            number = query.get("account", [""])[0]
            since = query.get("since", ["1970-01-01"])[0]
            try:
                date.fromisoformat(since)
            except ValueError:
                return self.reply(400, {"error": f"since must be YYYY-MM-DD, got {since!r}"})
            account = self.store.data["accounts"].get(number)
            if account is None:
                return self.reply(404, {"error": f"no statement read yet for account {number}"})
            balance, until = self.store.balance(account)
            self.reply(200, {
                "account": number, "balance": balance, "balanceDate": until,
                "transactions": self.store.transactions(number, since, load_config().get("start")),
            })
        else:
            self.reply(404, {"error": f"unknown path {url.path}"})

    def do_POST(self):
        _reset_idle_timer(self.server)
        if urlparse(self.path).path != "/refresh":
            return self.reply(404, {"error": "unknown path"})
        password = self.headers.get("X-Statement-Password", "")
        if not password:
            return self.reply(400, {"error": "X-Statement-Password header missing"})
        try:
            os.listdir(self.inbox)
        except OSError as error:
            return self.reply(500, {"error": f"cannot read the inbox folder {self.inbox}: {error}. "
                                             "Allow Python to access it under System Settings, Privacy, Files and Folders."})
        self.reply(200, refresh(self.store, self.inbox, password))


# ── Process lifetime ─────────────────────────────────────────────────────────

def _reset_idle_timer(server) -> None:
    """After IDLE_TIMEOUT seconds without a request the bridge exits; launchd restarts it."""
    global _idle_timer
    if not _socket_activated:
        return
    with _idle_lock:
        if _idle_timer:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(
            IDLE_TIMEOUT, lambda: threading.Thread(target=server.shutdown, daemon=True).start())
        _idle_timer.daemon = True
        _idle_timer.start()


def launchd_socket() -> socket.socket | None:
    """The listening socket launchd already bound for us, if we run under socket activation."""
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        fds = ctypes.POINTER(ctypes.c_int)()
        count = ctypes.c_size_t(0)
        if lib.launch_activate_socket(b"Listeners", ctypes.byref(fds), ctypes.byref(count)) != 0 or count.value == 0:
            return None
        sock = socket.socket(fileno=fds[0])
        lib.free(fds)
        return sock
    except Exception:
        return None


class PreBoundHTTPServer(http.server.HTTPServer):
    """An HTTPServer on a socket launchd has already bound and put into listen state."""

    def __init__(self, sock, handler):
        socketserver.BaseServer.__init__(self, sock.getsockname(), handler)
        self.socket = sock


def ensure_tls_cert() -> None:
    """Self-signed certificate for 127.0.0.1, created once. install.sh adds it to the Keychain."""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE), "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "keyUsage=digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=serverAuth",
    ], check=True, capture_output=True)
    os.chmod(KEY_FILE, 0o600)
    print(f"Created {CERT_FILE}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local HTTPS bridge between SCB statement PDFs and MoneyMoney.")
    parser.add_argument("--inbox", type=Path, default=DEFAULT_INBOX,
                        help=f"folder the statement PDFs arrive in (default: {DEFAULT_INBOX})")
    parser.add_argument("--init-cert", action="store_true", help="create the certificate and exit")
    parser.add_argument("--set-start", metavar="YYYY-MM-DD|off",
                        help="deliver only bookings from this day on (kept in config.json), 'off' removes it")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    if args.set_start:
        config = load_config()
        if args.set_start == "off":
            config.pop("start", None)
        else:
            config["start"] = date.fromisoformat(args.set_start).isoformat()
        save_config(config)
        print(f"Start date: {config.get('start') or 'none'}")
        return 0

    ensure_tls_cert()
    if args.init_cert:
        return 0
    inbox = args.inbox.expanduser()
    inbox.mkdir(parents=True, exist_ok=True)

    Handler.store = Store(STORE_FILE)
    Handler.inbox = inbox
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(CERT_FILE, KEY_FILE)

    global _socket_activated
    sock = launchd_socket()
    if sock:
        _socket_activated = True
        server = PreBoundHTTPServer(tls.wrap_socket(sock, server_side=True), Handler)
        _reset_idle_timer(server)
        mode = f"socket activation, exits after {IDLE_TIMEOUT}s idle"
    else:
        server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        mode = "manual start, stop with CTRL+C"
    print(f"SCB bridge {__version__} on https://127.0.0.1:{PORT} ({mode}), inbox {inbox}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("SCB bridge stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
