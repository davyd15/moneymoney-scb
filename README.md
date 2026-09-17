# MoneyMoney Importer: SCB (Siam Commercial Bank) Thailand

Imports account statements from **Siam Commercial Bank (SCB) Thailand** into [MoneyMoney](https://moneymoney-app.com) offline accounts. The importer reads the password-protected statement PDFs that the SCB EASY app sends by email and books every transaction through MoneyMoney's AppleScript interface.

> **Status: working, version 0.1.0.** Tested on savings account statements. First real import of a second account still pending, see the changelog.

---

## Why an importer and not a Web Banking extension

SCB shut down its internet banking (SCB EASY NET) on 14 July 2023. Personal customers only have the SCB EASY app, which is bound to one device and has no public API. There is nothing a MoneyMoney Web Banking extension could log in to.

What SCB does offer, free of charge: an account statement for up to the past 12 months, requested in the app and delivered as a password-protected PDF to the email address on file. That PDF is text based, not scanned, so it can be parsed reliably.

## How It Works

| Step | What happens |
|------|--------------|
| 1 | You request the statement in the SCB EASY app: account, *Other Services*, *Request Account Statement*, period. The PDF arrives by email within minutes. This is the only manual step. |
| 2 | The importer opens the PDF with the statement password stored in the macOS Keychain (service `scb-statement`) and extracts every row: date, time, transaction code, channel, amount, running balance, description, note. The debit and credit columns collapse into one amount in the extracted text, so the sign comes from the running balance. |
| 3 | Reconciliation against the statement itself: every row's amount must equal the change of the running balance, and the parsed debit and credit totals and item counts must equal the totals printed on the statement. Any mismatch aborts before MoneyMoney is touched. A layout change at the bank surfaces here instead of as wrong bookings. |
| 4 | The MoneyMoney account is found by account number, read from the PDF and matched against the account number entered in MoneyMoney. The file name cannot be used: SCB names every statement `AcctSt_<Mon><YY>.pdf`, for every account. |
| 5 | Only missing transactions are booked via AppleScript (`add transaction`). A transaction counts as present when the account already holds one with the same booking date and amount; the purpose text is ignored so that entries typed in by hand are recognised. The counterparty name is taken from the description (`Transfer to KBNK x3984 Mrs. ...`, `... B/O David Lemke`), the description itself becomes the purpose. MoneyMoney's auto-categorisation applies. |
| 6 | Balance check: the account balance at the end of the statement period must equal the closing balance of the statement. A difference is reported as a warning, it points at older entries that are missing or wrong. |
| 7 | The PDF is moved to `statements/<account number>/<from>_<to>.pdf`, so two statements with the same file name never overwrite each other. |

## Usage

```bash
pip3 install -r requirements.txt
security add-generic-password -a $USER -s scb-statement -w   # once, asks for the PDF password

python3 scb_import.py ~/Downloads/AcctSt_Sep26.pdf            # import and archive
python3 scb_import.py --dry-run ~/Downloads/AcctSt_Sep26.pdf  # parse, reconcile, compare, book nothing
python3 scb_import.py --keep ~/Downloads/AcctSt_Sep26.pdf     # import, leave the file where it is
```

MoneyMoney has to be running and unlocked. Each SCB account needs an offline account in MoneyMoney with the SCB account number entered in the account settings.

Optional trigger: a mail rule or a folder watcher can start the importer as soon as a new statement PDF arrives.

## Requirements

- macOS with MoneyMoney (AppleScript support is built in)
- Python 3.11 or newer with `pypdf`
- One MoneyMoney offline account per SCB account
- The statement password stored in the Keychain; the importer never asks for banking credentials

## Security

- No banking login, no PIN, no session with the bank. The importer only touches local files and MoneyMoney.
- The PDF password lives in the macOS Keychain, never in code or configuration.
- Statements, CSV files and MoneyMoney exports contain personal data and are excluded from the repository by `.gitignore`.

## Changelog

| Version | Change |
|---------|--------|
| 0.1.0 | Parser for SCB savings account statements, reconciliation, account matching by account number, AppleScript import with duplicate detection, balance check, archiving. Verified against a statement whose transactions were all entered by hand: 0 booked, balance check passed. |

## License

MIT, see [LICENSE](LICENSE).
