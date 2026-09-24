"""Tests for the statement parser and the bridge's store, on synthetic statements.

The fixtures copy the line structure pypdf extracts from real SCB statements
(checked against two statements from September 2026, one with and one without
transactions). Names, numbers and amounts are made up. From the repository root:

    python3 -m unittest discover -s tests -t .
"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scb_bridge import Store
from scb_statement import StatementError, parse_lines

HEADER = """ธนาคารไทยพาณิชย์ จำกัด (มหาชน)
THE SIAM COMMERCIAL BANK PUBLIC COMPANY LIMITED
ใบแจ้งรายการบัญชีออมทรัพย์
STATEMENT OF SAVING ACCOUNT
สาขา
ชื่อ - สกุล
Name
ที่อยู่
Address
เลขที่บัญชี
Account No.
วันที่
Date
123-456789-0
MISTER TEST PERSON
1 TEST ROAD
10110
TEST BRANCH
{period}
Date
วันที่
Time Code Channel Description/NoteBalance/BahtDebit/Credit
เวลา รายการ ช่องทาง ลูกหนี้/เจ้าหนี้ ยอดเงินคงเหลือ รายละเอียด/บันทึกช่วยจำ"""

FOOTER = """เอกสารฉบับนี้ออกโดยระบบอัตโนมัติ จึงไม่ต้องมีการลงลายมือชื่อ วันที่ 24/09/2026 เวลา 22:34:34 น.
This document is auto-generated, a signature is not required. On 24/09/2026 at 22:34:34.
หน้า {page} / {pages}"""

WITH_ROWS = "\n".join([
    HEADER.format(period="01/09/2026 - 10/09/2026"),
    "ยอดเงินคงเหลือยกมา (BALANCE BROUGHT FORWARD) 1,000.00",
    "01/09/26 06:27 X2 ENET 400.00 600.00 DESC : Transfer to KBNK x1234 Mrs. Example Payee",
    "NOTE : -",
    "02/09/26 17:22 X1 RIS 50.00 650.00 DESC : NTRF123456       B/O Test Person",
    "NOTE : rent",
    "TOTAL AMOUNTS (Debit) 400.00",
    "TOTAL AMOUNTS (Credit) 50.00",
    "TOTAL ITEMS 1 1",
    FOOTER.format(page=1, pages=1),
])

# What SCB sends for a period without transactions: no balance anywhere, zero totals.
WITHOUT_ROWS = "\n".join([
    HEADER.format(period="11/09/2026 - 23/09/2026"),
    "TOTAL AMOUNTS (Debit) 0.00",
    "TOTAL AMOUNTS (Credit) 0.00",
    "TOTAL ITEMS 0 0",
    FOOTER.format(page=1, pages=1),
])

# A page break inside the first transaction and a description that wraps.
TWO_PAGES = "\n".join([
    HEADER.format(period="01/09/2026 - 30/09/2026"),
    "ยอดเงินคงเหลือยกมา (BALANCE BROUGHT FORWARD) 1,000.00",
    "01/09/26 06:27 X2 ENET 400.00 600.00 DESC : Transfer to KBNK x1234 Mrs. Example Payee",
    FOOTER.format(page=1, pages=2),
    HEADER.format(period="01/09/2026 - 30/09/2026"),
    "NOTE : -",
    "15/09/26 09:00 X2 ENET 100.00 500.00 DESC : Transfer to BAY x9876 A payee with a name so long",
    "that it wraps",
    "NOTE : -",
    "TOTAL AMOUNTS (Debit) 500.00",
    "TOTAL AMOUNTS (Credit) 0.00",
    "TOTAL ITEMS 2 0",
    FOOTER.format(page=2, pages=2),
])


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


class StatementTest(unittest.TestCase):
    def test_rows_are_signed_and_reconciled(self):
        s = parse_lines(lines(WITH_ROWS))
        self.assertEqual(s.account_number, "123-456789-0")
        self.assertEqual(s.owner, "Mister Test Person")
        self.assertEqual([r.amount for r in s.rows], [Decimal("-400.00"), Decimal("50.00")])
        self.assertEqual(s.closing_balance, Decimal("650.00"))
        self.assertEqual([r.name for r in s.rows], ["Mrs. Example Payee", "Test Person"])
        self.assertEqual(s.rows[1].purpose, "NTRF123456       B/O Test Person | Note: rent | 17:22")
        # The time keeps identical bookings on the same day apart.
        self.assertEqual(s.rows[0].purpose, "Transfer to KBNK x1234 Mrs. Example Payee | 06:27")

    def test_statement_without_transactions_has_no_balance(self):
        s = parse_lines(lines(WITHOUT_ROWS))
        self.assertEqual(s.rows, [])
        self.assertIsNone(s.opening_balance)
        self.assertIsNone(s.closing_balance)

    def test_transactions_without_opening_balance_are_refused(self):
        text = WITH_ROWS.replace("ยอดเงินคงเหลือยกมา (BALANCE BROUGHT FORWARD) 1,000.00\n", "")
        with self.assertRaisesRegex(StatementError, "Opening balance"):
            parse_lines(lines(text))

    def test_totals_must_match(self):
        with self.assertRaisesRegex(StatementError, "debit total"):
            parse_lines(lines(WITH_ROWS.replace("(Debit) 400.00", "(Debit) 401.00")))

    def test_missing_totals_are_refused(self):
        text = "\n".join(line for line in WITHOUT_ROWS.splitlines() if not line.startswith("TOTAL"))
        with self.assertRaisesRegex(StatementError, "Totals"):
            parse_lines(lines(text))

    def test_other_pdf_is_refused(self):
        with self.assertRaisesRegex(StatementError, "Account number not found"):
            parse_lines(["Some other PDF", "01/09/2026 - 10/09/2026"])

    def test_page_furniture_never_ends_up_in_a_description(self):
        s = parse_lines(lines(TWO_PAGES))
        self.assertEqual(s.rows[0].description, "Transfer to KBNK x1234 Mrs. Example Payee")
        self.assertEqual(s.rows[1].description, "Transfer to BAY x9876 A payee with a name so long that it wraps")
        self.assertEqual(s.closing_balance, Decimal("500.00"))


class StoreBalanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "store.json")

    def tearDown(self):
        self.tmp.cleanup()

    def account(self) -> dict:
        return self.store.data["accounts"]["123-456789-0"]

    def test_unknown_until_a_statement_with_transactions_arrives(self):
        self.store.absorb(parse_lines(lines(WITHOUT_ROWS)), Path("quiet.pdf"))
        self.assertEqual(Store.balance(self.account()), (None, None))
        self.store.absorb(parse_lines(lines(WITH_ROWS)), Path("busy.pdf"))
        # 01.09. to 10.09. lists transactions, 11.09. to 23.09. follows without a gap.
        self.assertEqual(Store.balance(self.account()), ("650.00", "2026-09-23"))

    def test_a_gap_stops_the_balance_from_carrying_forward(self):
        self.store.absorb(parse_lines(lines(WITH_ROWS)), Path("busy.pdf"))
        later = WITHOUT_ROWS.replace("11/09/2026 - 23/09/2026", "15/09/2026 - 23/09/2026")
        self.store.absorb(parse_lines(lines(later)), Path("quiet.pdf"))
        self.assertEqual(Store.balance(self.account()), ("650.00", "2026-09-10"))

    def test_reading_the_same_file_twice_keeps_one_entry(self):
        for _ in range(2):
            self.store.absorb(parse_lines(lines(WITH_ROWS)), Path("busy.pdf"))
        self.assertEqual(len(self.account()["statements"]), 1)
        self.assertEqual(len(self.account()["transactions"]), 2)

    def test_start_date_holds_back_older_bookings(self):
        self.store.absorb(parse_lines(lines(WITH_ROWS)), Path("busy.pdf"))
        dates = [t["bookingDate"] for t in self.store.transactions("123-456789-0", "1970-01-01", "2026-09-02")]
        self.assertEqual(dates, ["2026-09-02"])
        # The balance does not depend on the start date.
        self.assertEqual(Store.balance(self.account()), ("650.00", "2026-09-10"))

    def test_store_of_bridge_1_0_is_migrated(self):
        path = Path(self.tmp.name) / "v1.json"
        path.write_text(json.dumps({"version": 1, "accounts": {"123-456789-0": {
            "owner": "Mister Test Person", "type": "savings", "balance": "0.00", "balanceDate": "2026-09-10",
            "statements": [{"from": "2026-09-01", "to": "2026-09-10", "file": "a.pdf", "rows": 5,
                            "read": "2026-09-17T17:45:00"}],
            "transactions": {},
        }}}))
        store = Store(path)
        self.assertEqual(store.data["version"], 2)
        self.assertEqual(Store.balance(store.data["accounts"]["123-456789-0"]), ("0.00", "2026-09-10"))


if __name__ == "__main__":
    unittest.main()
