#!/usr/bin/env python3
"""scb_import.py: book SCB statement PDFs into MoneyMoney offline accounts.

This is the command line alternative to the MoneyMoney extension (SCB.lua plus
scb_bridge.py). It needs no helper service: it reads the statement, compares it
with what the offline account already holds and books the rest through
MoneyMoney's AppleScript interface.

  1. Opens the PDF with the password from the macOS Keychain (service
     "scb-statement") and reconciles it, see scb_statement.py.
  2. Finds the MoneyMoney account by account number. The file name is useless
     for that: SCB names every statement AcctSt_<Mon><YY>.pdf, for every account.
  3. Books only what is missing. A transaction counts as present when the
     account already holds one with the same booking date and amount. The
     purpose text is ignored on purpose: entries typed in by hand never match it.
  4. Checks the account balance at the end of the period against the statement.
  5. Moves the processed file to statements/<account>/<from>_<to>.pdf so two
     statements with the same file name cannot overwrite each other.

    python3 scb_import.py ~/Downloads/AcctSt_Sep26.pdf
    python3 scb_import.py --dry-run FILE.pdf     parse, reconcile, compare, book nothing
    python3 scb_import.py --keep FILE.pdf        leave the file where it is

Requires macOS, MoneyMoney (running and unlocked), Python 3.11 or newer,
pypdf and cryptography (pip3 install -r requirements.txt).
"""

import argparse
import plistlib
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from scb_statement import Row, Statement, StatementError, parse_pdf

__version__ = "0.2.0"

KEYCHAIN_SERVICE = "scb-statement"
ARCHIVE_DIR = Path(__file__).resolve().parent / "statements"


class MoneyMoneyError(Exception):
    """MoneyMoney refused or is not reachable."""


def keychain_password(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise StatementError(
            f"No Keychain item for service {service!r}. Add it in Terminal with:\n"
            f"  security add-generic-password -a $USER -s {service} -w"
        )
    return result.stdout.rstrip("\n")


class MoneyMoney:
    """The AppleScript interface, see https://moneymoney.app/api/applescript/"""

    @staticmethod
    def quote(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    def run(self, command: str) -> bytes:
        result = subprocess.run(
            ["osascript", "-e", f'tell application "MoneyMoney" to {command}'],
            capture_output=True,
        )
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip()
            if "Locked database" in message:
                raise MoneyMoneyError("MoneyMoney is locked. Unlock it and run the import again.")
            raise MoneyMoneyError(message)
        return result.stdout

    def accounts(self) -> list[dict]:
        return [a for a in plistlib.loads(self.run("export accounts")) if not a.get("group")]

    def account_for(self, account_number: str) -> dict:
        wanted = re.sub(r"\D", "", account_number)
        for account in self.accounts():
            if re.sub(r"\D", "", account.get("accountNumber", "")) == wanted:
                return account
        raise MoneyMoneyError(
            f"No MoneyMoney account carries the account number {account_number}. "
            "Enter it in the account settings of the matching offline account."
        )

    def transactions(self, uuid: str, start: date, end: date | None = None) -> list[dict]:
        command = f'export transactions from account "{uuid}" from date "{start:%Y-%m-%d}"'
        if end is not None:
            command += f' to date "{end:%Y-%m-%d}"'
        return plistlib.loads(self.run(command + ' as "plist"'))["transactions"]

    def add(self, uuid: str, row: Row) -> None:
        self.run(
            f'add transaction to account "{uuid}" on date "{row.booking_date:%Y-%m-%d}" '
            f"to {self.quote(row.name)} amount {row.amount:.2f} purpose {self.quote(row.purpose)}"
        )


def missing_rows(statement: Statement, existing: list[dict]) -> list[Row]:
    """Rows the account does not hold yet, matched by booking date and amount only."""
    present = Counter(
        (t["bookingDate"].date(), Decimal(str(t["amount"])).quantize(Decimal("0.01")))
        for t in existing
    )
    missing = []
    for row in statement.rows:
        key = (row.booking_date, row.amount)
        if present[key] > 0:
            present[key] -= 1
        else:
            missing.append(row)
    return missing


def archive(pdf: Path, statement: Statement) -> Path:
    folder = ARCHIVE_DIR / statement.account_number
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{statement.period_start:%Y-%m-%d}_{statement.period_end:%Y-%m-%d}"
    target = folder / f"{stem}.pdf"
    counter = 2
    while target.exists() and target.resolve() != pdf.resolve():
        target = folder / f"{stem}-{counter}.pdf"
        counter += 1
    if target.resolve() != pdf.resolve():
        shutil.move(pdf, target)
    return target


def process(pdf: Path, password: str, dry_run: bool, keep: bool) -> int:
    statement = parse_pdf(pdf, password)
    print(
        f"{pdf.name}: account {statement.account_number}, {statement.period_start} to "
        f"{statement.period_end}, {len(statement.rows)} rows, opening {statement.opening_balance:,.2f}, "
        f"closing {statement.closing_balance:,.2f} THB, reconciled."
    )

    mm = MoneyMoney()
    account = mm.account_for(statement.account_number)
    existing = mm.transactions(account["uuid"], statement.period_start, statement.period_end)
    missing = missing_rows(statement, existing)
    for row in statement.rows:
        flag = "NEW " if any(row is m for m in missing) else "have"
        print(f"  {flag} {row.booking_date} {row.time} {row.amount:>12,.2f}  {row.name[:32]:32} {row.purpose[:60]}")

    if dry_run:
        print(f"Dry run: {len(missing)} of {len(statement.rows)} rows would be booked into {account['name']!r}.")
        return 0

    for row in missing:
        mm.add(account["uuid"], row)
    print(f"Booked {len(missing)} of {len(statement.rows)} rows into {account['name']!r}.")

    # The account balance at the end of the period must equal the statement.
    later = mm.transactions(account["uuid"], statement.period_end + timedelta(days=1))
    balance_now = Decimal(str(mm.account_for(statement.account_number)["balance"][0][0]))
    at_period_end = balance_now - sum((Decimal(str(t["amount"])) for t in later), Decimal(0))
    if at_period_end == statement.closing_balance:
        print(f"Balance check passed: {at_period_end:,.2f} THB on {statement.period_end}.")
    else:
        print(
            f"WARNING: MoneyMoney holds {at_period_end:,.2f} THB on {statement.period_end}, "
            f"the statement says {statement.closing_balance:,.2f}. Check the entries before this period."
        )

    if not keep:
        print(f"Moved to {archive(pdf, statement)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("pdf", nargs="+", type=Path, help="statement PDF(s) from the SCB EASY app")
    parser.add_argument("--dry-run", action="store_true", help="parse, reconcile and compare, but book nothing")
    parser.add_argument("--keep", action="store_true", help="do not move the file into statements/")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    try:
        password = keychain_password(KEYCHAIN_SERVICE)
        for pdf in args.pdf:
            process(pdf, password, args.dry_run, args.keep)
    except (StatementError, MoneyMoneyError) as error:
        sys.stdout.flush()
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
