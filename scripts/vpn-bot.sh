#!/bin/bash
BOT_DIR="{{BOT_DIR}}"
SERVICE="vpnbot"
WEBPANEL_SERVICE="webpanel"
source "$BOT_DIR/scripts/utils.sh" 2>/dev/null || true
BACKUP_DIR="${BOT_DIR}/backups"
mkdir -p "$BACKUP_DIR"

C_GREEN='\033[0;32m'
C_YELLOW='\033[1;33m'
C_RED='\033[0;31m'
C_CYAN='\033[0;36m'
C_BLUE='\033[0;34m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_NC='\033[0m'

info()  { echo -e "${C_CYAN}[*]${C_NC} $1"; }
ok()    { echo -e "${C_GREEN}[OK]${C_NC} $1"; }
warn()  { echo -e "${C_YELLOW}[!]${C_NC} $1"; }
err()   { echo -e "${C_RED}[X]${C_NC} $1"; }
pause() { read -rp "Press Enter to continue..." _; }

env_get() {
    python3 -c "
import sys
key = sys.argv[1]
path = sys.argv[2]
try:
    for line in open(path, encoding='utf-8'):
        line = line.rstrip('\n')
        if line.startswith(key + '='):
            print(line.split('=', 1)[1])
            break
except FileNotFoundError:
    pass
" "$1" "$BOT_DIR/.env"
}

env_set() {
    python3 -c "
import sys
key, value, path = sys.argv[1], sys.argv[2], sys.argv[3]
lines = []
found = False
try:
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
except FileNotFoundError:
    pass
for i, line in enumerate(lines):
    if line.startswith(key + '='):
        lines[i] = f'{key}={value}'
        found = True
        break
if not found:
    lines.append(f'{key}={value}')
with open(path, 'w', encoding='utf-8') as f:
    f.write('\n'.join(l for l in lines if l != '') + '\n')
" "$1" "$2" "$BOT_DIR/.env"
}

mask() {
    local v="$1"
    local len=${#v}
    if [ "$len" -le 8 ]; then
        echo "****"
    else
        echo "${v:0:4}****${v: -4}"
    fi
}

maybe_restart() {
    read -rp "Restart the bot now to apply changes? (y/n): " R
    if [ "$R" == "y" ]; then
        systemctl restart $SERVICE
        ok "Restarted."
    fi
}

status_line() {
    if systemctl is-active --quiet $SERVICE; then
        echo -e "${C_GREEN}${C_BOLD}● RUNNING${C_NC}"
    else
        echo -e "${C_RED}${C_BOLD}● STOPPED${C_NC}"
    fi
}

do_backup() {
    TS=$(date +%Y%m%d_%H%M%S)
    OUT="$BACKUP_DIR/backup_$TS.tar.gz"
    echo -e "${C_CYAN}Creating backup...${C_NC}"
    tar -czf "$OUT" -C "$BOT_DIR" \
        .env config.py database.py main.py requirements.txt vpn_bot.db \
        handlers keyboards middlewares services utils scripts 2>/dev/null || true
    ok "Backup saved: $OUT"
}

do_restore() {
    echo -e "${C_CYAN}Available backups:${C_NC}"
    ls -1t "$BACKUP_DIR" 2>/dev/null | nl
    echo ""
    read -rp "Enter backup number (or full file path): " CHOICE
    if [[ "$CHOICE" =~ ^[0-9]+$ ]]; then
        FILE=$(ls -1t "$BACKUP_DIR" | sed -n "${CHOICE}p")
        FILE="$BACKUP_DIR/$FILE"
    else
        FILE="$CHOICE"
    fi
    [ ! -f "$FILE" ] && { err "File not found."; return; }
    warn "This will overwrite the current bot files."
    read -rp "Are you sure? (y/n): " CONFIRM
    [ "$CONFIRM" != "y" ] && { echo "Cancelled."; return; }
    systemctl stop $SERVICE
    tar -xzf "$FILE" -C "$BOT_DIR"
    systemctl start $SERVICE
    ok "Restore complete, bot restarted."
}

view_db_stats() {
    DB="$BOT_DIR/vpn_bot.db"
    [ ! -f "$DB" ] && { err "Database not found."; return; }
    echo -e "${C_CYAN}--- Quick database stats ---${C_NC}"
    printf "%-16s: " "Users";            sqlite3 "$DB" "SELECT COUNT(*) FROM wallets;" 2>/dev/null || echo "N/A"
    printf "%-16s: " "Orders";           sqlite3 "$DB" "SELECT COUNT(*) FROM orders;" 2>/dev/null || echo "N/A"
    printf "%-16s: " "Total revenue";    sqlite3 "$DB" "SELECT COALESCE(SUM(amount_paid),0) FROM orders;" 2>/dev/null || echo "N/A"
    printf "%-16s: " "Pending payments"; sqlite3 "$DB" "SELECT COUNT(*) FROM pending_payments WHERE status='pending';" 2>/dev/null || echo "N/A"
}

reinstall_deps() {
    cd "$BOT_DIR"
    echo -e "${C_CYAN}Updating Python packages...${C_NC}"
    .venv/bin/python3 -m pip install --upgrade pip -q
    .venv/bin/python3 -m pip install -r requirements.txt -q --upgrade
    ok "Packages updated."
    read -rp "Restart the bot now? (y/n): " R
    [ "$R" == "y" ] && systemctl restart $SERVICE
}

health_check() {
    echo -e "${C_CYAN}Running health checks...${C_NC}"
    echo ""
    printf "%-30s" "Bot service:"
    systemctl is-active --quiet $SERVICE && echo -e "${C_GREEN}running${C_NC}" || echo -e "${C_RED}not running${C_NC}"

    printf "%-30s" ".env file:"
    [ -f "$BOT_DIR/.env" ] && echo -e "${C_GREEN}found${C_NC}" || echo -e "${C_RED}missing${C_NC}"

    [ -f "$BOT_DIR/.env" ] && { set -a; source "$BOT_DIR/.env"; set +a; }

    printf "%-30s" "Telegram bot token:"
    if [ -n "$BOT_TOKEN" ]; then
        TG=$(curl -s --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null)
        if echo "$TG" | grep -q '"ok":true'; then
            U=$(echo "$TG" | grep -oP '"username":"\K[^"]+')
            echo -e "${C_GREEN}valid (@${U})${C_NC}"
        else
            echo -e "${C_RED}invalid or unreachable${C_NC}"
        fi
    else
        echo -e "${C_YELLOW}not set${C_NC}"
    fi

    printf "%-30s" "SOCKS5 proxy:"
    if [ -n "$PROXY_HOST" ] && [ -n "$PROXY_PORT" ]; then
        if timeout 3 bash -c "cat < /dev/null > /dev/tcp/$PROXY_HOST/$PROXY_PORT" 2>/dev/null; then
            echo -e "${C_GREEN}reachable (${PROXY_HOST}:${PROXY_PORT})${C_NC}"
        else
            echo -e "${C_RED}cannot connect${C_NC}"
        fi
    else
        echo -e "${C_DIM}disabled (direct connection)${C_NC}"
    fi

    printf "%-30s" "3X-UI panel:"
    if [ -n "$PANEL_URL" ]; then
        CODE=$(curl -sk --max-time 10 -o /dev/null -w "%{http_code}" "${PANEL_URL}${PANEL_PATH}/login" 2>/dev/null)
        [ -n "$CODE" ] && [ "$CODE" != "000" ] && echo -e "${C_GREEN}reachable (HTTP $CODE)${C_NC}" || echo -e "${C_RED}unreachable${C_NC}"
    else
        echo -e "${C_YELLOW}not set${C_NC}"
    fi

    printf "%-30s" "Panel API token:"
    if [ -n "$PANEL_URL" ] && [ -n "$PANEL_API_TOKEN" ]; then
        RES=$(curl -sk --max-time 10 -H "Authorization: Bearer ${PANEL_API_TOKEN}" "${PANEL_URL}${PANEL_PATH}/panel/api/clients/list" 2>/dev/null)
        echo "$RES" | grep -q '"success":true' && echo -e "${C_GREEN}valid${C_NC}" || echo -e "${C_RED}invalid or unreachable${C_NC}"
    else
        echo -e "${C_YELLOW}not set${C_NC}"
    fi

    printf "%-30s" "Database file:"
    if [ -f "$BOT_DIR/vpn_bot.db" ]; then
        SZ=$(du -h "$BOT_DIR/vpn_bot.db" 2>/dev/null | cut -f1)
        echo -e "${C_GREEN}found (${SZ})${C_NC}"
    else
        echo -e "${C_YELLOW}not found yet${C_NC}"
    fi

    printf "%-30s" "Disk space:"
    echo -e "${C_CYAN}$(df -h "$BOT_DIR" 2>/dev/null | awk 'NR==2 {print $4}') free${C_NC}"
    echo ""
}

uninstall_bot() {
    warn "This removes the systemd service and the vpn-bot command."
    echo "Bot files in $BOT_DIR will NOT be deleted."
    read -rp "Are you sure? (y/n): " CONFIRM
    if [ "$CONFIRM" == "y" ]; then
        systemctl stop $SERVICE 2>/dev/null || true
        systemctl disable $SERVICE 2>/dev/null || true
        rm -f /etc/systemd/system/${SERVICE}.service
        systemctl daemon-reload
        rm -f /usr/local/bin/vpn-bot
        echo "Removed. Bot files remain in $BOT_DIR."
    fi
}


webpanel_status_line() {
    if systemctl is-active --quiet $WEBPANEL_SERVICE 2>/dev/null; then
        echo -e "${C_GREEN}${C_BOLD}â RUNNING${C_NC}"
    else
        echo -e "${C_RED}${C_BOLD}â STOPPED${C_NC}"
    fi
}

webpanel_link() {
    local port ip
    port=$(env_get WEBPANEL_PORT)
    ip=$(get_public_ip 2>/dev/null)
    echo "http://${ip:-YOUR-SERVER-IP}:${port}/"
}

webpanel_userpass() {
    clear
    echo -e "${C_CYAN}--- Web Panel: Username / Password ---${C_NC}"
    echo "Current username: $(env_get PANEL_ADMIN_USER)"
    echo ""
    echo "1) Change username"
    echo "2) Change password"
    echo "0) Back"
    read -rp "Choose: " C
    case $C in
        1) read -rp "New username: " V; env_set PANEL_ADMIN_USER "$V"; systemctl restart $WEBPANEL_SERVICE; ok "Updated." ;;
        2) read -rsp "New password: " V; echo ""; env_set PANEL_ADMIN_PASS "$V"; systemctl restart $WEBPANEL_SERVICE; ok "Updated." ;;
    esac
}

