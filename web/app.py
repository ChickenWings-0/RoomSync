import asyncio
import os
import janus
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import structlog

from config import settings
from ble.manager import BLEManager
from samplers.screen import ScreenSampler
from samplers.audio import AudioAnalyser
from peripherals.openrgb_bridge import OpenRGBBridge
from effects.engine import EffectEngine
from effects import registry
from persistence import StateStore, StatePersister
from samplers import backends
from tray import RoomSyncTray
from web.routes import router as api_router
from web.ws import ws_router

logger = structlog.get_logger()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──
    logger.info("Starting RoomSync lifespan lifecycle...")

    # 0. What can this machine actually capture?
    # Probed FIRST, before the samplers are built and long before the browser
    # can ask for the mode catalog. A capability that is discovered late is a
    # UI that offers a mode for a second and then takes it away; a capability
    # that is never discovered is the old behaviour, where selecting Audio
    # Reactive on a machine with no loopback device silently rendered nothing.
    caps = backends.probe_all()
    registry.set_capabilities(caps)

    # 1. BLE Subsystem
    ble_mgr = await BLEManager.create(settings.ble.strips)

    # 2. Input Samplers
    screen = ScreenSampler()

    # Audio queue bridges native thread and ASGI loop
    audio_queue = janus.Queue(maxsize=2)
    audio = AudioAnalyser(audio_queue)

    # 3. Core Engine
    fx_engine = EffectEngine(ble_mgr, screen, audio_queue.async_q)

    # 4. Peripheral Bridge
    rgb_bridge = OpenRGBBridge()
    fx_engine.rgb_bridge = rgb_bridge

    # 5. Restore the last session — BEFORE the first tick.
    # Order matters and is the whole point of doing it here rather than in a
    # startup task: the engine renders a frame the moment run() is scheduled,
    # and restoring afterwards means the room visibly flashes through the
    # config default (usually a warm white STATIC) before snapping to whatever
    # the user actually left it on. Injecting into fx_engine.state directly is
    # safe only because nothing is reading it yet; once the loop is running,
    # the command bus is the only legal way in.
    store = StateStore()
    restored = store.load()
    if restored:
        applied = fx_engine.state.apply_dict(restored)
        logger.info("Restored previous session state", fields=len(applied))
    else:
        logger.info("No stored state; starting from config defaults")

    # The debounced writer. It reaches into the engine through three tiny
    # closures rather than being handed the engine, so persistence stays a
    # thing done TO the engine rather than a responsibility of it.
    persister = StatePersister(
        store,
        is_dirty=lambda: fx_engine._state_dirty,
        clear_dirty=lambda: setattr(fx_engine, "_state_dirty", False),
        snapshot=fx_engine.state.to_dict,
    )

    # Expose state to HTTP routes
    app.state.engine = fx_engine
    app.state.ble_mgr = ble_mgr
    app.state.persister = persister

    # Launch async tasks. The engine's task is held by name as well as in the
    # set, because the tray's Restart Engine has to be able to replace exactly
    # that one without touching the samplers or the bridge.
    task_set = set()
    engine_task = asyncio.create_task(fx_engine.run())
    for t in [engine_task, asyncio.create_task(screen.run()),
              asyncio.create_task(rgb_bridge.run()),
              asyncio.create_task(persister.run())]:
        task_set.add(t)
        t.add_done_callback(task_set.discard)

    # Start audio thread
    audio.start()

    # The strips do not know about power_on, and a restored "off" has to reach
    # them as an actual power command or the room comes up lit. Done after the
    # workers exist and once only; from here on the REST route owns it.
    if not fx_engine.state.power_on:
        for strip in ble_mgr._workers:
            await ble_mgr.set_power(strip, False)

    # ── Engine restart, for the tray's "Restart Engine" ──
    async def restart_engine():
        """Replace the tick-loop task in place, keeping every connection.

        Cancels the current engine task and starts a fresh one against the
        SAME EffectEngine — so the modes, the colours and the BLE and OpenRGB
        connections all survive. What it clears is a loop that has wedged or
        died on an unhandled exception, which restarting the process would also
        fix at the cost of three BLE reconnects taking ten seconds.
        """
        nonlocal engine_task
        if engine_task is not None and not engine_task.done():
            engine_task.cancel()
            try:
                await engine_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Old engine task raised on cancel", error=str(exc))
        task_set.discard(engine_task)

        # The per-target filter and DSP state belong to the loop that died;
        # starting fresh means the first frame snaps rather than fading up out
        # of state that is now arbitrarily old.
        fx_engine._room_hsv.clear()

        engine_task = asyncio.create_task(fx_engine.run())
        task_set.add(engine_task)
        engine_task.add_done_callback(task_set.discard)
        logger.info("Engine tick loop restarted")

    def request_shutdown():
        """Ask uvicorn to exit. Set by main.py; absent when embedded in tests."""
        server = getattr(app.state, "server", None)
        if server is not None:
            server.should_exit = True
        else:
            logger.warning("No server handle — cannot stop from the tray")

    # ── Tray ──
    tray = None
    if settings.tray.enabled:
        tray = RoomSyncTray(
            loop=asyncio.get_running_loop(),
            engine=fx_engine,
            persister=persister,
            url="http://%s:%d" % (
                settings.web.host if settings.web.host not in ("0.0.0.0", "")
                else "127.0.0.1",
                settings.web.port,
            ),
            restart_engine=restart_engine,
            request_shutdown=request_shutdown,
        )
        if not tray.start():
            tray = None
    app.state.tray = tray

    logger.info("RoomSync fully operational",
                audio=caps["audio"].available, screen=caps["screen"].available,
                tray=tray is not None)
    yield

    # ── SHUTDOWN ──
    logger.info("Initiating graceful shutdown...")

    # The order below is the whole of the shutdown design, and every step is
    # placed where it is because the step before it makes it possible.

    # 1. Stop everything that PAINTS. The tick loop, the screen sampler and the
    #    persister all go here; the OpenRGB bridge deliberately does NOT, because
    #    step 3 still needs its socket. Nothing may repaint after this point or
    #    the blackout below is immediately undone by the next frame.
    if tray is not None:
        tray.stop()
    for t in task_set:
        t.cancel()
    await asyncio.gather(*task_set, return_exceptions=True)

    # 2. Persist. Before the blackout, because blackout() sets power_on = False
    #    and writing that would mean every restart came up dark — the room was
    #    turned off by the shutdown, not by the user, and the difference is the
    #    entire point of restoring state at all.
    #
    #    StatePersister.run() already flushes on its own CancelledError above;
    #    this is the belt to that pair of braces, for the cases where the task
    #    was never running (a crash during startup) or the cancel landed between
    #    polls. force=True so a clean exit always leaves a file behind.
    persister.flush(force=True)

    # 3. Black the outputs out, and WAIT for it to land. The strips take a
    #    colour by assignment and transmit it from their own loop, so exiting
    #    straight after writing black leaves the room lit on the last colour
    #    that was actually sent. Bounded internally — a wedged strip cannot
    #    hold the process open.
    await fx_engine.blackout()

    # 4. Now the bridge's socket is finished with: disconnect it, bounded.
    await rgb_bridge.stop()

    # 5. The audio thread. A daemon thread would be terminated at interpreter
    #    exit wherever it happened to be, which is normally inside a blocking
    #    read holding an open WASAPI handle. Asked to stop properly instead,
    #    with a timeout so a wedged device cannot hold Quit open.
    audio.stop()

    # 6. Finally the radio, once nothing can possibly write to it again.
    await ble_mgr.disconnect_all()
    logger.info("Shutdown complete")

