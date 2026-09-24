# MoneyMoney Extension: SCB (Siam Commercial Bank) Thailand

A [MoneyMoney](https://moneymoney-app.com) extension for **Siam Commercial Bank (SCB) Thailand**. SCB has no internet banking any more, so the extension reads the password-protected statement PDFs that the SCB EASY app emails on request. Refreshing the account in MoneyMoney picks up new statements, reconciles them and books every transaction.

> **Status: working, version 1.2.** Verified with savings account statements with and without transactions. See the changelog for what has been exercised.

---

## Why statements and not a login

SCB shut down its internet banking (SCB EASY NET) on 14 July 2023. Personal customers only have the SCB EASY app, which is bound to one device and has no public API. There is nothing an extension could log in to.

What SCB does offer, free of charge: account statements for up to the past 12 months, requested in the app (*account*, *Other Services*, *Request Account Statement*) and delivered as password-protected PDFs to the email address on file, one file per calendar month. The statement for the current month runs until the day before the request. The PDFs are text based, not scanned, so they can be parsed reliably.

## How It Works

MoneyMoney extensions run in a Lua sandbox without file access, so the extension talks to a small local bridge over HTTPS:

```
SCB EASY app ──email──▶ AcctSt_Sep26.pdf in the inbox folder
                                     │
MoneyMoney ──refresh──▶ SCB.lua ──https://127.0.0.1:8766──▶ scb_bridge.py ──▶ scb_statement.py
                                                             (LaunchAgent,        (decrypt, parse,
                                                              socket activated)    reconcile)
```

| Step | What happens |
|------|--------------|
| 1 | You request the statement in the SCB EASY app and save the PDF into the inbox folder (`~/Downloads` when installed with `--inbox ~/Downloads`). This is the only manual step. |
| 2 | On refresh, `SCB.lua` calls the bridge. launchd holds port 8766 and starts `scb_bridge.py` on the first connection; the bridge exits after two minutes without requests. |
| 3 | The bridge opens every `AcctSt*.pdf` in the inbox with the password MoneyMoney passes along (the password you entered for the account) and extracts the rows: date, time, transaction code, channel, amount, running balance, description, note. Debit and credit columns collapse into one amount in the extracted text, so the sign comes from the running balance. |
| 4 | Reconciliation: every row's amount must equal the change of the running balance, and the parsed debit and credit totals and item counts must equal the totals printed on the statement. A statement that does not add up is reported in MoneyMoney and left in the inbox; nothing of it is booked. |
| 5 | Good statements are moved to `~/Library/Application Support/SCBBridge/statements/<account number>/<from>_<to>.pdf`. SCB names every statement `AcctSt_<Mon><YY>.pdf`, for every account, so the file name is never used for anything. A byte-identical statement read twice is simply dropped. |
| 6 | Every booking gets a stable id from account, date, time, amount, balance and description, so overlapping statements never produce duplicates in the bridge's store. The extension delivers the whole store on every refresh; MoneyMoney discards what it already holds. |
| 7 | The balance comes from the statement that lists transactions and ends last. A statement for a period without transactions is valid, but SCB prints no balance on it at all; it only carries the known balance forward when it follows on without a gap. As long as no statement with transactions has been read for an account, the extension reports that its balance is not known yet instead of showing one. |
| 8 | The counterparty name is taken from the description (`Transfer to KBNK x3984 Mrs. ...`, `... B/O David Lemke`). The purpose is the description, the note and the booking time; the time keeps two otherwise identical bookings on the same day apart, which MoneyMoney could discard as duplicates. Channel and code become the booking text. MoneyMoney's auto-categorisation applies. |
| 9 | With a start date set, bookings before it stay in the store and count for the balance, but are never delivered to MoneyMoney. See below. |

Booking dates are anchored at 12:00 UTC so that a time zone change or a DST switch on the Mac never makes MoneyMoney import a day twice.

HTTPS keeps the statement password encrypted even on the loopback interface, and a certificate issued for `127.0.0.1` fails for any other host name, which shuts out web pages trying DNS rebinding. MoneyMoney does not consult the macOS keychain for certificates: it asks once whether to trust the bridge's self-signed certificate and remembers its fingerprint.

## Installation

```bash
git clone https://github.com/davyd15/moneymoney-scb.git
cd moneymoney-scb
./install.sh --inbox ~/Downloads      # or any folder your mail client saves attachments to
./install.sh --start 2026-09-12       # optional, see "Switching from accounts you kept by hand"
```

The installer copies `SCB.lua` into MoneyMoney's extensions folder and the bridge into `~/Library/Application Support/SCBBridge/`, creates the bridge's certificate for `127.0.0.1` once (it is kept on every later install, so MoneyMoney does not ask again) and loads a LaunchAgent (`com.moneymoney-scb.bridge`) with socket activation. It prints the certificate's SHA-256 fingerprint.

Then in MoneyMoney:

1. Put the statement PDFs into the inbox: the account list comes from the statements the bridge has read. For an account that has not moved lately, add the month of its last transaction, because only a statement with transactions shows a balance. Statements of two accounts for the same month have the same file name; let the browser or Finder keep both (the bridge reads the account number from the PDF).
2. Reload the extensions (right-click an account, *Reload Extensions*) or restart MoneyMoney.
3. Add an account and choose the service **SCB Thailand (Statement PDF)**. User name: anything. Password: the password of your statement PDFs. MoneyMoney keeps it; the bridge receives it per refresh and never stores it.
4. On the first connection MoneyMoney shows the bridge's certificate. Accept it if the SHA-256 fingerprint matches the one the installer printed.

Requirements: macOS, MoneyMoney, Python 3.11 or newer with `pypdf` and `cryptography` (the installer installs them). If the inbox is `~/Downloads`, `~/Documents` or `~/Desktop`, macOS may ask once whether Python may access that folder; the extension reports it when access is missing.

`./uninstall.sh` removes everything except the archived statements and the store; `./uninstall.sh --purge` removes those too.

## Switching from accounts you kept by hand

If you have tracked your SCB accounts as offline accounts in MoneyMoney, keep them as the archive and let the extension take over from a start date:

1. Pick the day after your last manual entry and install with it: `./install.sh --inbox ~/Downloads --start 2026-09-12`. The date is stored in `~/Library/Application Support/SCBBridge/config.json` and kept by every later install; `--start off` removes it.
2. Bring the old offline account to zero with one transfer entry on the last manual day, so its money is not counted twice next to the new account.
3. Monthly statements from before the start date are still worth reading: they establish the balance, they just never reach MoneyMoney as transactions.

Your manual history and its categories stay exactly as they are, including everything older than the 12 months SCB still provides.

## Command line alternative

`scb_import.py` books statements into MoneyMoney **offline** accounts through the AppleScript interface, without the bridge. It matches the offline account by the account number entered in MoneyMoney, skips transactions that already exist (same booking date and amount, so manually typed entries are recognised) and checks the period-end balance against the statement. It reads the PDF password from the Keychain item `scb-statement`.

```bash
pip3 install -r requirements.txt
security add-generic-password -a $USER -s scb-statement -w   # once
python3 scb_import.py ~/Downloads/AcctSt_Sep26.pdf            # or --dry-run, --keep
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The fixtures are synthetic statements that copy the line structure of real ones (with and without transactions, a page break, a wrapped description); no bank data is part of the repository.

## Security

- No banking login, no PIN, no session with the bank. The bridge listens on 127.0.0.1 only and touches local files and nothing else.
- The PDF password lives in MoneyMoney (extension) or in the macOS Keychain (command line), never in code, configuration or the bridge's store.
- The bridge's certificate is self-signed for `127.0.0.1` (`CA:FALSE`, server authentication only) and is not added to the macOS keychain.
- Statements and the booking store contain personal data and stay under `~/Library/Application Support/SCBBridge/`; the repository ignores `*.pdf`, `*.csv`, `*.plist` and `statements/`.

## Changelog

| Version | Change |
|---------|--------|
| 1.2 | Start date (`--start`, kept in `config.json`) for switching from accounts kept by hand: older bookings count for the balance but are never delivered. The booking time is part of the purpose, so identical bookings on the same day stay apart. Monthly statements documented. Verified with the live bridge: start date applied and kept across a reinstall without `--start`. |
| 1.1 | Statements for a period without transactions are accepted; SCB prints no balance on them. The balance now comes from the latest statement with transactions and is carried forward by gap-free statements without; an account without a known balance says so in MoneyMoney. Clearer errors (which header field is missing, missing totals). The installer no longer touches the macOS keychain, which MoneyMoney does not use, and prints the certificate fingerprint instead. Unit tests with synthetic statements. Verified with a real statement without transactions and the store written by 1.0. |
| 1.0 | Extension `SCB.lua` plus local bridge `scb_bridge.py` with socket activation, inbox scanning, stable booking ids, balance from the newest statement, installer and uninstaller. Bridge verified end to end with a real statement (parse, archive, duplicate drop, wrong password, unknown account); the extension logic verified with a stand-in for MoneyMoney's runtime. |
| 0.1 | Command line importer `scb_import.py` for offline accounts: parser, reconciliation, account matching by number, AppleScript booking with duplicate detection, balance check, archiving. Verified against a statement whose transactions were all entered by hand: 0 booked, balance check passed. |

## License

MIT, see [LICENSE](LICENSE).
