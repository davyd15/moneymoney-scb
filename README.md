# MoneyMoney Importer: SCB (Siam Commercial Bank) Thailand

Imports account statements from **Siam Commercial Bank (SCB) Thailand** into [MoneyMoney](https://moneymoney-app.com) offline accounts. The importer reads the password-protected statement PDFs that the SCB EASY app sends by email and books every transaction through MoneyMoney's AppleScript interface.

> **Status: in development.** No release yet. The approach is settled, the parser is not written.

---

## Why an importer and not a Web Banking extension

SCB shut down its internet banking (SCB EASY NET) on 14 July 2023. Personal customers only have the SCB EASY app, which is bound to one device and has no public API. There is nothing a MoneyMoney Web Banking extension could log in to.

What SCB does offer, free of charge: an account statement for up to the past 12 months, requested in the app and delivered as a password-protected PDF to the email address on file. That PDF is text based, not scanned, so it can be parsed reliably.

## How It Works

| Step | What happens |
|------|--------------|
| 1 | You request the statement in the SCB EASY app: account, *Other Services*, *Request Account Statement*, period. The PDF arrives by email within minutes. This is the only manual step. |
| 2 | The importer opens the PDF with the statement password stored in the macOS Keychain and extracts every transaction: date, time, channel, amount, running balance, description, note. |
| 3 | Reconciliation: the sum of all parsed transactions must match the closing balance printed on the statement. If it does not, nothing is imported. |
| 4 | Every new transaction is booked into the matching MoneyMoney offline account via AppleScript (`add transaction`). Transactions already present are skipped: the importer reads the account's existing transactions with `export transactions ... as "plist"` and compares date, amount and purpose. |

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
| 0.1 (planned) | Parser for SCB savings account statements, reconciliation, AppleScript import with duplicate detection |

## License

MIT, see [LICENSE](LICENSE).
