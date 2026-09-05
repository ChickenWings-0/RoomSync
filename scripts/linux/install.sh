#!/usr/bin/env bash
#
# Install RoomSync's systemd user units and desktop launcher.
#
# The three files next to this script are TEMPLATES: they carry __PLACEHOLDER__
# tokens instead of paths, because a unit file cannot compute anything. systemd
# offers %h for the home directory but nothing for "where this repo happens to
# be", which interpreter is in use, or which browser is installed — so the
# substitution happens here, at install time, and the result is written to the
# user unit directory.
#
# Everything is --user scope. RoomSync needs a graphical session for screen
# capture and the tray icon, and a --system unit has neither.
#
#   ./install.sh              install and enable
#   ./install.sh --no-openrgb skip the OpenRGB unit
#   ./install.sh --uninstall  remove everything
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/RoomSync"
PROFILE_DIR="${DATA_DIR}/browser-profile"

WITH_OPENRGB=1
UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --no-openrgb) WITH_OPENRGB=0 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# ── Output helpers ───────────────────────────────────────────────────
if [ -t 1 ]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_OFF=$'\033[0m'
else
    C_STEP=''; C_OK=''; C_WARN=''; C_OFF=''
fi
step() { printf '%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
ok()   { printf '%s    %s%s\n' "$C_OK"   "$1" "$C_OFF"; }
warn() { printf '%s    %s%s\n' "$C_WARN" "$1" "$C_OFF"; }
die()  { printf 'error: %s\n' "$1" >&2; exit 1; }

# ── Uninstall ────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
    step "Stopping and disabling units"
    for unit in roomsync.service roomsync-openrgb.service; do
        if systemctl --user list-unit-files "$unit" >/dev/null 2>&1; then
            # `stop` on roomsync.service sends SIGINT (see KillSignal in the
            # unit), so the engine runs its full teardown and the room goes
            # dark rather than being left lit.
            systemctl --user stop    "$unit" 2>/dev/null || true
            systemctl --user disable "$unit" 2>/dev/null || true
            ok "Disabled ${unit}"
        fi
        rm -f "${UNIT_DIR}/${unit}"
    done
    systemctl --user daemon-reload
    rm -f "${DESKTOP_DIR}/roomsync.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    ok "Removed the desktop entry"
    echo
    warn "Your settings and logs are still in ${DATA_DIR}"
    warn "Delete that directory to remove them too."
    exit 0
fi

# ── Preconditions ────────────────────────────────────────────────────
step "Checking the environment"

command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this script needs systemd."

# A user bus must exist, or `systemctl --user` silently talks to nothing. This
# is the failure people hit over SSH, where there is no session bus unless
# lingering is enabled.
if ! systemctl --user show-environment >/dev/null 2>&1; then
    die "No systemd user session (is DBUS_SESSION_BUS_ADDRESS set?).
       Over SSH, run: loginctl enable-linger $USER"
fi
ok "systemd user session is available"

[ -f "${PROJECT_DIR}/main.py" ] || die "main.py not found in ${PROJECT_DIR}"
ok "Project: ${PROJECT_DIR}"

# ── Python interpreter ───────────────────────────────────────────────
# The project virtualenv wins if it exists: the unit runs with no shell, so
# there is no activated environment and a bare `python3` would be the system
# one, missing every dependency.
step "Locating the Python interpreter"
if [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"
    ok "Using the project virtualenv"
else
    PYTHON_BIN="$(command -v python3 || true)"
    [ -n "$PYTHON_BIN" ] || die "No python3 on PATH and no .venv in the project."
    warn "No .venv found; using ${PYTHON_BIN}"
    warn "Dependencies must be installed system-wide for this to start."
fi
ok "Interpreter: ${PYTHON_BIN}"

# ── Browser ──────────────────────────────────────────────────────────
step "Locating a Chromium-based browser"
BROWSER_BIN=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser \
                 brave-browser microsoft-edge vivaldi-stable; do
    if command -v "$candidate" >/dev/null 2>&1; then
        BROWSER_BIN="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$BROWSER_BIN" ]; then
    # Not fatal: the units are the important half, and the dashboard is
    # reachable in any browser at the URL printed at the end.
    warn "No Chromium-based browser found; skipping the desktop launcher."
    warn "Firefox has no equivalent of --app, so the launcher needs one of"
    warn "chromium, google-chrome, brave or edge."
else
    ok "Browser: ${BROWSER_BIN}"
fi

# ── OpenRGB ──────────────────────────────────────────────────────────
OPENRGB_BIN=""
if [ "$WITH_OPENRGB" -eq 1 ]; then
    step "Locating OpenRGB"
    OPENRGB_BIN="$(command -v openrgb || true)"
    if [ -z "$OPENRGB_BIN" ]; then
        warn "openrgb not found on PATH; skipping its unit."
        warn "RoomSync runs fine without it — the BLE strips are unaffected."
        WITH_OPENRGB=0
    else
        ok "OpenRGB: ${OPENRGB_BIN}"
    fi
fi

# ── URL, read from config.toml ───────────────────────────────────────
# Read rather than hard-coded, so someone who moved the app off 8420 does not
# get a launcher pointing at a dead port. sed over the [web] section: no TOML
# parser is guaranteed present, and this is two integers.
step "Reading the dashboard URL from config.toml"
WEB_HOST=127.0.0.1
WEB_PORT=8420
CONFIG="${PROJECT_DIR}/config.toml"
if [ -f "$CONFIG" ]; then
    section="$(sed -n '/^\[web\]/,/^\[/p' "$CONFIG")"
    h="$(printf '%s' "$section" | sed -n 's/^[[:space:]]*host[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
    p="$(printf '%s' "$section" | sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' | head -1)"
    [ -n "$h" ] && WEB_HOST="$h"
    [ -n "$p" ] && WEB_PORT="$p"
fi
# 0.0.0.0 is a bind address, not somewhere a browser can navigate to.
[ "$WEB_HOST" = "0.0.0.0" ] && WEB_HOST=127.0.0.1
URL="http://${WEB_HOST}:${WEB_PORT}"
ok "URL: ${URL}"

# ── Icons ────────────────────────────────────────────────────────────
step "Ensuring the icon set exists"
ICON_PATH="${PROJECT_DIR}/web/static/icons/icon-256.png"
if [ ! -f "$ICON_PATH" ]; then
    if "$PYTHON_BIN" "${PROJECT_DIR}/scripts/make_icons.py" >/dev/null 2>&1; then
        ok "Generated the icon set"
    else
        warn "Could not generate icons (is Pillow installed?)"
    fi
fi
if [ -f "$ICON_PATH" ]; then
    # Install into the hicolor theme as well as pointing at the file directly.
    # A themed icon survives the project directory being moved and is what
    # every dock actually prefers to look up.
    for size in 48 64 128 192 256 512; do
        src="${PROJECT_DIR}/web/static/icons/icon-${size}.png"
        dest_dir="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/${size}x${size}/apps"
        [ -f "$src" ] || continue
        mkdir -p "$dest_dir"
        cp -f "$src" "${dest_dir}/roomsync.png"
    done
    ICON_NAME="roomsync"          # themed name, resolved by the icon theme
    command -v gtk-update-icon-cache >/dev/null 2>&1 && \
        gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true
    ok "Installed into the hicolor icon theme"
else
    ICON_NAME="$ICON_PATH"        # absolute path fallback
    warn "Falling back to an absolute icon path"
fi

# ── Template and install ─────────────────────────────────────────────
# `|` as the sed delimiter, not `/`: every value substituted here is a path.
render() {
    sed -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
        -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
        -e "s|__OPENRGB_BIN__|${OPENRGB_BIN}|g" \
        -e "s|__BROWSER_BIN__|${BROWSER_BIN}|g" \
        -e "s|__URL__|${URL}|g" \
        -e "s|__PROFILE_DIR__|${PROFILE_DIR}|g" \
        -e "s|__ICON__|${ICON_NAME}|g" \
        "$1" > "$2"
}

step "Installing systemd user units"
mkdir -p "$UNIT_DIR" "$PROFILE_DIR"

if [ "$WITH_OPENRGB" -eq 1 ]; then
    render "${SCRIPT_DIR}/roomsync-openrgb.service" "${UNIT_DIR}/roomsync-openrgb.service"
    ok "${UNIT_DIR}/roomsync-openrgb.service"
fi

render "${SCRIPT_DIR}/roomsync.service" "${UNIT_DIR}/roomsync.service"
ok "${UNIT_DIR}/roomsync.service"

# Without the OpenRGB unit installed, roomsync.service's Wants/After would
# reference a unit that does not exist. Harmless for Wants (systemd logs and
# continues) but it clutters `systemctl --user status`, so strip both lines.
if [ "$WITH_OPENRGB" -eq 0 ]; then
    sed -i -e 's| roomsync-openrgb.service||' \
           -e '/^Wants=roomsync-openrgb.service$/d' "${UNIT_DIR}/roomsync.service"
fi

systemctl --user daemon-reload
ok "Reloaded the systemd user daemon"

step "Enabling units"
[ "$WITH_OPENRGB" -eq 1 ] && systemctl --user enable roomsync-openrgb.service >/dev/null
systemctl --user enable roomsync.service >/dev/null
ok "Enabled (they will start with your graphical session)"

# ── Desktop entry ────────────────────────────────────────────────────
if [ -n "$BROWSER_BIN" ]; then
    step "Installing the desktop launcher"
    mkdir -p "$DESKTOP_DIR"
    render "${SCRIPT_DIR}/roomsync.desktop" "${DESKTOP_DIR}/roomsync.desktop"
    chmod 644 "${DESKTOP_DIR}/roomsync.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    command -v desktop-file-validate >/dev/null 2>&1 && \
        desktop-file-validate "${DESKTOP_DIR}/roomsync.desktop" && ok "Entry validates"
    ok "${DESKTOP_DIR}/roomsync.desktop"
fi

# ── Start ────────────────────────────────────────────────────────────
step "Starting now"
[ "$WITH_OPENRGB" -eq 1 ] && systemctl --user start roomsync-openrgb.service || true
systemctl --user start roomsync.service

# The unit sleeps 20 s before exec'ing python, so it is still in ExecStartPre
# at this point. Report that rather than a status that has not settled.
echo
ok "Started. The engine waits 20s for OpenRGB and Bluetooth before booting."
echo
cat <<EOF
Installed.

  Dashboard:   ${URL}
  Status:      systemctl --user status roomsync.service
  Logs:        journalctl --user -u roomsync.service -f
  App log:     ${DATA_DIR}/logs/roomsync.log
  Restart:     systemctl --user restart roomsync.service
  Stop:        systemctl --user stop roomsync.service
  Remove:      ${SCRIPT_DIR}/install.sh --uninstall

To keep RoomSync running when you are not logged in graphically:

  loginctl enable-linger $USER

If OpenRGB reports no motherboard or RAM devices, it needs SMBus access:

  sudo modprobe i2c-dev && echo i2c-dev | sudo tee /etc/modules-load.d/i2c.conf
  sudo usermod -aG i2c \$USER      # log out and back in
EOF