def _asset_version(static_dir: str) -> str:
    """A cache key that changes exactly when the assets do.

    Newest mtime across the versioned files, so editing either one mints a new
    URL and the browser is obliged to fetch it. Unchanged files keep their URL
    and stay cached, which is the whole point of not simply disabling caching.
    """
    newest = 0.0
    for name in ("app.js", "style.css"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(static_dir, name)))
        except OSError:
            pass
    return str(int(newest))


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, title="RoomSync")

    # REST and WebSocket endpoints
    app.include_router(api_router)
    app.include_router(ws_router)

    # Mount Static UI
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    # Create static dir if it doesn't exist to prevent crash on startup
    os.makedirs(static_dir, exist_ok=True)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def root():
        """Serve the shell, and make the browser re-check the assets.

        The dashboard is a single cached page whose markup and script must
        agree: app.js builds the mode banks by id, so a browser holding an
        older app.js against newer index.html throws on the first missing
        element and renders empty control groups with no clue why. Chrome will
        happily serve app.js from its memory cache for the session without ever
        revalidating, so this is not hypothetical — it is what a shipped UI
        change looks like to anyone who had the page open beforehand.

        no-store on the shell alone is enough: the asset URLs below carry a
        mtime stamp, so a changed file is a new URL and an unchanged one still
        hits the cache normally.
        """
        index = os.path.join(static_dir, "index.html")
        with open(index, encoding="utf-8") as fh:
            html = fh.read()
        return HTMLResponse(
            html.replace("__ASSETV__", _asset_version(static_dir)),
            headers={"Cache-Control": "no-store"},
        )

    return app
