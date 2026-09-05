import asyncio
import structlog
from typing import List, Dict

from config import settings, StripConfig
from .worker import BLEStripWorker, TRANSMIT_INTERVAL

logger = structlog.get_logger()

# How often the manager sweeps every strip. The workers already self-heal at
# 1 Hz; this is the outer net that catches a worker whose own loops died.
SUPERVISOR_INTERVAL = 2.0

# Warn when a strip's average GATT write takes longer than this fraction of the
# transmit interval. Past that point the link — not the engine — is setting the
# frame rate, frames are being coalesced, and the strip is running behind the
# PC lights. This is the early warning for the stale-colour class of bug.
LATENCY_WARN_RATIO = 0.8

# Spacing between one strip's first connect attempt and the next one's, from
# [ble].stagger_delay_ms. Three simultaneous scan+connect handshakes overwhelm
# the Windows Bluetooth stack: attempts hang or fail outright, and the retry
# backoff then lines the next round up on the same boundary again.
CONNECT_STAGGER_S = settings.ble.stagger_delay_ms / 1000.0


class BLEManager:
    def __init__(self):
        self._workers: Dict[str, BLEStripWorker] = {}
        self._supervisor_task: asyncio.Task | None = None

        # The adapter is a single shared resource. Every worker's scan+connect
        # runs under this, so only one handshake is ever in flight no matter
        # who asked for it or when — including the case a fixed stagger cannot
        # cover, where an attempt outlives its slot (a powered-off strip burns
        # the full 10 s scan timeout) and the "staggered" attempts overlap
        # again on retry.
        #
        # It is a gate, not a queue: when only one strip wants the radio the
        # acquire is free, so an isolated mid-session drop still reconnects
        # immediately.
        self._connect_gate = asyncio.Semaphore(1)

    @classmethod
    async def create(cls, configs: List[StripConfig]) -> 'BLEManager':
        """
        Creates workers for all strips. The workers handle their own
        connections independently in the background, allowing the app
        to start up instantly even if strips are offline.
        """
        mgr = cls()

        count = len(configs)
        for i, cfg in enumerate(configs):
            # Evenly spread each strip's transmit phase across the interval.
            # Three strips firing on the same 100 ms boundary contend for the
            # same radio slots, which inflates every write's latency; offsetting
            # them keeps each strip's write in its own slice of airtime.
            phase_offset = (TRANSMIT_INTERVAL * i / count) if count else 0.0

            # Boot stagger: strip 0 goes for the radio immediately, each
            # subsequent strip waits its turn. The wait is enforced INSIDE the
            # worker, not by sleeping here — create() is awaited during app
            # startup, and blocking it would trade a Bluetooth problem for a
            # multi-second delay before the UI and engine come up.
            connect_delay = CONNECT_STAGGER_S * i

            logger.info(
                "Initializing strip worker...",
                name=cfg.name,
                mac=cfg.mac,
                connect_delay=round(connect_delay, 2),
            )

            # The worker now boots without needing a client.
            worker = BLEStripWorker(
                config=cfg,
                phase_offset=phase_offset,
                connect_gate=mgr._connect_gate,
            )
            # Launch the independent transmit and background reconnect loops
            worker.start(connect_delay=connect_delay)
            mgr._workers[cfg.name] = worker

        logger.info(
            "Strip workers launched",
            strips=count,
            connect_stagger_s=CONNECT_STAGGER_S,
            boot_window_s=round(CONNECT_STAGGER_S * max(0, count - 1), 2),
        )

        mgr._supervisor_task = asyncio.create_task(mgr._supervisor_loop())
        return mgr

    def set_color(self, strip_name: str, r: int, g: int, b: int):
        """
        Overwrites the target color on a worker. This is SYNCHRONOUS.

        It is a plain attribute assignment on purpose: no queue, no task, no
        await. The engine hands off the newest colour and returns immediately;
        the worker's own loop decides what actually reaches the radio.
        """
        worker = self._workers.get(strip_name)
        if worker:
            worker.set_target_color(r, g, b)

    async def set_power(self, strip_name: str, on: bool):
        """Turns a specific strip on or off."""
        worker = self._workers.get(strip_name)
        if worker:
            await worker.set_power(on)

    # ── Status ─────────────────────────────────────────────────────────────────

    def status(self) -> List[dict]:
        """Per-strip connection state, for the UI and the supervisor."""
        return [w.status() for w in self._workers.values()]

    @property
    def all_connected(self) -> bool:
        return all(w.is_connected for w in self._workers.values())

    # ── Supervisor ─────────────────────────────────────────────────────────────

    async def _supervisor_loop(self):
        """
        Outer safety net. Every strip is checked continuously; any strip seen
        not connected has its recovery re-kicked, and a worker whose watchdog
        itself died is restarted. The workers are individually self-healing,
        so this normally does nothing but log — that is the point.

        It also watches write latency, which is the one failure mode the
        connection checks cannot see: a strip that is connected, writing, and
        steadily falling behind real time.
        """
        logger.info("BLE supervisor started", strips=len(self._workers))

        was_all_connected: bool | None = None
        latency_warned: set[str] = set()

        while True:
            try:
                down = []
                waiting = []
                needs_kick = []
                for name, worker in self._workers.items():
                    # Resurrect a worker whose own watchdog stopped running.
                    # No connect delay: this is a single sick strip, not a mass
                    # event, so it should recover as fast as the radio allows.
                    wd = worker._watchdog_task
                    if wd is None or wd.done():
                        logger.error("Worker watchdog died — restarting worker", name=name)
                        worker.start()

                    if not worker.is_connected:
                        # A strip still inside its boot stagger is doing exactly
                        # what it was told to. Counting it as "down" would fire
                        # a warning every sweep for the whole startup window.
                        if worker.awaiting_stagger:
                            waiting.append(name)
                        else:
                            down.append(name)
                            needs_kick.append(worker)
                        latency_warned.discard(name)
                        continue

                    # Connected but slow: the transport is now the bottleneck.
                    avg = worker._write_latency_ewma
                    interval = worker.transmit_interval
                    if avg > interval * LATENCY_WARN_RATIO:
                        if name not in latency_warned:
                            latency_warned.add(name)
                            logger.warning(
                                "Strip write latency is at or above the transmit "
                                "interval — frames are being dropped to stay live",
                                name=name,
                                avg_write_ms=round(avg * 1000.0, 1),
                                interval_ms=round(interval * 1000.0, 1),
                                acknowledged=worker._write_with_response,
                                dropped_frames=worker._dropped_frames,
                            )
                    else:
                        latency_warned.discard(name)

                # Re-kick recovery. Idempotent and single-flight per worker: a
                # strip already retrying keeps its own schedule and is not
                # pushed further out by this call, so sweeping every 2 s can
                # never starve it.
                #
                # In practice a mass event (adapter reset, power cut, the
                # strips' own PSU) is already being serialised by the connect
                # gate long before this runs — each worker's watchdog kicks its
                # own recovery within a second of the drop. The spacing here
                # only bites on strips whose recovery has NOT started, chiefly
                # after a worker was rebuilt above. A lone strip is index 0 and
                # is therefore never delayed either way.
                for i, worker in enumerate(needs_kick):
                    worker.request_reconnect(delay=CONNECT_STAGGER_S * i)

                if waiting:
                    logger.info("Strips awaiting staggered connect", strips=waiting)
                if down:
                    logger.warning("Strips not connected — reconnecting", strips=down)

                now_all = not down and not waiting
                if was_all_connected is False and now_all:
                    logger.info("All strips connected")
                was_all_connected = now_all

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("BLE supervisor iteration failed", error=str(e))

            await asyncio.sleep(SUPERVISOR_INTERVAL)

    async def disconnect_all(self):
        """Safely disconnects all managed strips and stops their transmit loops."""
        logger.info("Disconnecting all strips...")

        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass

        disconnect_tasks = [worker.disconnect() for worker in self._workers.values()]
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
