#!/bin/bash
# SCB Thailand MoneyMoney Extension: installer
# https://github.com/davyd15/moneymoney-scb
#
#   ./install.sh                      inbox: ~/Library/Application Support/SCBBridge/inbox
#   ./install.sh --inbox ~/Downloads  inbox: the folder your mail client saves attachments to
#
# Installs the extension into MoneyMoney, the bridge into Application Support,
# a LaunchAgent that starts the bridge on demand (socket activation) and the
# bridge's certificate into your login keychain (macOS asks for your password
# once). Safe to run again: everything is replaced in place.
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
step() { echo -e "\n${BLUE}▶ $*${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
die()  { echo -e "  ${RED}✗${NC}  $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXTENSIONS_DIR="$HOME/Library/Containers/com.moneymoney-app.retail/Data/Library/Application Support/MoneyMoney/Extensions"
BRIDGE_DIR="$HOME/Library/Application Support/SCBBridge"
LOG_FILE="$HOME/Library/Logs/scb-bridge.log"
LABEL="com.moneymoney-scb.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=8766
INBOX="$BRIDGE_DIR/inbox"

while [ $# -gt 0 ]; do
    case "$1" in
        --inbox) INBOX="${2:?--inbox needs a folder}"; shift 2 ;;
        *) die "Unknown argument: $1" ;;
    esac
done
INBOX="${INBOX/#\~/$HOME}"

echo -e "\n${BOLD}SCB Thailand (Statement PDF): MoneyMoney extension installer${NC}"
echo "──────────────────────────────────────────────────────────────"

# ── 1. Python with pypdf and cryptography ────────────────────────────────────
step "Finding Python 3..."
PYTHON=""
for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    [ -x "$candidate" ] && { PYTHON="$candidate"; break; }
done
[ -n "$PYTHON" ] || die "Python 3 not found. Install it with: brew install python"
PYTHON_REAL="$("$PYTHON" -c 'import sys; print(sys.executable)')"  # launchd needs the real binary, not a shim
ok "Using $PYTHON_REAL ($("$PYTHON" --version 2>&1))"

step "Installing Python packages (pypdf, cryptography)..."
if "$PYTHON" -c 'import pypdf, cryptography' 2>/dev/null; then
    ok "already installed"
else
    "$PYTHON" -m pip install --quiet pypdf cryptography 2>/dev/null \
        || "$PYTHON" -m pip install --quiet --break-system-packages pypdf cryptography \
        || die "pip failed. Try: $PYTHON -m pip install pypdf cryptography"
    ok "installed"
fi

# ── 2. Files ─────────────────────────────────────────────────────────────────
step "Installing files..."
[ -d "$EXTENSIONS_DIR" ] || die "MoneyMoney extensions folder not found. Start MoneyMoney once, then run this again."
cp "$SCRIPT_DIR/SCB.lua" "$EXTENSIONS_DIR/"
ok "SCB.lua → MoneyMoney extensions"
mkdir -p "$BRIDGE_DIR" "$INBOX" "$(dirname "$LOG_FILE")"
cp "$SCRIPT_DIR/scb_bridge.py" "$SCRIPT_DIR/scb_statement.py" "$BRIDGE_DIR/"
ok "scb_bridge.py, scb_statement.py → $BRIDGE_DIR"
ok "inbox: $INBOX"

# ── 3. Certificate ───────────────────────────────────────────────────────────
step "Certificate for https://127.0.0.1:$PORT..."
"$PYTHON" "$BRIDGE_DIR/scb_bridge.py" --init-cert >/dev/null
if security verify-cert -c "$BRIDGE_DIR/cert.pem" -p ssl -n 127.0.0.1 2>/dev/null | grep -q "successful"; then
    ok "already trusted"
else
    echo "  macOS asks for your login password once to trust the bridge's certificate."
    security add-trusted-cert -r trustRoot -p ssl -k "$HOME/Library/Keychains/login.keychain-db" "$BRIDGE_DIR/cert.pem" \
        && ok "trusted for TLS on 127.0.0.1" \
        || warn "Not trusted. MoneyMoney will refuse the bridge until you run:
     security add-trusted-cert -r trustRoot -p ssl -k ~/Library/Keychains/login.keychain-db \"$BRIDGE_DIR/cert.pem\""
fi

# ── 4. LaunchAgent with socket activation ────────────────────────────────────
step "Installing LaunchAgent..."
mkdir -p "$(dirname "$PLIST")"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_REAL</string>
        <string>$BRIDGE_DIR/scb_bridge.py</string>
        <string>--inbox</string>
        <string>$INBOX</string>
    </array>
    <key>ProcessType</key>
    <string>Background</string>
    <!-- launchd holds port $PORT. The bridge starts on the first connection from
         MoneyMoney and exits after two minutes without requests. -->
    <key>Sockets</key>
    <dict>
        <key>Listeners</key>
        <dict>
            <key>SockServiceName</key>
            <string>$PORT</string>
            <key>SockNodeName</key>
            <string>127.0.0.1</string>
            <key>SockFamily</key>
            <string>IPv4</string>
        </dict>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
</dict>
</plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$PLIST" && ok "loaded, listening on 127.0.0.1:$PORT" \
    || warn "Could not load the LaunchAgent. Run: launchctl bootstrap gui/$(id -u) \"$PLIST\""

# ── Done ─────────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}${BOLD}Installation complete.${NC}"
echo ""
echo "  1. In MoneyMoney: reload the extensions (right-click an account → Reload Extensions) or restart it."
echo "  2. Add an account: choose the service \"SCB Thailand (Statement PDF)\"."
echo "     User name: anything. Password: the password of your SCB statement PDFs."
echo "  3. Request a statement in the SCB EASY app, save the PDF into"
echo "     $INBOX"
echo "     and refresh the account in MoneyMoney."
echo ""
echo "  Bridge log: $LOG_FILE"
echo ""