webpanel_info() {
    clear
    echo -e "${C_CYAN}--- Web Panel: Info ---${C_NC}"
    echo "Status: $(webpanel_status_line)"
    echo "Username: $(env_get PANEL_ADMIN_USER)"
    echo "Link: $(webpanel_link)"
}

webpanel_reset() {
    clear
    warn "This restarts the web panel service."
    read -rp "Are you sure? (y/n): " CONFIRM
    [ "$CONFIRM" != "y" ] && return
    systemctl restart $WEBPANEL_SERVICE
    ok "Web panel restarted."
}

webpanel_change_port() {
    clear
    echo -e "${C_CYAN}--- Web Panel: Change Port ---${C_NC}"
    echo "Current port: $(env_get WEBPANEL_PORT)"
    echo ""
    NEW_PORT=""
    while [ -z "$NEW_PORT" ]; do
        read -rp "New port: " NEW_PORT
        if ! [[ "$NEW_PORT" =~ ^[0-9]+$ ]]; then
            warn "Please enter a numeric port."
            NEW_PORT=""
        elif ! is_port_free "$NEW_PORT"; then
            warn "Port $NEW_PORT is already in use."
            NEW_PORT=""
        fi
    done
    env_set WEBPANEL_PORT "$NEW_PORT"
    sed -i "s|--port [0-9]*|--port ${NEW_PORT}|" /etc/systemd/system/webpanel.service
    systemctl daemon-reload
    systemctl restart $WEBPANEL_SERVICE
    ok "Port changed. New link: $(webpanel_link)"
}

