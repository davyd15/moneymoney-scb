# MoneyMoney Extension: SCB (Siam Commercial Bank) Thailand

A [MoneyMoney](https://moneymoney-app.com) extension for **Siam Commercial Bank (SCB) Thailand**. SCB has no internet banking any more, so the extension reads the password-protected statement PDFs that the SCB EASY app emails on request. Refreshing the account in MoneyMoney picks up new statements, reconciles them and books every transaction.

> **Status: working, version 1.0.** Verified with savings account statements. See the changelog for what has been exercised.

---

## Why statements and not a login

SCB shut down its internet banking (SCB EASY NET) on 14 July 2023. Personal customers only have the SCB EASY app, which is bound to one device and has no public API. There is nothing an extension could log in to.

What SCB does offer, free of charge: an account statement for up to the past 12 months, requested in the app (*account*, *Other Services*, *Request Account Statement*) and delivered as a password-protected PDF to the email address on file. That PDF is text based, not scanned, so it can be parsed reliably.

## How It Works

MoneyMoney extensions run in a Lua sandbox without file access, and MoneyMoney's App Transport Security allows HTTPS only. The extension therefore talks to a small local bridge:

```
SCB EASY app ──email──▶ AcctSt_Sep26.pdf in the inbox folder
                                     │
MoneyMoney ──refresh──▶ SCB.lua ──https://127.0.0.1:8766──▶ scb_bridge.py ──▶ scb_statement.py
                                                             (LaunchAgent,        (decrypt, parse,
                                                              socket activated)    reconcile)
```

| Step | What happens |
|------|--------------|
| 1 | You request the statement in the SCB EASY app and save the PDF into the inbox folder (`~/Downloads` by default when installed with `--inbox ~/Downloads`). This is the only manual step. |
| 2 | On refresh, `SCB.lua` calls the bridge. launchd holds port 8766 and starts `scb_bridge.py` on the first connection; the bridge exits after two minutes without requests. |
| 3 | The bridge opens every `AcctSt*.pdf` in the inbox with the password MoneyMoney passes along (the password you entered for the account) and extracts the rows: date, time, transaction code, channel, amount, running balance, description, note. Debit and credit columns collapse into one amount in the extracted text, so the sign comes from the running balance. |
| 4 | Reconciliation: every row's amount must equal the change of the running balance, and the parsed debit and credit totals and item counts must equal the totals printed on the statement. A statement that does not add up is reported in MoneyMoney and left in the inbox; nothing of it is booked. |
| 5 | Good statements are moved to `~/Library/Application Support/SCBBridge/statements/<account number>/<from>_<to>.pdf`. SCB names every statement `AcctSt_<Mon><YY>.pdf`, for every account, so the file name is never used for anything. A byte-identical statement read twice is simply dropped. |
| 6 | Every booking gets a stable id from account, date, time, amount, balance and description, so overlapping statements never produce duplicates in the bridge's store. The extension delivers the whole store on every refresh; MoneyMoney discards what it already holds. |
| 7 | The account balance is the closing balance of the newest statement. The counterparty name is taken from the description (`Transfer to KBNK x3984 Mrs. ...`, `... B/O David Lemke`), the description becomes the purpose, channel and code the booking text. MoneyMoney's auto-categorisation applies. |

Booking dates are anchored at 12:00 UTC so that a time zone change or a DST switch on the Mac never makes MoneyMoney import a day twice.

## Installation

```bash
git clone https://github.com/davyd15/moneymoney-scb.git
cd moneymoney-scb
./install.sh --inbox ~/Downloads      # or any folder your mail client saves attachments to
```

The installer copies `SCB.lua` into MoneyMoney's extensions folder, the bridge into `~/Library/Application Support/SCBBridge/`, creates a self-signed certificate for `127.0.0.1` and asks for your login password once to trust it for TLS, and loads a LaunchAgent (`com.moneymoney-scb.bridge`) with socket activation.

Then in MoneyMoney:

1. Reload the extensions (right-click an account, *Reload Extensions*) or restart MoneyMoney.
2. Add an account and choose the service **SCB Thailand (Statement PDF)**. User name: anything. Password: the password of your statement PDFs. MoneyMoney keeps it; the bridge receives it per refresh and never stores it.
3. Put at least one statement PDF into the inbox before adding the account: the account list comes from the statements the bridge has read.

Requirements: macOS, MoneyMoney, Python 3.11 or newer with `pypdf` and `cryptography` (the installer installs them). If the inbox is `~/Downloads`, `~/Documents` or `~/Desktop`, macOS may ask once whether Python may access that folder; the extension reports it when access is missing.

`./uninstall.sh` removes everything except the archived statements and the store; `./uninstall.sh --purge` removes those too.

## Command line alternative

`scb_import.py` books statements into MoneyMoney **offline** accounts through the AppleScript interface, without the bridge. It matches the offline account by the account number entered in MoneyMoney, skips transactions that already exist (same booking date and amount, so manually typed entries are recognised) and checks the period-end balance against the statement. It reads the PDF password from the Keychain item `scb-statement`.

```bash
pip3 install -r requirements.txt
security add-generic-password -a $USER -s scb-statement -w   # once
python3 scb_import.py ~/Downloads/AcctSt_Sep26.pdf            # or --dry-run, --keep
```

## Security

- No banking login, no PIN, no session with the bank. The bridge listens on 127.0.0.1 only and touches local files and nothing else.
- The PDF password lives in MoneyMoney (extension) or in the macOS Keychain (command line), never in code, configuration or the bridge's store.
- The bridge's certificate is trusted for TLS on 127.0.0.1 only (`security add-trusted-cert -p ssl`, `CA:FALSE`).
- Statements and the booking store contain personal data and stay under `~/Library/Application Support/SCBBridge/`; the repository ignores `*.pdf`, `*.csv`, `*.plist` and `statements/`.

## Changelog

| Version | Change |
|---------|--------|
| 1.0 | Extension `SCB.lua` plus local bridge `scb_bridge.py` with socket activation, inbox scanning, stable booking ids, balance from the newest statement, installer and uninstaller. Bridge verified end to end with a real statement (parse, archive, duplicate drop, wrong password, unknown account); the extension logic verified with a stand-in for MoneyMoney's runtime. |
| 0.1 | Command line importer `scb_import.py` for offline accounts: parser, reconciliation, account matching by number, AppleScript booking with duplicate detection, balance check, archiving. Verified against a statement whose transactions were all entered by hand: 0 booked, balance check passed. |

## License

MIT, see [LICENSE](LICENSE).
