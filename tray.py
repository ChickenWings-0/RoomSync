"""System tray icon: the only UI a background service actually needs.

Once RoomSync stops being a terminal window, there is no longer anything to
Ctrl+C and nothing to tell you it is running. This is that something. It is a
deliberately small surface — open the dashboard, kill the lights, restart the
engine, quit — because everything else already has a better home in the web UI.

Threading is the whole difficulty here and it is worth being explicit about.
pystray's `run()` is a blocking native event loop (a Win32 message pump on
Windows) and it must own the thread it runs on. The asyncio loop must equally
own its own. So the icon runs on a daemon thread, and EVERY action it triggers
is a coroutine submitted back to the event loop with
`run_coroutine_threadsafe` — never a direct call into the engine.

That is not defensive style, it is a correctness requirement: the engine's
state is only safe to mutate from the tick loop's thread, and the command bus
is an `asyncio.Queue`, which is explicitly not thread-safe. A menu handler that
touched `engine.state` directly would be a data race that shows up as a
corrupted frame once a week.
"""

import asyncio
import threading
import webbrowser
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

# How long a menu action may take before the tray gives up waiting on it. The
# menu handler runs on pystray's thread, and blocking it freezes the icon —
# including its right-click menu — so no action is allowed to wait forever on
# an event loop that might be busy or already stopping.
ACTION_TIMEOUT_S = 5.0

# Quit waits longer: it is doing real work (flush the state, black the room out)
# and the user is already watching the lights rather than the icon.
QUIT_TIMEOUT_S = 8.0

ICON_SIZE = 64


