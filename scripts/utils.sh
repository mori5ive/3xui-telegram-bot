#!/bin/bash
C_GREEN='\033[0;32m'
C_YELLOW='\033[1;33m'
C_RED='\033[0;31m'
C_CYAN='\033[0;36m'
C_NC='\033[0m'

info()  { echo -e "${C_CYAN}[*]${C_NC} $1"; }
ok()    { echo -e "${C_GREEN}[OK]${C_NC} $1"; }
warn()  { echo -e "${C_YELLOW}[!]${C_NC} $1"; }
err()   { echo -e "${C_RED}[X]${C_NC} $1"; }

ask_required() {
    local prompt="$1" __resultvar="$2" value=""
    while [ -z "$value" ]; do
        read -rp "$prompt: " value
        value="$(echo -n "$value" | xargs)"
        [ -z "$value" ] && warn "This field is required and cannot be empty. Please try again."
    done
    printf -v "$__resultvar" '%s' "$value"
}

ask_optional() {
    local prompt="$1" default="$2" __resultvar="$3" value=""
    read -rp "$prompt: " value
    value="$(echo -n "$value" | xargs)"
    [ -z "$value" ] && value="$default"
    printf -v "$__resultvar" '%s' "$value"
}

ask_yesno() {
    local prompt="$1" __resultvar="$2" value=""
    while [[ "$value" != "y" && "$value" != "n" ]]; do
        read -rp "$prompt (y/n): " value
        value="$(echo -n "$value" | xargs | tr '[:upper:]' '[:lower:]')"
        [[ "$value" != "y" && "$value" != "n" ]] && warn "Please enter y or n only."
    done
    printf -v "$__resultvar" '%s' "$value"
}

ask_number() {
    local prompt="$1" __resultvar="$2" value=""
    while [[ ! "$value" =~ ^[0-9]+$ ]]; do
        read -rp "$prompt: " value
        value="$(echo -n "$value" | xargs)"
        [[ ! "$value" =~ ^[0-9]+$ ]] && warn "Please enter a whole number."
    done
    printf -v "$__resultvar" '%s' "$value"
}

ask_password() {
    local prompt="$1" __resultvar="$2" value=""
    while [ -z "$value" ]; do
        read -rsp "$prompt: " value
        echo ""
        [ -z "$value" ] && warn "This field is required and cannot be empty. Please try again."
    done
    printf -v "$__resultvar" '%s' "$value"
}

is_port_free() {
    local port="$1"
    if command -v ss &> /dev/null; then
        ! ss -tln | grep -q ":${port} "
    else
        ! (echo > /dev/tcp/127.0.0.1/"$port") 2>/dev/null
    fi
}

random_path_secret() {
    if command -v openssl &> /dev/null; then
        openssl rand -hex 16
    else
        head -c 32 /dev/urandom | md5sum | cut -d' ' -f1
    fi
}

get_public_ip() {
    curl -s --max-time 8 https://api.ipify.org || curl -s --max-time 8 https://ifconfig.me || echo ""
}

install_caddy_if_needed() {
    if command -v caddy &> /dev/null; then
        return 0
    fi
    info "Installing Caddy..."
    if command -v apt-get &> /dev/null; then
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg 2>/dev/null
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
            gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq
        apt-get install -y -qq caddy
    elif command -v dnf &> /dev/null; then
        dnf install -y -q 'dnf-command(copr)'
        dnf copr enable -y @caddy/caddy
        dnf install -y -q caddy
    else
        err "Automatic Caddy install is not supported on this OS. Please install Caddy manually."
        return 1
    fi
}
