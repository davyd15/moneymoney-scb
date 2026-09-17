"""scb_statement.py: read and reconcile SCB (Siam Commercial Bank) statement PDFs.

The statement the SCB EASY app sends by email is a text PDF built with
JasperReports. After decryption every transaction is one line of text

    DD/MM/YY HH:MM <code> <channel> <amount> <balance> DESC : <description>

followed by a NOTE line. Debit and credit are separate columns on paper, but
only one of them ever carries a value, so the extracted text holds a single
amount and the running balance. The sign is taken from the balance change and
the whole statement is checked against the totals the bank prints at the end.
A layout change at the bank surfaces as a StatementError, never as a wrong
booking.

Shared by scb_bridge.py (MoneyMoney extension) and scb_import.py (offline
accounts via AppleScript).
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader

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
TITLE = re.compile(r"^STATEMENT OF (?P<type>[A-Z ]+?) ACCOUNT$")

# How SCB writes the counterparty into the description. Whatever does not match
# is booked with the whole description as name, which is still searchable.
TRANSFER = re.compile(r"^Transfer (?:to|from) (?P<bank>[A-Z]+) x(?P<acct>\d+)\s+(?P<name>.+)$")
BY_ORDER_OF = re.compile(r"^(?P<ref>.*?)\s+B/O\s+(?P<name>.+)$")


class StatementError(Exception):
    """The PDF is not a statement this parser understands, or it does not add up."""


@dataclass
class Row:
    booking_date: date
    time: str
    code: str
    channel: str
    amount: Decimal  # negative for debits, signed by reconcile()
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

    @property
    def booking_text(self) -> str:
        return " ".join(p for p in (self.channel, self.code) if p)

    def identifier(self, account_number: str) -> str:
        """Stable across overlapping statements: the same booking yields the same id."""
        key = f"{account_number}|{self.booking_date}|{self.time}|{self.amount}|{self.balance}|{self.description}"
        return hashlib.sha1(key.encode()).hexdigest()

    def to_dict(self, account_number: str) -> dict:
        return {
            "id": self.identifier(account_number),
            "bookingDate": self.booking_date.isoformat(),
            "time": self.time,
            "amount": f"{self.amount:.2f}",
            "balance": f"{self.balance:.2f}",
            "name": self.name,
            "purpose": self.purpose,
            "bookingText": self.booking_text,
        }


@dataclass
class Statement:
    account_number: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    owner: str = ""
    account_type: str = "savings"  # "savings" or "current"
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


def extract_lines(pdf: Path, password: str) -> list[str]:
    reader = PdfReader(pdf)
    if reader.is_encrypted and not reader.decrypt(password):
        raise StatementError(f"{pdf.name}: the password does not open this PDF.")
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


def parse_lines(lines: list[str]) -> Statement:
    account = period = opening = None
    owner = ""
    account_type = "savings"
    for index, line in enumerate(lines):
        if account is None and (m := ACCOUNT_NUMBER.match(line)):
            account = m.group(1)
            # The account holder follows the account number in the header block.
            if index + 1 < len(lines) and re.fullmatch(r"[A-Z][A-Z .'-]+", lines[index + 1]):
                owner = lines[index + 1].title()
        if period is None and (m := PERIOD.search(line)):
            period = tuple(datetime.strptime(part, "%d/%m/%Y").date() for part in m.groups())
        if opening is None and (m := OPENING.search(line)):
            opening = money(m.group("amount"))
        if m := TITLE.match(line):
            account_type = "current" if "CURRENT" in m.group("type") else "savings"
    if account is None or period is None or opening is None:
        raise StatementError("Account number, period or opening balance not found. Is this an SCB statement?")

    statement = Statement(account, period[0], period[1], opening, owner, account_type)
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
    reconcile(statement)
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


def parse_pdf(pdf: Path, password: str) -> Statement:
    """Decrypt, parse and reconcile one statement. Raises StatementError on any doubt."""
    return parse_lines(extract_lines(pdf, password))
