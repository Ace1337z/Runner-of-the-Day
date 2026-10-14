#!/usr/bin/env bash
#
# ROTD (Runner of the Day) — one-shot installer.
#
# Usage (run in bash — Git Bash on Windows, WSL, or any Linux/macOS shell):
#   wget -O - https://raw.githubusercontent.com/Ace1337z/big-whale-penny-tracker/main/install.sh | bash
#   # or
#   curl -fsSL https://raw.githubusercontent.com/Ace1337z/big-whale-penny-tracker/main/install.sh | bash
#   # or download then run:
#   bash install.sh
#
# What it does:
#   1. Checks prerequisites (git, python3, pip).
#   2. Clones the repo and installs the package (editable, +dev extras).
#   3. Copies config.env.example -> $HOME/.rotd/rotd.env (mode 600).
#   4. Asks ONLY for the Telegram bot token + chat id (from the terminal, even
#      when piped). Everything else is a safe default and can be set later from
#      Telegram via /setkey.
#   5. Runs `rotd doctor` to confirm the system is wired up.
#
# Env overrides (mostly for testing):
#   ROTD_REPO / ROTD_BRANCH   — repo URL / branch to clone
#   ROTD_INSTALL_DIR          — where to clone (default: current dir)
#   ROTD_SKIP_INSTALL=1       — skip `pip install` (already installed)
#
set -euo pipefail

REPO="${ROTD_REPO:-https://github.com/Ace1337z/big-whale-penny-tracker.git}"
BRANCH="${ROTD_BRANCH:-main}"
CFG_DIR="$HOME/.rotd"
CFG_PATH="$CFG_DIR/rotd.env"
WORK_DIR="${ROTD_INSTALL_DIR:-$(pwd)}"

# --- helpers -------------------------------------------------------------
die() { echo "ERROR: $*" >&2; exit 1; }

# Prompt from the TERMINAL even when stdin is a pipe (wget | bash routes the
# script through stdin, so a bare `read` would consume script lines and never
# ask the user). We read from /dev/tty only when output is a real terminal, so
# the prompt always reaches the user at a real keyboard; in a non-interactive
# context (CI / no tty) it skips cleanly instead of hanging or swallowing lines.
ask() {
  local prompt="$1"
  if [ -t 1 ] && [ -e /dev/tty ]; then
    read -r -p "$prompt" REPLY < /dev/tty
  elif [ -t 0 ]; then
    read -r -p "$prompt" REPLY
  else
    REPLY=""
    echo "(non-interactive: skipping prompt — set TELEGRAM_TOKEN/TELEGRAM_CHAT_ID in $CFG_PATH later)" >&2
    return 1
  fi
}

# Persist one key into the config file, replacing any prior value.
set_key() {
  local key="$1" val="$2" tmp
  [ -z "$val" ] && return 0
  tmp="$(mktemp)"
  grep -v "^$key=" "$CFG_PATH" > "$tmp" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$CFG_PATH"
  chmod 600 "$CFG_PATH"
}

echo "== ROTD installer =="
echo "install dir : $WORK_DIR"
echo "config      : $CFG_PATH"

# --- 0. prerequisites ----------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required (https://git-scm.com/downloads)."
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else die "python3 is required (https://www.python.org/downloads)."; fi
if command -v pip >/dev/null 2>&1; then PIP=pip
elif command -v pip3 >/dev/null 2>&1; then PIP=pip3
else die "pip is required (it ships with Python 3)."; fi
echo "using: git, $PY, $PIP"

# --- 1. clone (or reuse) -------------------------------------------------
if [ ! -d "$WORK_DIR/big-whale-penny-tracker" ]; then
  echo "cloning $REPO ($BRANCH) ..."
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$WORK_DIR/big-whale-penny-tracker" \
    || die "git clone failed (network or repo unreachable)."
else
  echo "repo already present at $WORK_DIR/big-whale-penny-tracker"
fi
cd "$WORK_DIR/big-whale-penny-tracker"

# --- 2. install ----------------------------------------------------------
if [ -n "${ROTD_SKIP_INSTALL:-}" ]; then
  echo "skipping pip install (ROTD_SKIP_INSTALL set)"
elif [ -f pyproject.toml ] || [ -f setup.py ]; then
  echo "installing package (editable, +dev) ..."
  $PIP install -e ".[dev]" || die "pip install failed — see output above."
else
  die "pyproject.toml/setup.py not found in the cloned repo."
fi

# --- 3. config file (gitignored, mode 600) ------------------------------
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_PATH" ]; then
  [ -f config.env.example ] || die "config.env.example missing from the repo."
  cp config.env.example "$CFG_PATH"
  chmod 600 "$CFG_PATH"
  echo "created $CFG_PATH (mode 600)"
else
  echo "config already exists at $CFG_PATH (not overwritten)"
fi

# --- 4. the ONLY interactive question: Telegram --------------------------
# All other keys default to '' (safe) and can be filled later from Telegram
# via /setkey. This reads from /dev/tty so it prompts even under `wget | bash`.
set +e
ask "Telegram bot token (from @BotFather, or blank to skip): "
TG_TOKEN="$REPLY"
ask "Telegram chat id (your numeric id, or blank to skip): "
TG_CHAT="$REPLY"
set -e
if [ -n "${TG_TOKEN:-}" ]; then set_key "TELEGRAM_TOKEN" "$TG_TOKEN"; echo "  TELEGRAM_TOKEN saved"; fi
if [ -n "${TG_CHAT:-}" ];  then set_key "TELEGRAM_CHAT_ID" "$TG_CHAT"; echo "  TELEGRAM_CHAT_ID saved"; fi
chmod 600 "$CFG_PATH"

# --- 5. verify -----------------------------------------------------------
export ROTD_CONFIG="$CFG_PATH"
echo "== rotd doctor =="
$PY -m rotd.cli doctor || true

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
