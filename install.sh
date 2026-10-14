#!/usr/bin/env bash
#
# ROTD (Runner of the Day) — one-shot installer.
#
# Usage:
#   wget -O - https://raw.githubusercontent.com/Ace1337z/big-whale-penny-tracker/main/install.sh | bash
#   # or
#   curl -fsSL https://raw.githubusercontent.com/Ace1337z/big-whale-penny-tracker/main/install.sh | bash
#
# What it does:
#   1. Clones the repo and installs the package (editable, +dev extras).
#   2. Copies config.env.example -> $HOME/.rotd/rotd.env (mode 600).
#   3. Asks ONLY for the Telegram bot token + chat id (everything else is a
#      safe default and can be set later from Telegram, see below).
#   4. Runs `rotd doctor` to confirm the system is wired up.
#
# After install, add API keys later via Telegram by sending, from your chat:
#   /setkey FINVIZ_TOKEN yourtoken
#   /setkey TWELVEDATA_KEY yourkey
#   /setkey FINNHUB_KEY yourkey
#   /setkey ALPHA_VANTAGE_KEY yourkey
# (the /setkey command is wired only from your configured chat id)
#
set -euo pipefail

REPO="https://github.com/Ace1337z/big-whale-penny-tracker.git"
DEF_BRANCH="main"
CFG_DIR="$HOME/.rotd"
CFG_PATH="$CFG_DIR/rotd.env"
WORK_DIR="${ROTD_INSTALL_DIR:-$(pwd)}"

echo "== ROTD installer =="
echo "install dir : $WORK_DIR"
echo "config      : $CFG_PATH"

# --- 1. clone (or reuse) -----------------------------------------------------
if [ ! -d "$WORK_DIR/big-whale-penny-tracker" ]; then
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "$REPO" "$WORK_DIR/big-whale-penny-tracker"
  else
    echo "ERROR: git is required to install." >&2
    exit 1
  fi
else
  echo "repo already present at $WORK_DIR/big-whale-penny-tracker"
fi
cd "$WORK_DIR/big-whale-penny-tracker"

# --- 2. install --------------------------------------------------------------
if command -v pip >/dev/null 2>&1; then PIP="pip"; else PIP="pip3"; fi
$PIP install -e ".[dev]"

# --- 3. config file (gitignored, mode 600) -----------------------------------
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_PATH" ]; then
  cp config.env.example "$CFG_PATH"
  chmod 600 "$CFG_PATH"
  echo "created $CFG_PATH (mode 600)"
else
  echo "config already exists at $CFG_PATH (not overwritten)"
fi

# --- 4. the ONLY interactive question: Telegram ------------------------------
# All other keys default to '' (safe, system runs in shadow with Finviz only)
# and can be filled later from Telegram via /setkey.
set +e
read -r -p "Telegram bot token (from @BotFather, or blank to skip): " TG_TOKEN
read -r -p "Telegram chat id (your numeric id, or blank to skip): " TG_CHAT
set -e
if [ -n "${TG_TOKEN:-}" ]; then
  grep -v '^TELEGRAM_TOKEN=' "$CFG_PATH" > "$CFG_PATH.tmp" && mv "$CFG_PATH.tmp" "$CFG_PATH"
  printf 'TELEGRAM_TOKEN=%s\n' "$TG_TOKEN" >> "$CFG_PATH"
fi
if [ -n "${TG_CHAT:-}" ]; then
  grep -v '^TELEGRAM_CHAT_ID=' "$CFG_PATH" > "$CFG_PATH.tmp" && mv "$CFG_PATH.tmp" "$CFG_PATH"
  printf 'TELEGRAM_CHAT_ID=%s\n' "$TG_CHAT" >> "$CFG_PATH"
fi
chmod 600 "$CFG_PATH"

# --- 5. verify ---------------------------------------------------------------
export ROTD_CONFIG="$CFG_PATH"
echo "== rotd doctor =="
python -m rotd.cli doctor || true

echo
echo "Install complete. Next steps:"
echo "  rotd selftest            # offline self-test (no network)"
echo "  rotd shadow-start        # begin the 10-trading-day shadow clock"
echo "  rotd shadow              # run every decision, push nothing"
echo
echo "To add API keys later, message your bot from the configured chat with:"
echo "  /setkey FINVIZ_TOKEN <token>     # only key needed to run"
echo "  /setkey TWELVEDATA_KEY <key>     # fresh quotes"
echo "  /setkey FINNHUB_KEY <key>        # quote + market status"
echo "  /setkey ALPHA_VANTAGE_KEY <key>  # global quote"
echo
echo "(Without any key, ROTD still runs in shadow mode and the MISS REPORT"
echo "  will list every gate that is NOT_ENFORCED for lack of a feed.)"
