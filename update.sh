#!/usr/bin/env bash
#
# ROTD (Runner of the Day) updater — mirrors the penny-early-warning updater.
#
# Downloads the newest version and installs it over the install directory
# (config.env, rotd.db and the logs are left untouched). It then reinstalls the
# dependencies, re-copies the systemd unit (so an ExecStart change like run -> listen
# is applied), runs the offline self-test and restarts the service.
#
# Usage:
#   bash update.sh                 # update to ROTD_VERSION (default main)
#   bash update.sh v1.0.0          # update to a tag
#   ROTD_INSTALL_DIR=/opt/rotd bash update.sh
#
set -euo pipefail

INSTALL_DIR="${ROTD_INSTALL_DIR:-/opt/rotd}"
ROTD_URL="${ROTD_URL:-https://github.com/Ace1337z/big-whale-penny-tracker}"
ROTD_VERSION="${1:-${ROTD_VERSION:-main}}"
SERVICE_NAME="rotd"
PYTHON_BIN="${PYTHON_BIN:-python3}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

ROTD="$INSTALL_DIR/.venv/bin/rotd"
[[ -x "$ROTD" ]] || fail "ROTD is not installed at $INSTALL_DIR (run install.sh first)"

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

# Keep a copy of the current code for manual rollback (config + db are untouched).
if [[ -d "$INSTALL_DIR" ]]; then
    BACKUP="$(mktemp -d)"
    info "backing up the current install to $BACKUP"
    $SUDO cp -r "$INSTALL_DIR/." "$BACKUP/" 2>/dev/null || true
fi

# 1. Download the new source tarball and overlay it (idempotent; config is preserved).
info "Step 1/4: downloading ROTD $ROTD_VERSION"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [[ "$ROTD_VERSION" == "main" ]]; then
    ARCHIVE="$ROTD_URL/archive/refs/heads/main.tar.gz"
else
    ARCHIVE="$ROTD_URL/archive/refs/tags/$ROTD_VERSION.tar.gz"
fi
wget -q -O "$TMP/src.tar.gz" "$ARCHIVE" || fail "download failed: $ARCHIVE"
mkdir -p "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1

# Do not overwrite the user's config or database.
rm -f "$TMP/src/config.env" "$TMP/src/rotd.db" 2>/dev/null || true
$SUDO cp -r "$TMP/src/." "$INSTALL_DIR/"
$SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR" 2>/dev/null || true

# 2. Reinstall dependencies (new deps may have been added).
info "Step 2/4: reinstalling dependencies"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR[dev]"

# 3. Re-copy the systemd unit so ExecStart changes (run -> listen) are applied.
info "Step 3/4: refreshing the systemd unit and self-test"
if [[ -f "$INSTALL_DIR/deploy/rotd.service" ]] && command -v systemctl >/dev/null 2>&1 \
   && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    RUN_USER="${ROTD_USER:-$(id -un)}"
    $SUDO cp "$INSTALL_DIR/deploy/rotd.service" "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO sed -i "s|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO systemctl daemon-reload
fi
"$ROTD" --config "$INSTALL_DIR/config.env" selftest || warn "self-test reported failures (see above)"

# 4. Restart the service.
info "Step 4/4: restarting the service"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    $SUDO systemctl --no-pager status "$SERVICE_NAME" | head -n 12 || true
    info "update complete. The bot now runs as a daemon (rotd listen) and replies to Telegram."
    info "Rollback: restore $BACKUP over $INSTALL_DIR and 'systemctl restart $SERVICE_NAME'."
else
    info "update complete. Start it with: $ROTD --config $INSTALL_DIR/config.env listen"
fi