webpanel_settings() {
    while true; do
        clear
        echo -e "${C_CYAN}${C_BOLD}--- Web Panel Settings ---${C_NC}"
        echo "1) Username / Password"
        echo "2) Change port"
        echo "3) Info (panel link)"
        echo "4) Reset (restart panel)"
        echo "0) Back to main menu"
        read -rp "Choose: " C
        case $C in
            1) webpanel_userpass; pause ;;
            2) webpanel_change_port; pause ;;
            3) webpanel_info; pause ;;
            4) webpanel_reset; pause ;;
            0) break ;;
        esac
    done
}

edit_env() {
    EDITOR_BIN="${EDITOR:-nano}"
    command -v "$EDITOR_BIN" &> /dev/null || EDITOR_BIN="vi"
    "$EDITOR_BIN" "$BOT_DIR/.env"
    maybe_restart
}

show_menu() {
    clear
    echo -e "${C_BLUE}${C_BOLD}╔══════════════════════════════════════════════════╗${C_NC}"
    echo -e "${C_BLUE}${C_BOLD}║          🤖  VPN Sales Bot — Control Panel         ║${C_NC}"
    echo -e "${C_BLUE}${C_BOLD}╚══════════════════════════════════════════════════╝${C_NC}"
    echo -e "  Bot status:    $(status_line)"
    echo -e "  Web panel:     $(webpanel_status_line)  ($(webpanel_link 2>/dev/null))"
    echo -e "${C_DIM}──────────────────────────────────────────────────────${C_NC}"
    echo -e "  ${C_GREEN}1)${C_NC}  🟢 Start bot"
    echo -e "  ${C_GREEN}2)${C_NC}  🔴 Stop bot"
    echo -e "  ${C_GREEN}3)${C_NC}  🔄 Restart bot"
    echo -e "  ${C_GREEN}4)${C_NC}  📊 Service status"
    echo -e "  ${C_GREEN}5)${C_NC}  📜 Live logs (Ctrl+C to exit)"
    echo -e "  ${C_GREEN}6)${C_NC}  📄 Last 100 log lines"
    echo -e "  ${C_GREEN}7)${C_NC}  ⚙️  Raw .env editor"
    echo -e "  ${C_GREEN}8)${C_NC}  💾 Create backup"
    echo -e "  ${C_GREEN}9)${C_NC}  ♻️  Restore from backup"
    echo -e "  ${C_GREEN}10)${C_NC} 📈 Quick database stats"
    echo -e "  ${C_GREEN}11)${C_NC} 📦 Update Python packages"
    echo -e "  ${C_GREEN}12)${C_NC} 🩺 Health check"
    echo -e "  ${C_GREEN}13)${C_NC} 🗑  Uninstall CLI & service"
    echo -e "  ${C_GREEN}14)${C_NC} 🌐 Web Panel Settings"
    echo -e "  ${C_RED}0)${C_NC}  Exit"
    echo -e "${C_DIM}──────────────────────────────────────────────────────${C_NC}"
}

