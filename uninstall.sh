#!/bin/bash
# SCB Thailand MoneyMoney Extension: uninstaller
#
#   ./uninstall.sh          removes extension, bridge, LaunchAgent and certificate trust
#   ./uninstall.sh --purge  also deletes the archived statements and the booking store
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }

EXTENSIONS_DIR="$HOME/Library/Containers/com.moneymoney-app.retail/Data/Library/Application Support/MoneyMoney/Extensions"
BRIDGE_DIR="$HOME/Library/Application Support/SCBBridge"
LABEL="com.moneymoney-scb.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PURGE=false
[ "${1:-}" = "--purge" ] && PURGE=true

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null && ok "LaunchAgent stopped" || true
rm -f "$PLIST" && ok "LaunchAgent removed"
rm -f "$EXTENSIONS_DIR/SCB.lua" && ok "SCB.lua removed from MoneyMoney"

if [ -f "$BRIDGE_DIR/cert.pem" ]; then
    security remove-trusted-cert "$BRIDGE_DIR/cert.pem" 2>/dev/null && ok "keychain trust removed" || true
    SHA1="$(openssl x509 -in "$BRIDGE_DIR/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)"
    security delete-certificate -Z "$SHA1" "$HOME/Library/Keychains/login.keychain-db" >/dev/null 2>&1 \
        && ok "certificate removed from the login keychain" || true
fi
rm -f "$BRIDGE_DIR/scb_bridge.py" "$BRIDGE_DIR/scb_statement.py" "$BRIDGE_DIR/cert.pem" "$BRIDGE_DIR/key.pem"
rm -rf "$BRIDGE_DIR/__pycache__"
ok "bridge removed"

if $PURGE; then
    rm -rf "$BRIDGE_DIR"
    ok "statements and booking store deleted"
else
    echo "  Kept: $BRIDGE_DIR (archived statements, store.json, inbox). Delete it with --purge."
fi
echo "  The accounts in MoneyMoney stay until you delete them there."
