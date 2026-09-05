"""RoomSync entry point.

The dev-script shape (uvicorn --reload, logs to whatever terminal happened to
launch it, one process per invocation) is not what a daily driver looks like.
Three changes here make it a service:

  no reload   the reloader forks a second process, and every stateful thing in
              this app is a hardware handle — a BLE transport, an audio device,
              an OpenRGB socket. Two processes fighting over those is not a
              development convenience, it is a disconnect loop.
  file logs   a background service has no console. Logs go to a rotating file
              under log_dir(), and to the console as well when there is one.
  one instance a second copy silently steals nothing and achieves nothing: it
              cannot bind the port, and it would fight the first for the radio.
              Detected by the port bind, which is the only lock that is
              genuinely released when the process dies, however it dies.
"""

import logging
import socket
import sys
from logging.handlers import RotatingFileHandler

import structlog
import uvicorn

from config import settings
from utils.helpers import enable_high_resolution_timer
from utils.paths import config_path, data_dir, log_file
from web.app import create_app

logger = structlog.get_logger()

# Must happen at import time, before uvicorn builds the event loop: on Windows
# the default ~15.6 ms timer granularity rounds every asyncio.sleep() up, which
# caps the engine at ~21 Hz regardless of tick_hz.
if enable_high_resolution_timer():
    logger.info("Raised system timer resolution to 1ms for accurate tick pacing")

# Keep ~5 runs' worth of history. The engine logs per-event, not per-tick, so
# 2 MB is a long time; five files is enough to still see the boot before the
# one where something broke.
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def configure_logging() -> None:
    """Send structlog through stdlib logging, to a rotating file and the console.

    structlog was already in use but was writing straight to stdout, which for a
    service means straight to nowhere. Routing it through the stdlib gives the
    rotation, and keeps uvicorn's own loggers — which are stdlib — in the same
    file rather than in a second, differently-formatted stream.
    """
    level = getattr(logging, str(settings.general.log_level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    # Idempotent: uvicorn may import this module more than once in some launch
    # shapes, and duplicated handlers mean every line logged twice.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    plain = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")

    try:
        file_handler = RotatingFileHandler(
            log_file(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(plain)
        root.addHandler(file_handler)
    except OSError as exc:
        # An unwritable log directory must not stop the lights coming on.
        print("Could not open the log file (%s); logging to console only" % exc,
              file=sys.stderr)

    # Only when there is actually a console. Under pythonw.exe, a scheduled
    # task, or a tray wrapper, sys.stderr can be None and a StreamHandler on it
    # raises on the first record.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(plain)
        root.addHandler(console)

    # No TimeStamper and no add_log_level here: the stdlib formatter above
    # already prefixes both, and running them in each layer renders every line
    # with two timestamps and two level words.
    structlog.configure(
        processors=[
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def acquire_single_instance(host: str, port: int) -> socket.socket:
    """Bind the web port up front, and hand the bound socket to uvicorn.

    This IS the single-instance guard, and doing it by binding rather than with
    a PID file is deliberate: a lock file outlives a process that was killed,
    crashed, or lost power, and then the app refuses to start until someone
    deletes a file they do not know about. A port is released by the kernel no
    matter how the process ended.

    The socket is not merely tested and closed — it is passed to uvicorn, which
    closes the race between the check and the real bind.

    Exits with status 1 rather than raising: a second launch is a normal thing
    for a user to do (clicking the tray shortcut twice), not a crash.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # NOT SO_REUSEADDR on Windows: there it behaves like SO_REUSEPORT and lets
    # a second instance steal the port from a running first one, which is the
    # exact failure this function exists to prevent.
    if not sys.platform.startswith("win"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
    except OSError:
        sock.close()
        logger.error(
            "RoomSync is already running (port %d is in use). "
            "Open http://%s:%d instead of starting a second copy."
            % (port, host if host not in ("0.0.0.0", "") else "127.0.0.1", port)
        )
        sys.exit(1)
    return sock


app = create_app()

if __name__ == "__main__":
    configure_logging()
    logger.info("RoomSync starting",
                config=str(config_path()), data=str(data_dir()),
                log=str(log_file()))

    sock = acquire_single_instance(settings.web.host, settings.web.port)

    # No reloader, and the app OBJECT rather than an "main:app" import string.
    # With the reloader gone there is no worker process to re-import for, and
    # passing the live object keeps every hardware handle in one process.
    #
    # Server(...).run(sockets=[...]) rather than uvicorn.run(): it is the only
    # entry point that accepts an already-bound socket, which is what makes the
    # single-instance guard race-free.
    #
    # log_config=None leaves configure_logging()'s handlers in place — uvicorn
    # otherwise installs its own dictConfig and the file handler disappears.
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))

    # The tray's Quit needs a way to stop the server, and the lifespan has no
    # other route to it — it is handed the app, not the process. Setting
    # `server.should_exit` is uvicorn's own supported stop signal and it
    # unwinds the lifespan properly, which is what makes Quit and Ctrl+C take
    # exactly the same shutdown path.
    app.state.server = server

    server.run(sockets=[sock])
