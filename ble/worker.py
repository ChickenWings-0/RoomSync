import asyncio
import time
import structlog
from typing import Optional
from bleak import BleakClient, BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak import BleakScanner

from config import settings, StripConfig
from .protocol import build_color_payload, build_power_payload

logger = structlog.get_logger()

# Safe hardware ceiling: 10 Hz write rate for MELK-OA10 stability.
# NOTE: this is a CEILING, not a target. When the link is slower than this the
# loop self-limits below it (see _transmit_loop) — it never runs faster.
TRANSMIT_INTERVAL = 0.1

# Ceiling used when the characteristic only supports write-without-response.
# There is no ACK in that mode, so `await write_gatt_char(...)` returns the
# instant the host stack accepts the packet and we are flying blind: nothing
# downstream can tell us we are outrunning the radio. The only safe response to
# having no feedback is to be conservative, so the blind path runs at half rate.
UNACKED_TRANSMIT_INTERVAL = 0.2

# Hard ceiling on a single GATT write. Without this, a wedged write parks the
# transmit loop forever: the task never completes, so the watchdog's done()
# check never fires and the strip goes silently mute until restart.
WRITE_TIMEOUT = 2.0

# If the transmit loop has not completed a cycle in this long it is considered
# wedged, and is cancelled and rebuilt. Must exceed WRITE_TIMEOUT.
TRANSMIT_STALL_TIMEOUT = 5.0

# Liveness probe: if the engine has not changed the colour for this long, we
# re-send the current colour anyway. Without it a strip that dies silently
# while the room is dark (MUSIC mode sits at pure black between kicks) is
# never written to, so the link failure is never discovered.
KEEPALIVE_INTERVAL = 2.0

# Supervisor cadence: how often every strip is checked for liveness.
WATCHDOG_INTERVAL = 1.0

# Consecutive acknowledged-write timeouts tolerated before we conclude the
# firmware does not really honour write-with-response and drop back to
# write-without-response. Cheap strips sometimes advertise "write" and then
# never actually ACK.
ACK_FAILURE_LIMIT = 3

# Reconnect backoff: aggressive at first, capped so a strip that is genuinely
# powered off does not spin the radio forever.
RECONNECT_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0)
RECONNECT_DELAY_MAX = 10.0

SCAN_TIMEOUT = 10.0

# How long the adapter is left alone after a connect attempt before the next
# strip is allowed to touch it. Held INSIDE the connect gate, so it spaces
# consecutive handshakes rather than merely delaying this worker.
CONNECT_SETTLE_S = 0.5