if [ -n "$1" ]; then
    case "$1" in
        start) systemctl start $SERVICE; ok "Started." ;;
        stop) systemctl stop $SERVICE; warn "Stopped." ;;
        restart) systemctl restart $SERVICE; ok "Restarted." ;;
        status) systemctl status $SERVICE ;;
        logs) journalctl -u $SERVICE -f ;;
        backup) do_backup ;;
        restore) do_restore ;;
        health) health_check ;;
        *) err "Unknown command: $1" ;;
    esac
    exit 0
fi

while true; do
    show_menu
    read -rp "Choose an option: " CHOICE
    case $CHOICE in
        1) systemctl start $SERVICE; ok "Started."; pause ;;
        2) systemctl stop $SERVICE; warn "Stopped."; pause ;;
        3) systemctl restart $SERVICE; ok "Restarted."; pause ;;
        4) systemctl status $SERVICE --no-pager; pause ;;
        5) journalctl -u $SERVICE -f ;;
        6) journalctl -u $SERVICE -n 100 --no-pager; pause ;;
        7) edit_env ;;
        8) do_backup; pause ;;
        9) do_restore; pause ;;
        10) view_db_stats; pause ;;
        11) reinstall_deps; pause ;;
        12) health_check; pause ;;
        13) uninstall_bot; exit 0 ;;
        14) webpanel_settings ;;
        0) exit 0 ;;
        *) err "Invalid option."; pause ;;
    esac
done