#!/usr/bin/env bash
#
# ROTD (Runner of the Day) installer — mirrors the penny-early-warning installer.
#
# Usage (run directly, NOT piped into bash, so it can read your answers):
#
#   wget -O install.sh https://raw.githubusercontent.com/Ace1337z/big-whale-penny-tracker/main/install.sh
#   bash install.sh
#
# Keep the exact filename with `wget -O install.sh`; otherwise a repeat download
# becomes install.sh.1 and `bash install.sh` keeps running the stale first copy.
#
# It is idempotent: running it again keeps existing values (shown masked; pressing
# Enter keeps them).
#
# Environment (non-interactive mode, skips the prompts):
#   ROTD_NONINTERACTIVE=1
#   ROTD_INSTALL_DIR=/opt/rotd
#   ROTD_VERSION=main            # or a tag such as v1.0.0
#   ROTD_URL=https://github.com/Ace1337z/big-whale-penny-tracker
#   TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...   # the only required credentials
#
# Setup asks for Telegram only, so the bot can be reached and every other key
# (Finviz, AI, SEC, Alpaca, Finnhub) can be set from the chat afterwards with
#   /setkey FINVIZ_TOKEN <token>        /keys        /help
#
set -euo pipefail

REPO_DEFAULT="https://github.com/Ace1337z/big-whale-penny-tracker"
INSTALL_DIR="${ROTD_INSTALL_DIR:-/opt/rotd}"
ROTD_URL="${ROTD_URL:-$REPO_DEFAULT}"
ROTD_VERSION="${ROTD_VERSION:-main}"
SERVICE_NAME="rotd"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# --- helpers ---------------------------------------------------------------
info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

need_root_or_sudo() {
    if [[ "$(id -u)" -ne 0 ]]; then
        if command -v sudo >/dev/null 2>&1; then
            SUDO="sudo"
        else
            warn "not root and no sudo: systemd installation will be skipped"
            SUDO=""
        fi
    else
        SUDO=""
    fi
}

# --- 1. credentials first --------------------------------------------------
info "ROTD (Runner of the Day) installer"
info "install directory: $INSTALL_DIR"
echo
info "Step 1/9: credentials (asked before anything is downloaded)"
echo "Secrets are read with hidden input and are never echoed."
echo "Only Telegram is required; every other key can be set later with /setkey KEY VALUE."

if [[ "${ROTD_NONINTERACTIVE:-0}" == "1" || ! -t 0 ]]; then
    info "non-interactive mode: reading values from the environment"
    TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-}"
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
else
    # Interactive: hidden input via /dev/tty (works even when stdin is a pipe).
    read -r -s -p "Telegram bot token (@BotFather): " TELEGRAM_TOKEN </dev/tty; echo
    read -r -s -p "Telegram chat id (or blank to auto-detect): " TELEGRAM_CHAT_ID </dev/tty; echo
fi

# Every optional credential is read from the environment only (not prompted). Set
# any of these to pre-fill them; otherwise use /setkey KEY VALUE from the chat.
FINVIZ_TOKEN="${FINVIZ_TOKEN:-}"
TWELVEDATA_KEY="${TWELVEDATA_KEY:-}"
FINNHUB_KEY="${FINNHUB_KEY:-}"
ALPHA_VANTAGE_KEY="${ALPHA_VANTAGE_KEY:-}"
ALPACA_KEY="${ALPACA_KEY:-}"; ALPACA_SECRET="${ALPACA_SECRET:-}"
SEC_USER_AGENT="${SEC_USER_AGENT:-}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-}"; OPENAI_API_KEY="${OPENAI_API_KEY:-}"; OPENAI_MODEL="${OPENAI_MODEL:-}"

# --- 2. system packages ----------------------------------------------------
info "Step 2/9: system packages"
need_root_or_sudo
if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y >/dev/null
    $SUDO apt-get install -y python3 python3-venv python3-pip wget tar >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 python3-pip wget tar >/dev/null
elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add python3 py3-virtualenv py3-pip wget tar >/dev/null
else
    warn "unknown package manager; ensure python3, venv, pip, wget and tar are installed"