class BLEStripWorker:
    def __init__(
        self,
        config: StripConfig,
        phase_offset: float = 0.0,
        connect_gate: Optional[asyncio.Semaphore] = None,
    ):
        self._client: Optional[BleakClient] = None
        self.config = config

        # Spreads this strip's writes across the transmit interval so three
        # strips do not contend for the same radio slot every 100 ms.
        # NOTE: this is the TRANSMIT stagger, and is unrelated to the CONNECT
        # stagger below — one spaces steady-state writes, the other spaces
        # handshakes.
        self._phase_offset = phase_offset

        # ── Connect stagger ────────────────────────────────────────────────
        # Two mechanisms, because they fail differently.
        #
        # The gate serialises scan+connect across every strip: the Windows BLE
        # stack handles one handshake at a time far better than three, and a
        # fixed delay stops helping the moment an attempt runs long (a dead
        # strip burns the full 10 s SCAN_TIMEOUT, by which point three
        # "staggered" attempts overlap again). Shared by the manager; the
        # default keeps a standalone worker self-consistent.
        #
        # The embargo spaces the initial attempts so the three strips do not
        # all hit the gate in the same millisecond and queue up behind one
        # another. It is an absolute monotonic deadline and therefore one-shot
        # by construction: once it has passed it stays in the past, so a strip
        # dropping mid-session reconnects with exactly zero added delay.
        self._connect_gate = connect_gate or asyncio.Semaphore(1)
        self._connect_not_before = 0.0

        # State-based color target (overwritten by engine at 30Hz, no queue)
        self._target_color = (0, 0, 0)
        self._last_written_color = None
        self._last_write_ts = 0.0

        self._connected = False
        self._intentional_disconnect = False
        self._reconnect_lock = asyncio.Lock()
        self._transmit_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # ── Transport write mode ───────────────────────────────────────────────
        # Resolved from the characteristic's advertised properties on connect.
        # True  = write-with-response: the ATT ACK gates the await, which is the
        #         only real source of backpressure the transport gives us.
        # False = write-without-response: the host stack buffers and the await
        #         returns immediately, so pacing must be enforced entirely here.
        self._write_char: Optional[BleakGATTCharacteristic] = None
        self._write_with_response = False
        self._ack_failures = 0

        # Diagnostics surfaced to the UI
        self._reconnect_attempts = 0
        self._last_connected_ts: Optional[float] = None
        self._last_error: Optional[str] = None

        # Backlog telemetry. If mean write latency creeps above TRANSMIT_INTERVAL
        # the link is the bottleneck and frames are being coalesced — that is the
        # signature of the bug this file was rewritten to kill.
        self._last_write_latency = 0.0
        self._write_latency_ewma = 0.0
        self._dropped_frames = 0
        self._transmit_heartbeat = 0.0

    # ── Liveness ───────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Single source of truth: our flag AND the transport agreeing."""
        return bool(
            self._connected
            and self._client is not None
            and self._client.is_connected
        )

    @property
    def awaiting_stagger(self) -> bool:
        """True while this strip is deliberately holding off the radio.

        Lets the supervisor tell "waiting its turn in the boot sequence" apart
        from "genuinely will not connect", which otherwise look identical.
        """
        return time.monotonic() < self._connect_not_before

    def status(self) -> dict:
        return {
            "name": self.config.name,
            "mac": self.config.mac,
            "connected": self.is_connected,
            "awaiting_stagger": self.awaiting_stagger,
            "reconnect_attempts": self._reconnect_attempts,
            "last_connected_ts": self._last_connected_ts,
            "last_error": self._last_error,
            "acknowledged_writes": self._write_with_response,
            "write_latency_ms": round(self._last_write_latency * 1000.0, 1),
            "write_latency_avg_ms": round(self._write_latency_ewma * 1000.0, 1),
            "dropped_frames": self._dropped_frames,
        }

    def start(self, connect_delay: float = 0.0):
        """Spawns the transmit loop, the watchdog, and the initial connect.

        `connect_delay` holds this strip's FIRST radio contact back by that many
        seconds. Everything else still starts immediately — the loops are pure
        bookkeeping until there is a link, so nothing is gained by delaying
        them and a late watchdog is a blind spot.

        Crucially the delay lives inside the worker rather than in the caller:
        the manager must not block on it (the app is designed to come up
        instantly whether or not the strips are reachable), and the watchdog
        and transmit loop both kick reconnects of their own, so a caller-side
        sleep would be overridden within a second anyway.

        Idempotent: the manager's supervisor calls this to resurrect a worker,
        and it must never end up with two transmit loops writing to one strip.
        """
        self._loop = asyncio.get_running_loop()
        self._transmit_heartbeat = time.monotonic()
        if self._transmit_task is None or self._transmit_task.done():
            self._transmit_task = asyncio.create_task(self._transmit_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        self.request_reconnect(connect_delay)

    def request_reconnect(self, delay: float = 0.0):
        """Ask for recovery, optionally not before `delay` seconds from now.

        The delay is only ever applied to a worker that is not already
        recovering. A reconnect loop that is mid-flight keeps its existing
        deadline, so a caller that re-requests on a timer (the supervisor
        sweeps every 2 s) can never push a strip's next attempt further and
        further out — the classic way a "polite" backoff turns into starvation.
        """
        if self._reconnect_task and not self._reconnect_task.done():
            return
        if delay > 0.0:
            self._connect_not_before = time.monotonic() + delay
        self._trigger_reconnect()

    def set_target_color(self, r: int, g: int, b: int):
        """Called by the engine at 30Hz. Just overwrites the target — zero latency.

        Frames the transmit loop never got to are counted, not stored. This is
        the intended lossy hand-off: the strip always gets the newest colour,
        never a replay of the ones it missed.
        """
        color = (r, g, b)
        if color != self._target_color:
            if self._target_color != self._last_written_color:
                # The previous target was superseded before it ever went on air.
                self._dropped_frames += 1
            self._target_color = color

    def _mark_down(self, reason: str):
        """Flag the link dead and kick recovery. Safe to call repeatedly."""
        if self._intentional_disconnect:
            return
        was_up = self._connected
        self._connected = False
        self._last_written_color = None
        self._write_char = None
        self._last_error = reason
        if was_up:
            logger.warning("Strip went down", name=self.config.name, reason=reason)
        self._trigger_reconnect()

    def _trigger_reconnect(self):
        """Single-flight: never stack duplicate reconnect tasks."""
        if self._intentional_disconnect:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    # ── Watchdog ───────────────────────────────────────────────────────────────

    async def _watchdog_loop(self):
        """
        Continuously verifies this strip. If it is ever seen not connected —
        for any reason, whether or not a write failed or a callback fired —
        recovery is kicked off immediately. Also resurrects the transmit loop
        if it ever died OR wedged, so neither an unexpected exception nor a
        stuck GATT write can permanently mute a strip.
        """
        logger.info("Watchdog started", name=self.config.name)

        while not self._intentional_disconnect:
            try:
                now = time.monotonic()

                # A transmit loop that is alive but stuck is worse than a dead
                # one: done() stays False forever, so the restart check below
                # would never fire. Catch it on the heartbeat instead.
                if (
                    self._transmit_task
                    and not self._transmit_task.done()
                    and (now - self._transmit_heartbeat) > TRANSMIT_STALL_TIMEOUT
                ):
                    logger.error(
                        "Transmit loop wedged — cancelling",
                        name=self.config.name,
                        stalled_for=round(now - self._transmit_heartbeat, 1),
                    )
                    self._transmit_task.cancel()
                    self._transmit_heartbeat = now
                    self._mark_down("transmit loop wedged")

                # The transmit loop must always be alive.
                if self._transmit_task is None or self._transmit_task.done():
                    if self._transmit_task and self._transmit_task.done():
                        exc = (
                            self._transmit_task.exception()
                            if not self._transmit_task.cancelled()
                            else None
                        )
                        if exc:
                            logger.error(
                                "Transmit loop died — restarting",
                                name=self.config.name,
                                error=str(exc),
                            )
                    self._transmit_heartbeat = time.monotonic()
                    self._transmit_task = asyncio.create_task(self._transmit_loop())

                # The flag and the transport must agree. This catches a link
                # that dropped without firing the disconnect callback.
                if not self.is_connected:
                    self._mark_down("watchdog saw strip not connected")

            except Exception as e:
                logger.error("Watchdog iteration failed", name=self.config.name, error=str(e))

            await asyncio.sleep(WATCHDOG_INTERVAL)

        logger.info("Watchdog exited", name=self.config.name)

    # ── Independent Transmit Loop ──────────────────────────────────────────────

    def _resolve_write_mode(self):
        """Pick the write mode from what the characteristic actually supports.

        Write-with-response is strongly preferred: the peripheral's ATT
        acknowledgement is what makes `await write_gatt_char(...)` mean "the
        strip has it" rather than "the OS has queued it". That ACK is the entire
        backpressure mechanism. Without it the host stack accepts an unbounded
        number of packets and drains them at the negotiated connection interval,
        which is exactly how seconds of stale colour accumulate.
        """
        self._write_char = None
        self._write_with_response = False
        self._ack_failures = 0

        try:
            char = self._client.services.get_characteristic(settings.ble.write_uuid)
        except Exception as e:
            logger.warning(
                "Could not resolve write characteristic — falling back to unacknowledged writes",
                name=self.config.name,
                error=str(e),
            )
            return

        if char is None:
            logger.warning(
                "Write characteristic not found — falling back to unacknowledged writes",
                name=self.config.name,
                uuid=settings.ble.write_uuid,
            )
            return

        # Writing through the characteristic object skips a UUID lookup on
        # every single packet.
        self._write_char = char
        props = set(char.properties)
        self._write_with_response = "write" in props

        logger.info(
            "Write mode resolved",
            name=self.config.name,
            acknowledged=self._write_with_response,
            properties=sorted(props),
        )
        if not self._write_with_response:
            logger.warning(
                "Characteristic is write-without-response only — no transport "
                "backpressure available; relying on paced writes alone",
                name=self.config.name,
            )

    @property
    def transmit_interval(self) -> float:
        """Pacing for the current write mode. Blind writes get a slower cadence."""
        return (
            TRANSMIT_INTERVAL
            if self._write_with_response
            else UNACKED_TRANSMIT_INTERVAL
        )

    async def _write(self, payload: bytes) -> float:
        """One GATT write. Returns how long the transport actually took.

        Raises on failure; the caller decides whether that means the link died.
        """
        target_char = (
            self._write_char if self._write_char is not None else settings.ble.write_uuid
        )
        started = time.monotonic()
        await asyncio.wait_for(
            self._client.write_gatt_char(
                target_char, payload, response=self._write_with_response
            ),
            timeout=WRITE_TIMEOUT,
        )
        latency = time.monotonic() - started

        self._last_write_latency = latency
        # EWMA so a single slow write does not swamp the reading.
        self._write_latency_ewma = (
            latency
            if self._write_latency_ewma == 0.0
            else self._write_latency_ewma * 0.8 + latency * 0.2
        )
        return latency

    async def _transmit_loop(self):
        """
        Grab the freshest colour, write it, wait out the remainder of the
        interval, repeat. Strictly one write in flight at a time, ever.

        The colour is sampled at the last possible instant before the payload is
        built — never before an await — so a slow link can never put a stale
        frame on the wire. Anything the engine produced while the previous write
        was in flight is simply overwritten and dropped.

        The sleep is the REMAINDER of the interval, not a flat delay. When the
        link is slower than TRANSMIT_INTERVAL that remainder is zero and the loop
        self-limits to the link's real rate, so the producer can never outrun the
        drain and no backlog can form — in the host stack or anywhere else.
        """
        logger.info("Transmit loop started", name=self.config.name)

        # Stagger this strip's cadence against its siblings.
        if self._phase_offset:
            await asyncio.sleep(self._phase_offset)

        while not self._intentional_disconnect:
            self._transmit_heartbeat = time.monotonic()

            if not self.is_connected:
                # Strip is down — make sure recovery is actually running.
                self._mark_down("transmit loop saw strip not connected")
                await asyncio.sleep(0.5)
                continue

            if self._write_char is None:
                self._resolve_write_mode()

            cycle_start = time.monotonic()

            # Freshest colour, read immediately before the payload is built.
            target = self._target_color
            stale = (cycle_start - self._last_write_ts) >= KEEPALIVE_INTERVAL

            # The keepalive rewrite is what makes a silent death detectable
            # while the room is dark and the colour never changes.
            if target != self._last_written_color or stale:
                payload = build_color_payload(*target)
                try:
                    await self._write(payload)
                    self._last_written_color = target
                    self._last_write_ts = time.monotonic()
                    self._ack_failures = 0
                except (asyncio.TimeoutError, TimeoutError):
                    # An acknowledged write that never gets its ACK usually means
                    # the firmware lied about supporting it. Fall back rather
                    # than stalling the strip forever.
                    if self._write_with_response:
                        self._ack_failures += 1
                        if self._ack_failures >= ACK_FAILURE_LIMIT:
                            logger.warning(
                                "Acknowledged writes repeatedly timed out — "
                                "falling back to unacknowledged writes",
                                name=self.config.name,
                            )
                            self._write_with_response = False
                            self._ack_failures = 0
                        await asyncio.sleep(self.transmit_interval)
                        continue
                    logger.warning(
                        "GATT write timed out — flagging disconnected",
                        name=self.config.name,
                        timeout=WRITE_TIMEOUT,
                    )
                    self._mark_down("write timed out")
                    continue
                except (BleakError, OSError) as e:
                    logger.warning(
                        "GATT write failed — flagging disconnected",
                        name=self.config.name,
                        error=str(e),
                    )
                    self._mark_down(f"write failed: {e}")
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Catch-all for unexpected errors so the loop never dies silently
                    logger.error(
                        "Unexpected transmit error",
                        name=self.config.name,
                        error=str(e),
                    )
                    self._mark_down(f"unexpected transmit error: {e}")
                    continue

            # Sleep only the unused remainder. A write that overran the interval
            # yields zero delay — we go straight back for the newest colour
            # instead of trying to catch up on the ones we missed.
            elapsed = time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, self.transmit_interval - elapsed))

        logger.info("Transmit loop exited", name=self.config.name)

    # ── Bulletproof Reconnect ──────────────────────────────────────────────────

    def _on_disconnect(self, _client: BleakClient):
        """Bleak disconnect callback. May fire from a non-loop thread."""
        if self._intentional_disconnect:
            return
        logger.warning("Strip disconnected (callback)", name=self.config.name)
        loop = self._loop
        if loop and not loop.is_closed():
            # Hop back onto the event loop — create_task is not thread-safe.
            loop.call_soon_threadsafe(self._mark_down, "disconnect callback")
        else:
            self._connected = False
            self._last_written_color = None
            self._write_char = None

    async def _reconnect_loop(self):
        async with self._reconnect_lock:
            if self.is_connected:
                return  # Another task already reconnected

            # Wait out the boot stagger before touching the radio at all. This
            # is where the delay is actually enforced: the watchdog and the
            # transmit loop both call _trigger_reconnect() on a disconnected
            # strip, but those are no-ops while THIS task is alive, so parking
            # here holds the whole worker off the adapter.
            hold = self._connect_not_before - time.monotonic()
            if hold > 0:
                logger.info(
                    "Holding off initial connect (staggered boot)",
                    name=self.config.name,
                    delay=round(hold, 2),
                )
                await asyncio.sleep(hold)

            logger.info("Starting connection attempts", name=self.config.name)
            attempt = 0

            while not self._intentional_disconnect and not self.is_connected:
                self._reconnect_attempts += 1
                device = None
                try:
                    # One strip at a time on the adapter. Uncontended — a
                    # single strip dropping mid-session — this is a free
                    # acquire and costs the reconnect nothing. The settle
                    # sleep is held INSIDE the gate so it spaces the next
                    # strip's handshake instead of just delaying this one.
                    async with self._connect_gate:
                        try:
                            device = await BleakScanner.find_device_by_address(
                                self.config.mac, timeout=SCAN_TIMEOUT
                            )
                            if device:
                                self._client = await establish_connection(
                                    BleakClientWithServiceCache,
                                    device,
                                    self.config.name,
                                    disconnected_callback=self._on_disconnect,
                                    max_attempts=settings.ble.reconnect_attempts,
                                )
                            else:
                                self._last_error = "device not found in scan"
                        finally:
                            await asyncio.sleep(CONNECT_SETTLE_S)

                    if device:
                        if self._client and self._client.is_connected:
                            self._connected = True
                            # Decide acknowledged vs unacknowledged writes now
                            # that the service table is available.
                            self._resolve_write_mode()
                            # Force an immediate repaint on the recovered strip.
                            self._last_written_color = None
                            self._last_write_ts = 0.0
                            self._last_connected_ts = time.time()
                            self._last_error = None
                            logger.info(
                                "Connected successfully",
                                name=self.config.name,
                                attempts=self._reconnect_attempts,
                            )
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._last_error = str(e)
                    logger.error(
                        "Connection attempt failed", name=self.config.name, error=str(e)
                    )

                # Escalating backoff, capped. It will retry forever.
                delay = (
                    RECONNECT_DELAYS[attempt]
                    if attempt < len(RECONNECT_DELAYS)
                    else RECONNECT_DELAY_MAX
                )
                attempt += 1
                await asyncio.sleep(delay)

    # ── Power Control ──────────────────────────────────────────────────────────

    async def set_power(self, on: bool):
        if not self.is_connected:
            self._mark_down("power write while not connected")
            return
        payload = build_power_payload(on)
        try:
            await self._write(payload)
            self._last_write_ts = time.monotonic()
        except (asyncio.TimeoutError, TimeoutError):
            logger.error("Power write timed out", name=self.config.name)
            self._mark_down("power write timed out")
        except Exception as e:
            logger.error("Power write failed", name=self.config.name, error=str(e))
            self._mark_down(f"power write failed: {e}")

    # ── Clean Shutdown ─────────────────────────────────────────────────────────

    async def disconnect(self):
        self._intentional_disconnect = True

        for task in (self._transmit_task, self._watchdog_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._connected = False
        self._write_char = None
