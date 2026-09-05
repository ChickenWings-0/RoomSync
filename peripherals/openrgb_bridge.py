import asyncio
import time
import structlog
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor
from config import settings

logger = structlog.get_logger()

# The PC lights are a local TCP socket, not a 2.4 GHz radio: they run at the
# engine's full native tick rate. This is deliberately NOT the BLE strips'
# 10 Hz ceiling — the two paths are independent and must stay that way.
PUSH_HZ = settings.general.tick_hz

# Periodic refresh so another OpenRGB client stealing the devices does not
# leave us silently painting nothing (we only write on change otherwise).
KEEPALIVE_INTERVAL = 2.0

# Reconnect backoff after the OpenRGB server goes away.
RECONNECT_DELAYS = (1.0, 2.0, 5.0)
RECONNECT_DELAY_MAX = 10.0

# A push slower than this fraction of the interval means the SDK, not us, is
# setting the frame rate. Logged once per episode rather than every frame.
LATENCY_WARN_RATIO = 0.8

# Ceiling on the graceful client disconnect during shutdown. See stop().
DISCONNECT_TIMEOUT = 2.0


class OpenRGBBridge:
    """Owns its own push loop so a slow OpenRGB write can never stall the engine.

    The engine hands off a colour synchronously (`set_target_color`) and returns
    immediately; this loop decides what actually reaches the socket. Same
    latest-wins, zero-queue contract as the BLE workers — the difference is
    purely the cadence, because a loopback socket has none of the radio's
    constraints.
    """

    def __init__(self):
        self._client: Optional[OpenRGBClient] = None
        # Dedicated single worker: OpenRGB writes are strictly serialised and
        # never compete with anything else on the default executor.
        self._thread_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="openrgb"
        )

        self._target_color: Tuple[int, int, int] = (0, 0, 0)
        self._last_written_color: Optional[Tuple[int, int, int]] = None
        self._last_write_ts = 0.0
        self._stopped = False

        # Diagnostics
        self._connected = False
        self._last_latency = 0.0
        self._latency_ewma = 0.0
        self._pushes = 0
        self._last_error: Optional[str] = None
        self._latency_warned = False

    # ── Hot path ───────────────────────────────────────────────────────────────

    def set_target_color(self, rgb: Tuple[int, int, int]):
        """Called by the engine every tick. Plain assignment — never blocks.

        No queue: a colour the push loop did not get to is overwritten, not
        stored, so the PC lights always show the newest frame rather than
        replaying old ones.
        """
        self._target_color = rgb

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "push_hz": PUSH_HZ,
            "pushes": self._pushes,
            "latency_ms": round(self._last_latency * 1000.0, 2),
            "latency_avg_ms": round(self._latency_ewma * 1000.0, 2),
            "last_error": self._last_error,
        }

    # ── Connection ─────────────────────────────────────────────────────────────

    async def _connect(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            self._client = await loop.run_in_executor(
                self._thread_pool,
                lambda: OpenRGBClient(
                    settings.openrgb.host, settings.openrgb.port, "RoomSync"
                ),
            )
            self._connected = True
            self._last_error = None
            # Force a repaint: we know nothing about the device state now.
            self._last_written_color = None
            logger.info(
                "Connected to OpenRGB server",
                devices=len(self._client.devices),
                push_hz=PUSH_HZ,
            )
            return True
        except Exception as e:
            self._client = None
            self._connected = False
            self._last_error = str(e)
            return False

    def _write_blocking(self, rgb: Tuple[int, int, int]):
        """Runs on the dedicated executor thread. Raises so the loop can react."""
        self._client.set_color(RGBColor(*rgb))

    # ── Push loop ──────────────────────────────────────────────────────────────

    async def run(self):
        if not settings.openrgb.enabled:
            logger.info("OpenRGB integration is disabled in config.")
            return

        interval = 1.0 / PUSH_HZ
        loop = asyncio.get_running_loop()
        attempt = 0
        next_push = time.monotonic()

        logger.info("OpenRGB bridge starting", push_hz=PUSH_HZ)

        while not self._stopped:
            # ── Reconnect until we have a client ──
            if self._client is None:
                if not await self._connect():
                    delay = (
                        RECONNECT_DELAYS[attempt]
                        if attempt < len(RECONNECT_DELAYS)
                        else RECONNECT_DELAY_MAX
                    )
                    attempt += 1
                    logger.warning(
                        "OpenRGB connect failed — retrying",
                        error=self._last_error,
                        retry_in=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                attempt = 0

            cycle_start = time.monotonic()

            # Freshest colour, sampled immediately before the write.
            target = self._target_color
            stale = (cycle_start - self._last_write_ts) >= KEEPALIVE_INTERVAL

            if target != self._last_written_color or stale:
                started = time.monotonic()
                try:
                    await loop.run_in_executor(
                        self._thread_pool, self._write_blocking, target
                    )
                except Exception as e:
                    # Server went away or a device stalled: drop the client and
                    # let the reconnect path above take over. Logged once, not
                    # once per frame.
                    logger.error("OpenRGB push failed — reconnecting", error=str(e))
                    self._client = None
                    self._connected = False
                    self._last_error = str(e)
                    self._last_written_color = None
                    continue

                latency = time.monotonic() - started
                self._last_latency = latency
                self._latency_ewma = (
                    latency
                    if self._latency_ewma == 0.0
                    else self._latency_ewma * 0.9 + latency * 0.1
                )
                self._last_written_color = target
                self._last_write_ts = time.monotonic()
                self._pushes += 1

                if self._latency_ewma > interval * LATENCY_WARN_RATIO:
                    if not self._latency_warned:
                        self._latency_warned = True
                        logger.warning(
                            "OpenRGB writes are slower than the push interval — "
                            "PC lights are running below the engine tick rate",
                            avg_ms=round(self._latency_ewma * 1000.0, 2),
                            interval_ms=round(interval * 1000.0, 2),
                        )
                elif self._latency_warned:
                    self._latency_warned = False

            # Absolute deadline so timer-granularity overshoot does not compound
            # (see EffectEngine.run). A write slower than the interval yields
            # zero delay and we go straight back for the newest colour rather
            # than accumulating a backlog.
            next_push += interval
            delay = next_push - time.monotonic()
            if delay < -interval:
                next_push = time.monotonic() + interval
                delay = interval
            await asyncio.sleep(max(0.0, delay))

        logger.info("OpenRGB bridge stopped")

    # ── Direct push (out-of-band, not the hot path) ────────────────────────────

    async def set_unified_color(self, rgb: Tuple[int, int, int]):
        """Immediate one-shot push, bypassing the loop.

        Kept for out-of-band callers (shutdown blackout, manual override). The
        engine must NOT use this on the tick path — awaiting a socket write
        there stalls the tick loop and starves the BLE target updates with it.
        """
        if not self._client:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._thread_pool, self._write_blocking, rgb)
            self._last_written_color = rgb
            self._last_write_ts = time.monotonic()
        except Exception as e:
            logger.error("Failed to push color to OpenRGB", error=str(e))
            self._client = None
            self._connected = False

    async def stop(self):
        """Stop the push loop, disconnect the client, release the thread.

        The disconnect is the new part and it is bounded. `OpenRGBClient`
        speaks a blocking TCP protocol on the executor thread: if the OpenRGB
        server has already gone away, or is mid-rescan and not reading its
        socket, the close can block for the OS-level socket timeout — tens of
        seconds — and every one of those is a second the tray's Quit appears to
        have done nothing.

        So: ask politely, wait DISCONNECT_TIMEOUT, then stop caring. The
        process is exiting; the server sees a dropped connection, which is a
        case it already handles because that is what a crash looks like.
        """
        self._stopped = True

        client, self._client = self._client, None
        if client is not None:
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(self._thread_pool, client.disconnect),
                    timeout=DISCONNECT_TIMEOUT,
                )
                logger.info("Disconnected from OpenRGB server")
            except asyncio.TimeoutError:
                logger.warning("OpenRGB disconnect timed out — abandoning the socket",
                               timeout_s=DISCONNECT_TIMEOUT)
            except Exception as e:
                # A server that already vanished raises here, which is fine:
                # there is nothing left to close politely.
                logger.warning("OpenRGB disconnect failed", error=str(e))

        self._connected = False
        # wait=False on purpose. A thread still stuck inside an abandoned
        # socket call must not hold up the shutdown; it is a daemon-equivalent
        # by the time we get here and the interpreter will not wait for it.
        self._thread_pool.shutdown(wait=False)
