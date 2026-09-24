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

# Installs of bridge 1.0 also trusted the certificate in the login keychain.
if [ -f "$BRIDGE_DIR/cert.pem" ] && security verify-cert -c "$BRIDGE_DIR/cert.pem" -p ssl -n 127.0.0.1 2>/dev/null | grep -q successful; then
    security remove-trusted-cert "$BRIDGE_DIR/cert.pem" 2>/dev/null && ok "keychain trust from bridge 1.0 removed" \
        || warn "Keychain trust not removed. Delete the certificate \"127.0.0.1\" in Keychain Access if you like."
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
