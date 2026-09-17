#!/usr/bin/env python3
"""scb_import.py: book SCB (Siam Commercial Bank) statement PDFs into MoneyMoney.

WHY A PDF IMPORTER: SCB shut down its web banking on 14 July 2023. The SCB EASY
app is the only channel left and it has no API, so the free statement PDF the
app sends by email is the best machine-readable source there is.

WHAT IT DOES
  1. Opens the PDF with the password from the macOS Keychain (service
     "scb-statement") and reads account number, period, opening balance and
     every transaction row.
  2. Reconciles the statement against itself: the running balance of every row,
     the debit and credit totals and the item counts. Any mismatch aborts before
     MoneyMoney is touched. A layout change at the bank shows up here, not as
     wrong bookings.
  3. Finds the MoneyMoney account by account number. The file name is useless
     for that: SCB names every statement AcctSt_<Mon><YY>.pdf, for every account.
  4. Books only what is missing. A transaction counts as present when the
     account already holds one with the same booking date and amount. The
     purpose text is ignored on purpose: entries typed in by hand never match it.
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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

__version__ = "0.1.0"

KEYCHAIN_SERVICE = "scb-statement"
ARCHIVE_DIR = Path(__file__).resolve().parent / "statements"

# One statement row in the text pypdf extracts. Debit and credit are separate
# columns on paper, but only one of them ever carries a value, so the text has a
# single amount followed by the running balance. The sign comes from the balance.
ROW = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<time>\d{2}:\d{2})\s+(?P<code>[^\d\s]\S*)"
    r"(?:\s+(?P<channel>[^\d\s]\S*))?\s+(?P<amount>[\d,]+\.\d{2})\s+(?P<balance>[\d,]+\.\d{2})"
    r"(?:\s+DESC\s*:\s*(?P<desc>.*))?$"
)
NOTE = re.compile(r"^NOTE\s*:\s*(?P<note>.*)$")
ACCOUNT_NUMBER = re.compile(r"^(\d{3}-\d{6}-\d)$")
PERIOD = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
OPENING = re.compile(r"BALANCE BROUGHT FORWARD\)\s*(?P<amount>[\d,]+\.\d{2})")
TOTAL_DEBIT = re.compile(r"^TOTAL AMOUNTS \(Debit\)\s+(?P<amount>[\d,]+\.\d{2})$")
TOTAL_CREDIT = re.compile(r"^TOTAL AMOUNTS \(Credit\)\s+(?P<amount>[\d,]+\.\d{2})$")
TOTAL_ITEMS = re.compile(r"^TOTAL ITEMS\s+(?P<debit>\d+)\s+(?P<credit>\d+)$")

# How SCB writes the counterparty into the description. Whatever does not match
# is booked with the whole description as name, which is still searchable.
TRANSFER = re.compile(r"^Transfer (?:to|from) (?P<bank>[A-Z]+) x(?P<acct>\d+)\s+(?P<name>.+)$")
BY_ORDER_OF = re.compile(r"^(?P<ref>.*?)\s+B/O\s+(?P<name>.+)$")


class StatementError(Exception):
    """The PDF is not a statement this importer understands, or it does not add up."""


class MoneyMoneyError(Exception):
    """MoneyMoney refused or is not reachable."""


@dataclass
class Row:
    booking_date: date
    time: str
    code: str
    channel: str
    amount: Decimal  # negative for debits
    balance: Decimal
    description: str
    note: str

    @property
    def name(self) -> str:
        m = TRANSFER.match(self.description) or BY_ORDER_OF.match(self.description)
        return m.group("name").strip() if m else self.description or self.channel or "SCB"

    @property
    def purpose(self) -> str:
        parts = [self.description]
        if self.note and self.note != "-":
            parts.append(f"Note: {self.note}")
        return " | ".join(p for p in parts if p)


@dataclass
class Statement:
    account_number: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    rows: list[Row] = field(default_factory=list)
    total_debit: Decimal = Decimal(0)
    total_credit: Decimal = Decimal(0)
    items_debit: int = 0
    items_credit: int = 0

    @property
    def closing_balance(self) -> Decimal:
        return self.rows[-1].balance if self.rows else self.opening_balance


def money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


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


def extract_lines(pdf: Path, password: str) -> list[str]:
    reader = PdfReader(pdf)
    if reader.is_encrypted and not reader.decrypt(password):
        raise StatementError(f"{pdf.name}: the Keychain password does not open this PDF.")
    lines = []
    for page in reader.pages:
        lines += [line.strip() for line in page.extract_text().splitlines()]
    return [line for line in lines if line]


def row_date(text: str, start: date, end: date) -> date:
    day, month, year = (int(part) for part in text.split("/"))
    booked = date(2000 + year, month, day)
    if not start <= booked <= end:
        raise StatementError(f"Row date {booked} lies outside the statement period {start} to {end}.")
    return booked


def parse_statement(lines: list[str]) -> Statement:
    account = period = opening = None
    for line in lines:
        if account is None and (m := ACCOUNT_NUMBER.match(line)):
            account = m.group(1)
        if period is None and (m := PERIOD.search(line)):
            period = tuple(datetime.strptime(part, "%d/%m/%Y").date() for part in m.groups())
        if opening is None and (m := OPENING.search(line)):
            opening = money(m.group("amount"))
    if account is None or period is None or opening is None:
        raise StatementError("Account number, period or opening balance not found. Is this an SCB statement?")

    statement = Statement(account, period[0], period[1], opening)
    current: Row | None = None  # the row whose DESC continuation or NOTE line may follow
    for line in lines:
        if m := ROW.match(line):
            statement.rows.append(current := Row(
                booking_date=row_date(m.group("date"), *period),
                time=m.group("time"),
                code=m.group("code"),
                channel=m.group("channel") or "",
                amount=money(m.group("amount")),
                balance=money(m.group("balance")),
                description=(m.group("desc") or "").strip(),
                note="",
            ))
        elif current is not None and (m := NOTE.match(line)):
            current.note = m.group("note").strip()
            current = None
        elif m := TOTAL_DEBIT.match(line):
            statement.total_debit = money(m.group("amount"))
        elif m := TOTAL_CREDIT.match(line):
            statement.total_credit = money(m.group("amount"))
        elif m := TOTAL_ITEMS.match(line):
            statement.items_debit, statement.items_credit = int(m.group("debit")), int(m.group("credit"))
        elif current is not None:
            # A long description wraps onto the next line, before the NOTE line.
            current.description = f"{current.description} {re.sub(r'^DESC\s*:\s*', '', line)}".strip()
        elif re.match(r"^\d{2}/\d{2}/\d{2}\s", line):
            raise StatementError(f"Unparsed statement row: {line!r}")
    return statement


def reconcile(statement: Statement) -> None:
    """Sign every amount from the running balance and prove the statement adds up."""
    previous = statement.opening_balance
    for row in statement.rows:
        delta = row.balance - previous
        if abs(delta) != row.amount:
            raise StatementError(
                f"{row.booking_date} {row.time}: amount {row.amount} does not match the balance "
                f"change {delta} ({previous} to {row.balance})."
            )
        row.amount = delta
        previous = row.balance

    debits = [r for r in statement.rows if r.amount < 0]
    credits = [r for r in statement.rows if r.amount > 0]
    checks = {
        "debit total": (-sum(r.amount for r in debits), statement.total_debit),
        "credit total": (sum(r.amount for r in credits), statement.total_credit),
        "debit items": (len(debits), statement.items_debit),
        "credit items": (len(credits), statement.items_credit),
    }
    for label, (parsed, printed) in checks.items():
        if parsed != printed:
            raise StatementError(f"Reconciliation failed: {label} parsed {parsed}, statement says {printed}.")


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
    statement = parse_statement(extract_lines(pdf, password))
    reconcile(statement)
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
