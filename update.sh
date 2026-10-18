#!/usr/bin/env bash
#
# ROTD (Runner of the Day) updater — mirrors the penny-early-warning updater.
#
# Downloads the newest version and installs it over the install directory
# (config.env, rotd.db and the logs are left untouched). It stops the daemon,
# reinstalls the dependencies, re-copies the systemd unit (so an ExecStart change
# like run -> listen is applied), runs the offline self-test and doctor, and
# restarts the service.
#
# Usage:
#   bash update.sh                 # update to ROTD_VERSION (default main)
#   bash update.sh v1.0.0          # update to a tag
#   ROTD_INSTALL_DIR=/opt/rotd bash update.sh
#
set -euo pipefail

INSTALL_DIR="${ROTD_INSTALL_DIR:-/opt/rotd}"
ROTD_URL="${ROTD_URL:-https://github.com/Ace1337z/Runner-of-the-Day}"
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

# Re-exec from a temporary copy. Step 1 replaces the whole tree — including this
# script — while bash is still reading it from disk, which makes bash resume at
# the wrong byte offset ("syntax error near unexpected token"). Reading from an
# unlinked temp copy is immune to that.
if [[ "${ROTD_UPDATE_RELOCATED:-0}" != "1" ]]; then
    SELF="$(mktemp -d)"
    cp "$0" "$SELF/update.sh"
    ROTD_UPDATE_RELOCATED=1 ROTD_UPDATE_SELF="$SELF" exec bash "$SELF/update.sh" "$@"
fi
SELF_TMP="${ROTD_UPDATE_SELF:-}"

# Keep a copy of the current code for manual rollback (config + db are untouched).
# The database is excluded on purpose: a live rotd.db can be tens of GB and
# copying it into /tmp would fill the disk (and is unnecessary — code rollback
# does not need the data).
if [[ -d "$INSTALL_DIR" ]]; then
    BACKUP="$(mktemp -d)"
    info "backing up the current code to $BACKUP (database and logs excluded)"
    if $SUDO tar -C "$INSTALL_DIR" -cf "$BACKUP/code.tar" \
        --exclude=rotd.db --exclude='rotd.db-*' --exclude=logs --exclude=.venv . 2>/dev/null; then
        info "code backup: $BACKUP/code.tar"
    else
        warn "code backup failed; continuing without one"
        BACKUP=""
    fi
fi

# 1. Download the new source tarball and overlay it (idempotent; config is preserved).
info "Step 1/5: downloading ROTD $ROTD_VERSION"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP" "${SELF_TMP:-}"' EXIT
if [[ "$ROTD_VERSION" == "main" ]]; then
    ARCHIVE="$ROTD_URL/archive/refs/heads/main.tar.gz"
else
    ARCHIVE="$ROTD_URL/archive/refs/tags/$ROTD_VERSION.tar.gz"
fi
wget -q -O "$TMP/src.tar.gz" "$ARCHIVE" || fail "download failed: $ARCHIVE"
mkdir -p "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1

# Stop the daemon before touching the package directory, so a running process
# never imports a half-written tree.
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]] \
   && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "stopping $SERVICE_NAME for the upgrade"
    $SUDO systemctl stop "$SERVICE_NAME" || warn "could not stop $SERVICE_NAME"
fi

# Do not overwrite the user's config or database.
rm -f "$TMP/src/config.env" "$TMP/src/rotd.db" 2>/dev/null || true
# The package tree is fully owned by the repo: replace it instead of overlaying,
# so modules deleted upstream (e.g. the removed twelvedata adapter) do not linger.
$SUDO rm -rf "$INSTALL_DIR/src/rotd"
$SUDO cp -r "$TMP/src/." "$INSTALL_DIR/"
$SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR" 2>/dev/null || true

# 2. Reinstall dependencies (new deps may have been added).
info "Step 2/5: reinstalling dependencies"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR[dev]"

# 3. Re-copy the systemd unit so ExecStart changes (run -> listen) are applied.
info "Step 3/5: refreshing the systemd unit"
if [[ -f "$INSTALL_DIR/deploy/rotd.service" ]] && command -v systemctl >/dev/null 2>&1 \
   && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    RUN_USER="${ROTD_USER:-$(id -un)}"
    $SUDO cp "$INSTALL_DIR/deploy/rotd.service" "/etc/systemd/system/$SERVICE_NAME.service" \
        || warn "could not install the systemd unit; keeping the existing one"
    $SUDO sed -i "s|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "/etc/systemd/system/$SERVICE_NAME.service" 2>/dev/null || true
    $SUDO systemctl daemon-reload || warn "daemon-reload failed"
fi
"$ROTD" --config "$INSTALL_DIR/config.env" selftest || warn "self-test reported failures (see above)"

# 4. Health check against the live config (sources, model, DB).
info "Step 4/5: doctor"
"$ROTD" --config "$INSTALL_DIR/config.env" doctor || warn "doctor reported warnings (see above)"

# 5. Restart the service.
info "Step 5/5: restarting the service"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    $SUDO systemctl restart "$SERVICE_NAME" || warn "restart failed — check: journalctl -u $SERVICE_NAME -n 50"
    sleep 2
    $SUDO systemctl --no-pager status "$SERVICE_NAME" 2>/dev/null | head -n 12 || true
    info "update complete. The bot now runs as a daemon (rotd listen) and replies to Telegram."
    if [[ -n "$BACKUP" ]]; then
        info "Rollback: sudo tar -C $INSTALL_DIR -xf $BACKUP/code.tar && sudo systemctl restart $SERVICE_NAME"
    fi
else
    info "update complete. Start it with: $ROTD --config $INSTALL_DIR/config.env listen"
fi

# Legacy databases keep their old page layout until rebuilt once. Report it
# instead of touching a file that may be tens of GB.
DB="$INSTALL_DIR/rotd.db"
if [[ -f "$DB" ]]; then
    DB_MB=$(( $(stat -c %s "$DB" 2>/dev/null || echo 0) / 1048576 ))
    if (( DB_MB > 5120 )); then
        warn "rotd.db is ${DB_MB} MB. Retention now prunes it, but the file only"
        warn "shrinks to its old high-water mark. To reclaim the space once:"
        warn "  systemctl stop $SERVICE_NAME"
        warn "  sqlite3 $DB 'VACUUM'   # needs ~2x the file size free"
        warn "  systemctl start $SERVICE_NAME"
    fi
fi
