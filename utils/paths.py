"""Where RoomSync's files live, decided once and independently of the CWD.

A dev script is always launched from its own directory, so every relative path
in it is silently correct. A background service is not: it is started by the
Task Scheduler, by a shortcut, by a systemd unit, or by a tray app, and the
working directory is then whatever the launcher felt like. `config.toml` read
relatively is the difference between "starts" and "crashes on import" the first
time RoomSync is started by anything other than a terminal sitting in the repo.

Two distinct roots, deliberately:

  APP_DIR   the installation — code and the shipped config.toml. Read-mostly,
            and under a program-files-style install it is not writable at all.
  state/log the user's data — restored state, rotating logs. Always writable,
            per-user, and survives replacing the installation wholesale.

Both are overridable by environment variable, which is what makes a portable
install (everything in one folder next to the .exe) and a test run against a
scratch directory possible without special-casing either in the code.
"""

import os
import sys
from pathlib import Path

APP_NAME = "RoomSync"

# The installation root: the directory holding main.py, i.e. the parent of the
# package this module lives in. Resolved from __file__ rather than from argv or
# the CWD, so it is correct however the process was started. Under PyInstaller
# the modules are unpacked into a temp dir and sys._MEIPASS is the only truth.
if getattr(sys, "frozen", False):
    APP_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    # Files shipped ALONGSIDE the exe rather than baked into it — a config the
    # user is expected to edit is one of those, so it gets its own root.
    BUNDLE_DIR = Path(sys.executable).parent.resolve()
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = APP_DIR


def _user_data_root() -> Path:
    """The per-user writable root, following each platform's own convention.

    No dependency on `platformdirs` for three lines of os.environ lookups that
    have not changed in a decade.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


def data_dir() -> Path:
    """Root of everything the user owns. ROOMSYNC_DATA_DIR overrides it."""
    override = os.environ.get("ROOMSYNC_DATA_DIR")
    root = Path(override).expanduser() if override else _user_data_root()
    return root.resolve()


def _ensure(path: Path) -> Path:
    """Create a directory and return it.

    Failure is deliberately not fatal here — a read-only or unwritable data
    root must degrade to "no persistence, no file log" rather than preventing
    the lights from coming on. The callers (StateStore, the log handler) each
    handle an unwritable path on their own.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def state_dir() -> Path:
    """Where the restored UI/engine state lives."""
    return _ensure(data_dir() / "state")


def log_dir() -> Path:
    """Where the rotating log files live."""
    return _ensure(data_dir() / "logs")


def config_path() -> Path:
    """The absolute path to config.toml.

    Search order, first hit wins:

      1. $ROOMSYNC_CONFIG            explicit, for tests and odd installs
      2. <data_dir>/config.toml      the user's own copy, survives upgrades
      3. <bundle>/config.toml        next to the .exe, for a portable install
      4. <APP_DIR>/config.toml       the shipped default — the repo checkout

    The last one is what has always been read; it is now reached by an absolute
    path, so the CWD stops mattering. The user copy sits ahead of it so editing
    settings does not mean editing a file inside the installation.
    """
    override = os.environ.get("ROOMSYNC_CONFIG")
    if override:
        return Path(override).expanduser().resolve()

    for candidate in (data_dir() / "config.toml",
                      BUNDLE_DIR / "config.toml",
                      APP_DIR / "config.toml"):
        if candidate.is_file():
            return candidate.resolve()

    # Nothing exists yet. Name the shipped location so the error a caller
    # raises points at the file they are expected to create.
    return (APP_DIR / "config.toml").resolve()


def state_file() -> Path:
    return state_dir() / "state.json"


def log_file() -> Path:
    return log_dir() / "roomsync.log"