fi

# --- 3. download the source ------------------------------------------------
info "Step 3/9: downloading the source"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [[ "$ROTD_VERSION" == "main" ]]; then
    ARCHIVE="$ROTD_URL/archive/refs/heads/main.tar.gz"
else
    ARCHIVE="$ROTD_URL/archive/refs/tags/$ROTD_VERSION.tar.gz"
fi
WGET_OPTS=(-q -O "$TMP/src.tar.gz")
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    WGET_OPTS+=(--header="Authorization: token $GITHUB_TOKEN")
fi
wget "${WGET_OPTS[@]}" "$ARCHIVE" || fail "download failed: $ARCHIVE"
mkdir -p "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO cp -r "$TMP/src/." "$INSTALL_DIR/"
$SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR" 2>/dev/null || true
cd "$INSTALL_DIR"

# --- 4. virtual environment ------------------------------------------------
info "Step 4/9: virtual environment and dependencies"
$PYTHON_BIN -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR[dev]"
ROTD="$INSTALL_DIR/.venv/bin/rotd"
if [[ ! -x "$ROTD" ]]; then
    fail "rotd entry point was not created; check the pip install output above."
fi

# --- 5. configuration ------------------------------------------------------
info "Step 5/9: configuration file"
export ROTD_HOME="$INSTALL_DIR"
CONFIG_FILE="$INSTALL_DIR/config.env"
if [[ -f "$CONFIG_FILE" ]]; then
    info "existing configuration found; values are kept unless you overwrite them"
fi
export TELEGRAM_TOKEN TELEGRAM_CHAT_ID FINVIZ_TOKEN TWELVEDATA_KEY FINNHUB_KEY
export ALPHA_VANTAGE_KEY ALPACA_KEY ALPACA_SECRET SEC_USER_AGENT
export OPENAI_BASE_URL OPENAI_API_KEY OPENAI_MODEL
"$ROTD" --config "$CONFIG_FILE" setup --non-interactive --skip-doctor
chmod 600 "$CONFIG_FILE" 2>/dev/null || true

# --- 6. Telegram chat id ---------------------------------------------------
info "Step 6/9: Telegram chat id (so the bot can receive your settings)"
if [[ -z "${TELEGRAM_CHAT_ID:-}" && -t 0 ]]; then
    "$ROTD" --config "$CONFIG_FILE" detect-chat || \
        warn "set the chat id later with: rotd setkey TELEGRAM_CHAT_ID <id>"
fi
echo
info "From the chat you can set anything else at any time, e.g.:"
echo "  /keys                     show what is set and what is missing"
echo "  /setkey FINVIZ_TOKEN <token>   set a key (secrets are saved, not echoed)"
echo "  /help                     all commands"

# --- 7. systemd unit -------------------------------------------------------
info "Step 7/9: systemd service"
RUN_USER="${ROTD_USER:-$(id -un)}"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    $SUDO cp "$INSTALL_DIR/deploy/rotd.service" "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO sed -i "s|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE_NAME" >/dev/null
    info "service installed and enabled"
else
    warn "systemd not installed by the installer. To enable it later run:"
    echo "  sudo cp $INSTALL_DIR/deploy/rotd.service /etc/systemd/system/rotd.service"
    echo "  sudo sed -i 's|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g' /etc/systemd/system/rotd.service"
    echo "  sudo systemctl daemon-reload && sudo systemctl enable --now rotd"
fi

# --- 8. self-test and doctor -----------------------------------------------
info "Step 8/9: offline self-test, then the live checker (doctor)"
"$ROTD" --config "$CONFIG_FILE" selftest || warn "self-test reported failures (see above)"
echo
"$ROTD" --config "$CONFIG_FILE" doctor || warn "doctor reported failures (see above)"

# --- 9. start --------------------------------------------------------------
info "Step 9/9: start the service"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    $SUDO systemctl --no-pager status "$SERVICE_NAME" | head -n 12 || true
    echo
    info "installed. Logs: journalctl -u $SERVICE_NAME -f"
else
    info "installed. Start it with: $ROTD --config $CONFIG_FILE run"
fi