def _build_image(on: bool = True):
    """The icon itself, drawn rather than shipped as a file.

    A generated image means no asset path to resolve — which matters more than
    it sounds, because the tray is exactly the launch shape (a shortcut, the
    Task Scheduler) where a relative asset path is least likely to be correct.
    Pillow is already a hard dependency of pystray, so this costs nothing.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # A filled ring: lit amber when the lights are on, dim grey when they are
    # not, so the tray answers "is it running and are the lights on?" at a
    # glance without opening anything.
    fill = (255, 147, 41, 255) if on else (70, 70, 78, 255)
    d.ellipse((6, 6, ICON_SIZE - 6, ICON_SIZE - 6), fill=fill)
    d.ellipse((18, 18, ICON_SIZE - 18, ICON_SIZE - 18), fill=(16, 16, 20, 255))
    return img


class RoomSyncTray:
    """The tray icon and its menu.

    Owns no application state. It is given the event loop and the handful of
    objects its four menu items act on, and every action becomes a coroutine
    submitted to that loop.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        engine,
        persister,
        url: str,
        restart_engine: Callable[[], "asyncio.Future"],
        request_shutdown: Callable[[], None],
    ):
        self._loop = loop
        self._engine = engine
        self._persister = persister
        self._url = url
        self._restart_engine = restart_engine
        self._request_shutdown = request_shutdown

        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self._quitting = threading.Event()

    # ── Thread plumbing ────────────────────────────────────────────

    def _submit(self, coro, timeout: float = ACTION_TIMEOUT_S):
        """Run a coroutine on the event loop from the tray thread, bounded.

        Returns the result, or None if it failed or timed out. Failure is
        logged and swallowed: an exception raised out of a pystray menu handler
        tears down the icon, and losing the tray because OpenRGB hiccuped would
        be a far worse outcome than a menu click that did nothing.
        """
        if self._loop.is_closed():
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            # The loop stopped between the check and the submit.
            return None
        try:
            return future.result(timeout)
        except TimeoutError:
            logger.warning("Tray action timed out", timeout_s=timeout)
        except Exception as exc:
            logger.error("Tray action failed", error=str(exc))
        return None

    # ── Menu actions ───────────────────────────────────────────────

    def _on_open(self, icon=None, item=None):
        """Open the dashboard in the default browser."""
        try:
            webbrowser.open(self._url)
        except Exception as exc:
            logger.error("Could not open the dashboard", url=self._url, error=str(exc))

    def _on_lights_off(self, icon=None, item=None):
        """Toggle master power, through the command bus like every other change.

        A toggle rather than a one-way "off": the menu item relabels itself
        from the engine's own state, so the tray never claims a power state the
        engine does not have.
        """
        from effects.engine import Command

        target_on = not self._engine.state.power_on

        async def apply():
            await self._engine.command_bus.put(
                Command(type="SET_POWER", payload={"on": target_on})
            )
            # The strips need the actual power command too — the engine's
            # blackout only stops it sending colour. Same two-path shape as
            # the /api/power route, and for the same reason.
            for strip in self._engine.ble_mgr._workers:
                await self._engine.ble_mgr.set_power(strip, target_on)

        self._submit(apply())
        self.refresh()

    def _on_restart_engine(self, icon=None, item=None):
        """Rebuild the tick loop without restarting the process.

        The realistic use for this is a wedged sampler or an engine task that
        died on an unhandled exception: the web UI is still up, the strips are
        still connected, and the only thing that needs replacing is the loop.
        Restarting the whole process would drop three BLE connections that take
        ten seconds to re-establish.
        """
        logger.info("Restarting the engine from the tray")
        self._submit(self._restart_engine())
        self.refresh()

    def _on_quit(self, icon=None, item=None):
        """Flush state, black the outputs out, then stop the server.

        Order matters. The flush goes first because it is fast and it is the
        thing a user would be angriest to lose. The blackout goes second and is
        the visible one — the room must be dark before the process exits, not
        merely told to be.

        Both are done here, synchronously from the tray's point of view, rather
        than being left to the lifespan teardown: the teardown does run and does
        repeat both, but only after uvicorn has finished its own shutdown, and
        the delay between clicking Quit and the room going dark is exactly the
        thing that makes an app feel broken.
        """
        if self._quitting.is_set():
            return   # double-click on Quit; the first one is already running
        self._quitting.set()
        logger.info("Quit requested from the tray")

        async def teardown():
            try:
                self._persister.flush(force=True)
            except Exception as exc:
                logger.error("State flush during quit failed", error=str(exc))
            await self._engine.blackout()

        self._submit(teardown(), timeout=QUIT_TIMEOUT_S)

        # Ask uvicorn to exit. That unwinds the lifespan, which cancels the
        # tasks and runs the full teardown — this function only front-runs the
        # two user-visible parts of it.
        self._request_shutdown()

        if self._icon is not None:
            self._icon.stop()

    # ── Lifecycle ──────────────────────────────────────────────────

    def _menu(self):
        import pystray

        # Callables rather than literals: pystray re-evaluates them each time
        # the menu is opened, so the label tracks the engine instead of showing
        # whatever was true when the icon was built.
        return pystray.Menu(
            pystray.MenuItem("Open Dashboard", self._on_open, default=True),
            pystray.MenuItem(
                lambda item: "Lights On" if not self._engine.state.power_on else "Lights Off",
                self._on_lights_off,
            ),
            pystray.MenuItem("Restart Engine", self._on_restart_engine),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    def refresh(self):
        """Repaint the icon from the current power state."""
        if self._icon is None:
            return
        try:
            self._icon.icon = _build_image(self._engine.state.power_on)
        except Exception:
            pass

    def start(self) -> bool:
        """Build the icon and run it on a daemon thread. False if unavailable.

        pystray needs a system tray to exist, which a headless Linux box or a
        session with no status-notifier host does not have. That is a normal
        configuration — the web UI is fully sufficient on its own — so it is
        reported and shrugged off rather than raised.
        """
        try:
            import pystray
        except ImportError as exc:
            logger.warning("pystray is not installed — no tray icon", error=str(exc))
            return False

        try:
            self._icon = pystray.Icon(
                "roomsync", _build_image(self._engine.state.power_on),
                "RoomSync", self._menu(),
            )
        except Exception as exc:
            logger.warning("Could not create the tray icon", error=str(exc))
            return False

        # Daemon: if the process is exiting for any reason other than the Quit
        # item, the tray must not be what keeps it alive.
        self._thread = threading.Thread(
            target=self._run, name="tray", daemon=True
        )
        self._thread.start()
        logger.info("Tray icon started")
        return True

    def _run(self):
        try:
            self._icon.run()
        except Exception as exc:
            # A tray backend failing at runtime (the shell restarting, the
            # status-notifier host going away) is not fatal to the app.
            logger.warning("Tray icon stopped unexpectedly", error=str(exc))

    def stop(self):
        """Tear the icon down. Called from the lifespan shutdown."""
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
