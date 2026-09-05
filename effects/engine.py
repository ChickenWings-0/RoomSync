import asyncio
import json
import time
import math
from typing import Dict, List, Tuple, Optional
import structlog
from dataclasses import dataclass, field

from config import settings
from utils.helpers import drain_latest, drain_all
from utils.color import apply_brightness, hsv_to_rgb, rgb_to_hsv, clamp, clamp01
from ble.manager import BLEManager
from samplers.screen import ScreenSampler, ScreenFrame
from samplers.audio import AudioFrame
from peripherals.openrgb_bridge import OpenRGBBridge

from .modes import Mode
from .topology import STRIP_MIDPOINTS
from .palettes import PALETTES
from .patterns import PatternState, RenderCtx
from .registry import (SPECS, SMOOTH_AUDIO, SMOOTH_SCREEN, allows,
                       is_available, unavailable_reason)

logger = structlog.get_logger()

_BANDS = ("bass", "mids", "highs", "punch")

BLACK = (0, 0, 0)

# ── Two bodies, one soul ──────────────────────────────────────────────────
# Modes whose output is a single musical colour for the whole space rather
# than a spatial field. For these the two targets are deliberately NOT fed
# the same signal:
#
#   PC   (OpenRGB, loopback socket, 30 Hz) — the raw master colour. Every
#        transient, no smoothing. The socket can take it, so it gets all of it.
#   Room (BLE, 2.4 GHz radio, 10 Hz ceiling) — the SAME colour, low-passed.
#        Anything faster than the macro-rhythm cannot physically land on the
#        strips: the transmit loop coalesces it, and what does arrive reads as
#        fluctuation and smear rather than as detail.
#
# Same soul, two bodies. The PC plays the beat; the room breathes the bar.
#
# Which modes those are is now ModeSpec.room_smoothing == SMOOTH_AUDIO in
# effects/registry.py, alongside every other per-mode routing decision. This
# used to be three frozensets here, one of which (_ROOM_SMOOTHED_MODES) was
# never read and another of which (_COLOR_DRIVEN_MODES) silently painted the
# PC with the room's colour if you forgot to add a mode to it.

# Room low-pass time constants. Filtered in HSV, not RGB: a straight RGB lerp
# from red to blue passes through grey, which is exactly the washed-out mush
# we are trying to avoid. Hue takes the short way round the wheel instead, so
# the room sweeps through the spectrum rather than desaturating across it.
#
# Colour drifts slowly (roughly one gesture per phrase); brightness follows
# faster, so the room still visibly breathes with the kick instead of settling
# into a constant glow. Tune by ear — these are the only two knobs that matter.
ROOM_COLOR_TAU_S = 0.28     # hue + saturation glide
ROOM_VALUE_TAU_S = 0.11     # brightness envelope

# SCREEN_SYNC gets its own, much slower pair. A scene cut is a step function,
# not a rhythm — an instantaneous jump to an unrelated colour. The audio taus
# above are tuned to let the room breathe with a kick, and at that speed a cut
# arrives on the strips as a flash, which is precisely the aggressive flicker
# we are trying to remove. Three-ish times slower turns the same cut into a
# wash. The PC does NOT go through this: it takes the screen raw.
SCREEN_COLOR_TAU_S = 0.65   # hue + saturation glide across a scene cut
SCREEN_VALUE_TAU_S = 0.35   # brightness envelope

@dataclass
class Command:
    type: str
    payload: dict

def _seed_mode(target: str) -> Mode:
    """The configured start mode, or STATIC if it is not legal for this target.

    config.general.mode is a single value shared by both outputs, so pointing
    it at a PC-exclusive pattern would otherwise boot the room into a mode the
    command layer would refuse to set — unreachable state that only a restart
    could produce and nothing could clear.
    """
    try:
        mode = Mode(settings.general.mode)
    except ValueError:
        return Mode.STATIC
    return mode if allows(mode, target) else Mode.STATIC


@dataclass
class AppState:
    # ── Independent per-target modes ─────────────────────────
    # There is no longer a single "the mode". Room and PC each carry their
    # own, so the room can pulse to the beat while the PC holds a flat
    # custom colour, or the exact reverse. Both seed from config.general.mode.
    room_mode: Mode = field(default_factory=lambda: _seed_mode("room"))
    pc_mode: Mode = field(default_factory=lambda: _seed_mode("pc"))

    # -- One brightness per output -------------------------------
    # Split for the same reason the speeds are: a room dimmed to 20% behind a
    # monitor running flat out is a normal way to use the two, and one knob
    # cannot say it. Both seed from config.general.brightness, so an
    # untouched install still behaves exactly as it did.
    room_brightness: float = settings.general.brightness
    pc_brightness: float = settings.general.brightness

    # -- One speed and one palette per output --------------------
    # Same split as the modes and the pickers, and for the same reason: with
    # independent patterns these are an active conflict, not a convenience.
    # A room breathing at 0.3x and a PC strobing at 3x cannot share one knob.
    room_speed: float = 1.0
    pc_speed: float = 1.0
    room_palette: str = "rainbow"
    pc_palette: str = "rainbow"

    # ── One picker per output ────────────────────────────────
    # Same split as the modes: STATIC on both targets no longer implies the
    # same colour on both. They start equal, which is what lets the UI open
    # with its Link Colors box checked.
    room_static_color: Tuple[int, int, int] = (255, 147, 41)  # Warm white
    pc_static_color: Tuple[int, int, int] = (255, 147, 41)    # Warm white

    # ── Target decoupling ────────────────────────────────────────
    # Each output is independently switchable. A disabled target is not
    # merely skipped — it is driven to black, so switching it off turns
    # those lights off rather than freezing them on the last frame.
    sync_room: bool = True   # BLE strips
    sync_pc: bool = True     # OpenRGB

    # ── Master power ─────────────────────────────────────────────
    # Distinct from sync_room / sync_pc, which are per-target routing. This is
    # the one switch on the dashboard, it covers both outputs at once, and —
    # unlike the old power button — it now lives in the engine rather than only
    # in the browser and the BLE workers. It has to: it is part of the state we
    # restore, and a power-off that the engine does not know about is a room
    # that comes back on by itself the moment the next tick renders a frame.
    power_on: bool = True

    # Room-valued alias for the pre-split single knob. Reading it gives the
    # room's, writing it sets BOTH — which is what every existing caller
    # (and every existing test) meant by "the brightness".
    @property
    def brightness(self) -> float:
        return self.room_brightness

    @brightness.setter
    def brightness(self, value: float):
        self.room_brightness = self.pc_brightness = value

    # ── Serialisation ────────────────────────────────────────────
    # One field list, used by BOTH the persistence layer and the /api/state
    # route. They were always the same set of values, and keeping two hand
    # written copies of it is how a newly added knob ends up restored but not
    # reported, or reported but not restored. The Mode enums and the colour
    # tuples are the only fields that are not already JSON scalars, so they are
    # the only two that need converting.

    def to_dict(self) -> dict:
        """A JSON-safe snapshot of every user-settable value.

        Deliberately excludes derived and transient state — DSP envelopes,
        pattern phases, the room's HSV filter. Those are rebuilt from the first
        frame after a restart and restoring them would only reproduce a stale
        moment of a signal that no longer exists.
        """
        return {
            "room_mode": self.room_mode.value,
            "pc_mode": self.pc_mode.value,
            "room_brightness": self.room_brightness,
            "pc_brightness": self.pc_brightness,
            "room_speed": self.room_speed,
            "pc_speed": self.pc_speed,
            "room_palette": self.room_palette,
            "pc_palette": self.pc_palette,
            "room_static_color": list(self.room_static_color),
            "pc_static_color": list(self.pc_static_color),
            "sync_room": self.sync_room,
            "sync_pc": self.sync_pc,
            "power_on": self.power_on,
        }

    def apply_dict(self, data: dict) -> List[str]:
        """Merge a stored snapshot in, field by field. Returns what applied.

        Every field is optional and every field is validated independently: a
        state file written by an older build simply lacks the newer keys, and a
        corrupt or hand-edited value is skipped rather than taken. A restore
        must never be able to put the engine somewhere the command layer would
        refuse to — which is why the modes go through `allows()` here, exactly
        as _apply_command does.
        """
        applied: List[str] = []
        if not isinstance(data, dict):
            return applied

        for target in ("room", "pc"):
            raw = data.get(target + "_mode")
            if raw is None:
                continue
            try:
                mode = Mode(raw)
            except ValueError:
                continue   # a mode this build no longer has: keep the default
            if allows(mode, target):
                setattr(self, target + "_mode", mode)
                applied.append(target + "_mode")

        for key, lo, hi in (("room_brightness", 0.0, 1.0),
                            ("pc_brightness", 0.0, 1.0),
                            ("room_speed", 0.1, 100.0),
                            ("pc_speed", 0.1, 100.0)):
            if key in data:
                try:
                    setattr(self, key, max(lo, min(hi, float(data[key]))))
                    applied.append(key)
                except (TypeError, ValueError):
                    pass

        for key in ("room_palette", "pc_palette"):
            value = data.get(key)
            # Checked against the live palette table: a palette that was
            # renamed or removed between builds would otherwise be restored as
            # a name every lookup then has to defend against.
            if isinstance(value, str) and value in PALETTES:
                setattr(self, key, value)
                applied.append(key)

        for key in ("room_static_color", "pc_static_color"):
            value = data.get(key)
            if isinstance(value, (list, tuple)) and len(value) == 3:
                try:
                    setattr(self, key, tuple(clamp(int(c)) for c in value))
                    applied.append(key)
                except (TypeError, ValueError):
                    pass

        for key in ("sync_room", "sync_pc", "power_on"):
            if key in data:
                setattr(self, key, bool(data[key]))
                applied.append(key)

        return applied

def _hue_lerp(a: float, b: float, t: float) -> float:
    """Interpolate hue along the shortest path around the colour wheel."""
    d = ((b - a) + 0.5) % 1.0 - 0.5
    return (a + d * t) % 1.0

def _reduce_audio(frames: List[AudioFrame]) -> Optional[AudioFrame]:
    """Peak-preserving decimation from ~47 Hz audio to the 30 Hz tick."""
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    return AudioFrame(
        bass=max(f.bass for f in frames),
        mids=max(f.mids for f in frames),
        highs=max(f.highs for f in frames),
        ts=frames[-1].ts,
        flux_bass=max(f.flux_bass for f in frames),
        flux_mids=max(f.flux_mids for f in frames),
        flux_highs=max(f.flux_highs for f in frames),
    )

@dataclass
class _AudioDSP:
    """One target's audio analysis state.

    Previously a single set of envelopes lived on the engine, which was
    correct only while both outputs ran the same mode. They no longer do.
    MUSIC and AUDIO_REACTIVE drive the *same* AGC and follower code with
    different time constants, floors and hold windows — run both against one
    shared dict and each target's filter eats the other's state one tick
    later, so neither output is the mode it claims to be.

    One bundle per target. They are cheap (six floats and three small dicts)
    and they make the two signal chains genuinely independent.
    """
    env: Dict[str, float] = field(default_factory=lambda: {b: 0.0 for b in _BANDS})   # post-AGC envelopes, 0..1
    peak: Dict[str, float] = field(default_factory=lambda: {b: 0.0 for b in _BANDS})  # AGC running peaks
    hold: Dict[str, float] = field(default_factory=lambda: {b: 0.0 for b in _BANDS})  # hold-until timestamps
    hue: float = settings.audio.reactive.base_hsv[0]
    music_band: str = "bass"   # winner-takes-all incumbent for MUSIC
    music_peak: float = 0.0    # running peak of the MUSIC dominance contest

    def reset(self):
        for b in _BANDS:
            self.env[b] = 0.0
            self.peak[b] = 0.0
            self.hold[b] = 0.0
        self.hue = settings.audio.reactive.base_hsv[0]
        self.music_band = "bass"
        self.music_peak = 0.0


class EffectEngine:
    def __init__(self, ble_mgr: BLEManager, screen: ScreenSampler, audio_queue):
        self.ble_mgr = ble_mgr
        self.screen = screen
        self.audio_queue = audio_queue
        self.rgb_bridge: Optional[OpenRGBBridge] = None

        self.state = AppState()
        self.command_bus: asyncio.Queue = asyncio.Queue()
        self.ws_clients: set = set()

        # Set by the tick loop whenever a command was applied, cleared by the
        # StatePersister task. A plain bool and nothing more: this is touched
        # from inside a 30 Hz loop that also has to talk to a radio, so the
        # producer side must cost a single attribute store. Everything
        # expensive — deciding whether to write, serialising, the disk I/O —
        # lives in persistence.StatePersister, off this loop entirely.
        self._state_dirty = False

        # ── Audio DSP state, one bundle per target ───────────
        self._last_tick: Optional[float] = None
        self._dsp: Dict[str, _AudioDSP] = {
            "room": _AudioDSP(),
            "pc": _AudioDSP(),
        }

        # -- Generative pattern state, one bundle per target ---
        # Exactly the reasoning in the _AudioDSP docstring above, applied to
        # phase accumulators and RNG streams. Two targets running the same
        # pattern at different speeds share nothing; run them against one
        # bundle and each eats the other's phase one tick later, so neither
        # output is the pattern it claims to be.
        self._pattern: Dict[str, PatternState] = {
            "room": PatternState(),
            "pc": PatternState(),
        }

        # Room low-pass state, held in HSV floats so small deltas are not
        # rounded away to nothing. Now keyed BY STRIP: SCREEN_SYNC needs three
        # independent filters so the left wall can fade to what is on the left
        # of the screen while the right wall does something else entirely. The
        # unified audio modes feed all three the same input and therefore stay
        # in lockstep for free. A missing key = disarmed: that strip's next
        # frame snaps instead of fading up out of stale state.
        self._room_hsv: Dict[str, Tuple[float, float, float]] = {}

        self._screen_current_colors: Dict[str, Tuple[int, int, int]] = {
            "OA10 33": (0, 0, 0),
            "OA10 30": (0, 0, 0),
            "OA10 20": (0, 0, 0),
        }


    async def run(self):
        tick_interval = 1.0 / settings.general.tick_hz
        logger.info("EffectEngine tick loop started", target_hz=settings.general.tick_hz)

        # Absolute deadline, not "sleep the leftover". Windows can only wake us
        # on a timer boundary, so an individual sleep overshoots; scheduling
        # against a fixed deadline makes the NEXT sleep correspondingly shorter,
        # which holds the average at the configured rate instead of letting the
        # overshoot compound. Worth ~30 Hz vs ~26 Hz measured.
        next_tick = time.monotonic()

        while True:
            tick_start = time.monotonic()

            # Measured dt — every rate below is frame-rate independent.
            now = tick_start
            dt = tick_interval if self._last_tick is None else (now - self._last_tick)
            dt = min(max(dt, 1e-3), 0.25)   # guard first tick, stalls, debugger pauses
            self._last_tick = now

            screen_frame = drain_latest(self.screen.screen_queue)
            audio_frame = _reduce_audio(drain_all(self.audio_queue))

            cmds = drain_all(self.command_bus)
            for cmd in cmds:
                self._apply_command(cmd)
            if cmds:
                # Commands are the ONLY thing that mutates persisted state, so
                # this one line is the whole producer side of persistence. It
                # is deliberately coarse — a rejected command still marks the
                # state dirty — because comparing snapshots to find out whether
                # anything really changed would cost more, every tick, than the
                # occasional redundant write it would save.
                self._state_dirty = True

            room_mode = self.state.room_mode
            pc_mode = self.state.pc_mode

            # Render once and share only when the two targets would genuinely
            # produce the same frame. Same mode has never been sufficient —
            # both can sit in STATIC on two different picker colours — and now
            # that speed and palette are per-target too, the whole parameter
            # tuple has to agree. _render_key is that tuple.
            share = self._render_key("room") == self._render_key("pc")

            if share:
                # Sharing is not merely an optimisation — SCREEN_SYNC's frame
                # hold must advance exactly once per tick, and this is what
                # guarantees the two audio targets stay bit-identical rather
                # than merely converging from two separately-warmed filters.
                frame = self._compute_mode(
                    "room", room_mode, screen_frame, audio_frame, now, dt
                )
                room_source = pc_source = frame
            else:
                room_source = self._compute_mode(
                    "room", room_mode, screen_frame, audio_frame, now, dt
                )
                pc_source = self._compute_mode(
                    "pc", pc_mode, screen_frame, audio_frame, now, dt
                )

            # Two rendered frames, each routed to its own hardware reality.
            room_colors, pc_color = self._route(
                room_source, room_mode, pc_source, pc_mode, dt
            )

            for strip_name, color in room_colors.items():
                self.ble_mgr.set_color(strip_name, color[0], color[1], color[2])

            if self.rgb_bridge:
                # Synchronous hand-off, exactly like the BLE strips: overwrite
                # the target and return. The bridge's own loop pushes at the
                # full tick rate, so the PC lights get every frame — and a slow
                # socket write can never stall this loop or starve the strips.
                self.rgb_bridge.set_target_color(pc_color)

            if self.ws_clients:
                await self._broadcast_ws(room_colors, pc_color)

            next_tick += tick_interval
            delay = next_tick - time.monotonic()
            if delay < -tick_interval:
                # Fell more than a whole tick behind (debugger pause, GC, a
                # sampler stall). Resync rather than sprinting to "catch up",
                # which would burn a burst of frames nobody would see.
                next_tick = time.monotonic() + tick_interval
                delay = tick_interval
            await asyncio.sleep(max(0.0, delay))

    async def blackout(self, settle_s: float = 0.35):
        """Drive every output to black and wait for it to actually land.

        The shutdown path's first step, and the reason it exists is the BLE
        hand-off contract: `set_color` is a plain assignment that the worker's
        own transmit loop picks up later. Disconnecting immediately after
        writing black therefore leaves the strips lit on whatever colour was
        last actually transmitted — the app exits and the room stays on, which
        is the single most visible way a "clean" shutdown can be wrong.

        `settle_s` is the wait for that transmit loop to run. TRANSMIT_INTERVAL
        is 100 ms and doubles to 200 ms on the unacknowledged write path, so
        350 ms covers a full cycle on the slow path plus the write itself. It
        is a bounded wait, not a confirmation: a strip that is disconnected or
        wedged must not be able to hold the process open.

        Must be called with the tick loop already stopped. Running against a
        live engine would work exactly once, until the next tick repainted
        everything from the current mode.
        """
        logger.info("Blacking out all outputs")

        self.state.power_on = False
        self._room_hsv.clear()

        for strip_name in STRIP_MIDPOINTS:
            self.ble_mgr.set_color(strip_name, 0, 0, 0)

        if self.rgb_bridge:
            # The out-of-band push, not the tick hand-off: the bridge's own
            # loop is being cancelled around now, so leaving black in
            # _target_color for it to pick up would be a race we would lose.
            try:
                await self.rgb_bridge.set_unified_color(BLACK)
            except Exception as exc:
                logger.warning("OpenRGB blackout failed", error=str(exc))

        await asyncio.sleep(settle_s)

    # ── Mode dispatch ─────────────────────────────────────

    def _compute_mode(
        self,
        target: str,
        mode: Mode,
        screen_frame: Optional[ScreenFrame],
        audio_frame: Optional[AudioFrame],
        now: float,
        dt: float,
    ) -> Dict[str, Tuple[int, int, int]]:
        """Render one mode into a per-strip colour field, for one target.

        A registry lookup rather than an if/elif chain. Everything the mode is
        allowed to see is resolved for THIS target first and handed over as one
        RenderCtx: its picker, its speed, its palette, its DSP bundle, its
        pattern state. A mode therefore cannot reach across and read the other
        target's parameters even by accident — which is the bug class that
        forced the per-target _AudioDSP split, generalised to everything.
        """
        ctx = RenderCtx(
            target=target,
            now=now,
            dt=dt,
            base_color=self._static_color_for(target),
            speed=self._speed_for(target),
            palette=self._palette_for(target),
            state=self._pattern[target],
            cfg=settings.patterns,
            engine=self,
            screen_frame=screen_frame,
            audio_frame=audio_frame,
            dsp=self._dsp[target],
        )
        return SPECS[mode].render(ctx)

    def _render_key(self, target: str):
        """A hashable summary of everything that decides this target's frame.

        Two targets may share a single render only if their keys are equal. The
        key names exactly the inputs the mode's spec says it reads, so a mode
        that ignores the palette does not lose the fast path merely because the
        two palettes happen to differ.

        A stochastic mode returns a fresh object, which never compares equal:
        sharing one render would advance only one target's RNG, leaving the
        other to resume from cold state the moment anything unlinked them.
        Rendering twice is the cheaper problem.
        """
        spec = SPECS[self._mode_for(target)]
        if spec.stochastic:
            return object()
        return (
            spec.mode,
            self._static_color_for(target) if spec.color_driven else None,
            self._speed_for(target) if spec.speed_driven else None,
            self._palette_for(target) if spec.palette_driven else None,
        )

    # -- Routing --------------------------------------------------

    def _route(
        self,
        room_source: Dict[str, Tuple[int, int, int]],
        room_mode: Mode,
        pc_source: Dict[str, Tuple[int, int, int]],
        pc_mode: Mode,
        dt: float,
    ) -> Tuple[Dict[str, Tuple[int, int, int]], Tuple[int, int, int]]:
        """Route each target's own rendered frame to its own hardware.

        Returns (room_colors, pc_color), each scaled by ITS OWN brightness.
        Brightness is applied HERE, after the room filter rather than before
        it, so dragging a slider is instant on both paths instead of fading
        in through the low-pass.

        The two branches are now fully independent: the room decides whether
        to low-pass from ITS mode, the PC collapses ITS own frame. When both
        targets share a mode the caller passes the same dict twice, which
        reproduces the old "same soul, two bodies" behaviour exactly.
        """
        # Master power sits ahead of everything else: off means both outputs
        # are driven to black and KEEP being driven to black, exactly like a
        # de-synced target. Not "stop writing" — the strips would then simply
        # hold whatever colour they were last sent.
        if not self.state.power_on:
            self._room_hsv.clear()
            return {name: BLACK for name in STRIP_MIDPOINTS}, BLACK

        room_brightness = self.state.room_brightness
        pc_brightness = self.state.pc_brightness
        room_spec = SPECS[room_mode]

        # ── PC: the raw master colour of the PC's own mode ──
        # OpenRGB is a single logical zone, so a spatial field collapses to a
        # single colour — the most vibrant one in the frame, not its average;
        # see _dominant_color. WAVE on the PC reads as the palette cycling,
        # which is the only thing it can mean on one zone.
        if self.state.sync_pc:
            pc_color = apply_brightness(self._dominant_color(pc_source), pc_brightness)
        else:
            # Strict blackout, applied AFTER brightness rather than through
            # it, so "off" is exactly (0, 0, 0) at any slider position.
            pc_color = BLACK

        # ── Room: temporally smoothed on the audio AND screen modes ──
        if not self.state.sync_room:
            # Blackout, and keep writing it. The worker de-duplicates writes
            # itself, so this costs one GATT write plus the keepalive — but it
            # guarantees the strips actually go dark even if the toggle is
            # flipped while a write is in flight.
            self._room_hsv.clear()
            room_colors = {name: BLACK for name in STRIP_MIDPOINTS}
        elif room_spec.room_smoothing == SMOOTH_AUDIO:
            # One master colour, broadcast to all three strips and then
            # filtered. Identical input and identical filter state means the
            # three stay in exact lockstep, which is the point.
            master = self._dominant_color(room_source)
            smoothed = self._smooth_dict_for_room(
                {name: master for name in STRIP_MIDPOINTS},
                dt, ROOM_COLOR_TAU_S, ROOM_VALUE_TAU_S,
            )
            room_colors = {
                name: apply_brightness(color, room_brightness)
                for name, color in smoothed.items()
            }
        elif room_spec.room_smoothing == SMOOTH_SCREEN:
            # The spatial field SURVIVES — left screen edge still drives the
            # left wall — but each strip is low-passed on its own accumulator.
            # The room becomes an ambient extension of the screen that washes
            # across a scene cut instead of strobing on it. The PC, meanwhile,
            # took the same source dict raw a few lines above.
            smoothed = self._smooth_dict_for_room(
                room_source, dt, SCREEN_COLOR_TAU_S, SCREEN_VALUE_TAU_S
            )
            room_colors = {
                name: apply_brightness(color, room_brightness)
                for name, color in smoothed.items()
            }
        else:
            # SMOOTH_NONE: the generative patterns. They are driven by the
            # clock rather than by content, so there are no scene cuts to wash
            # out, and every one of them is band-limited below the radio's
            # worst-case 2.5 Hz Nyquist floor by construction (see the module
            # docstring in effects/patterns.py). Filtering them again here
            # would add lag and remove nothing. They pass through untouched.
            self._room_hsv.clear()
            room_colors = {
                name: apply_brightness(color, room_brightness)
                for name, color in room_source.items()
            }

        return room_colors, pc_color

    def _smooth_dict_for_room(
        self,
        colors: Dict[str, Tuple[int, int, int]],
        dt: float,
        color_tau: float,
        value_tau: float,
    ) -> Dict[str, Tuple[int, int, int]]:
        """Low-pass a whole per-strip frame, each strip on its own filter.

        The taus are arguments rather than constants because the two callers
        want genuinely different behaviour out of the same filter: the audio
        modes want the room breathing with the bar, SCREEN_SYNC wants it
        washing across a cut roughly three times slower.
        """
        return {
            name: self._smooth_strip(
                name, colors.get(name, BLACK), dt, color_tau, value_tau
            )
            for name in STRIP_MIDPOINTS
        }

    def _smooth_strip(
        self,
        name: str,
        rgb: Tuple[int, int, int],
        dt: float,
        color_tau: float,
        value_tau: float,
    ) -> Tuple[int, int, int]:
        """One-pole low-pass on a single strip's colour.

        dt-correct, so the filter feels the same whatever the tick rate.

        Hue and saturation are HELD — not tracked — while the colour is at or
        near black. Pure black converts back to hue 0 / saturation 0, and
        letting that into the filter would drag the room towards red and grey
        every time MUSIC gates to darkness between kicks — or every time a
        film cuts to a dark frame. Only brightness follows into the gaps,
        which is exactly the breathing we want.
        """
        h, s, v = rgb_to_hsv(*rgb)

        prev = self._room_hsv.get(name)
        if prev is None:              # disarmed: snap, do not fade up from stale state
            self._room_hsv[name] = (h, s, v)
            return rgb

        prev_h, prev_s, prev_v = prev
        a_color = 1.0 - math.exp(-dt / color_tau)
        a_value = 1.0 - math.exp(-dt / value_tau)

        if s > 1e-3 and v > 1e-3:
            new_h = _hue_lerp(prev_h, h, a_color)
            new_s = prev_s + (s - prev_s) * a_color
        else:
            new_h, new_s = prev_h, prev_s

        new_v = prev_v + (v - prev_v) * a_value

        self._room_hsv[name] = (new_h, new_s, new_v)
        return hsv_to_rgb(new_h, clamp01(new_s), clamp01(new_v))

    # ── Commands ─────────────────────────────────────────────────

    def _apply_command(self, cmd: Command):
        if cmd.type in ("SET_MODE", "SET_ROOM_MODE", "SET_PC_MODE"):
            # One parser, three spellings. SET_ROOM_MODE / SET_PC_MODE are the
            # explicit forms; SET_MODE carries an optional "target" and still
            # means "both" when it is absent, so every pre-existing caller
            # (the REST route, an old cached page) keeps working untouched.
            try:
                mode = Mode(cmd.payload.get("mode"))
            except ValueError:
                return   # unknown mode: leave both targets exactly as they were

            if cmd.type == "SET_ROOM_MODE":
                target = "room"
            elif cmd.type == "SET_PC_MODE":
                target = "pc"
            else:
                target = str(cmd.payload.get("target") or "both").lower()
                if target not in ("room", "pc", "both"):
                    target = "both"

            # A mode can be refused for a target on hardware grounds: the
            # PC-exclusive patterns have features shorter than one BLE sample,
            # so assigning one to the room is not a preference we can honour.
            # Refused exactly like an unknown mode — that target keeps the mode
            # it had rather than going dark. Under target "both" the half that
            # is legal still applies.
            # A mode can also be refused because this MACHINE cannot feed it:
            # AUDIO_REACTIVE with no loopback device renders a flat nothing and
            # SCREEN_SYNC with no capture renders black, and silently accepting
            # either is how a user ends up with dark strips and no explanation.
            # Refused exactly like an illegal target — that target keeps the
            # mode it had. The UI greys these out from the catalog, so reaching
            # here means a stale page or a direct API call.
            if not is_available(mode):
                logger.warning("Refusing a mode this host cannot feed",
                               mode=mode.value, reason=unavailable_reason(mode))
                return

            if target in ("room", "both") and allows(mode, "room"):
                self.state.room_mode = mode
                self._reset_target("room")
            if target in ("pc", "both") and allows(mode, "pc"):
                self.state.pc_mode = mode
                self._reset_target("pc")
        elif cmd.type == "SET_BRIGHTNESS":
            # Same target grammar as SET_SPEED, absent still meaning both, so
            # a pre-split caller — the REST route, an old cached page — keeps
            # dimming everything with one command.
            try:
                value = max(0.0, min(1.0, float(cmd.payload.get("value", 0.85))))
            except (TypeError, ValueError):
                return   # malformed: leave both brightnesses standing
            for t in self._targets_in(cmd.payload):
                setattr(self.state, t + "_brightness", value)
        elif cmd.type == "SET_PALETTE":
            # Same target grammar as SET_MODE and SET_STATIC_COLOR, absent
            # still meaning both, so every pre-existing caller — the REST
            # route, an old cached page — keeps working untouched.
            name = cmd.payload.get("name", "rainbow")
            for t in self._targets_in(cmd.payload):
                setattr(self.state, t + "_palette", name)
        elif cmd.type == "SET_SPEED":
            try:
                value = max(0.1, float(cmd.payload.get("value", 1.0)))
            except (TypeError, ValueError):
                return   # malformed: leave both speeds standing
            for t in self._targets_in(cmd.payload):
                setattr(self.state, t + "_speed", value)
        elif cmd.type == "SET_STATIC_COLOR":
            color = self._parse_color(cmd.payload)
            if color is not None:
                # Same target grammar as SET_MODE, absent still meaning both,
                # so the UI's "Link Colors" mode is literally one command
                # rather than two that could land on different ticks.
                for t in self._targets_in(cmd.payload):
                    setattr(self.state, t + "_static_color", color)
        elif cmd.type == "SET_TARGETS":
            # Partial updates are allowed: a payload naming only one target
            # leaves the other exactly as it was.
            if "room" in cmd.payload:
                self.state.sync_room = bool(cmd.payload["room"])
            if "pc" in cmd.payload:
                self.state.sync_pc = bool(cmd.payload["pc"])
        elif cmd.type == "SET_POWER":
            # The master switch. Accepts "on" (what the REST route and the UI
            # send) or "value", so the socket and the HTTP paths can share one
            # spelling without either having to translate.
            raw = cmd.payload.get("on", cmd.payload.get("value"))
            if raw is not None:
                self.state.power_on = bool(raw)
                if self.state.power_on:
                    # Coming back on snaps to the current frame rather than
                    # fading up out of the HSV state the room held when it went
                    # dark, which is by now arbitrarily stale.
                    self._room_hsv.clear()

    @staticmethod
    def _parse_color(payload: dict) -> Optional[Tuple[int, int, int]]:
        """Accept either explicit r/g/b or the '#rrggbb' an <input type=color> sends.

        Returns None on anything unparseable, so a malformed command leaves the
        current colour standing rather than blacking the room out.
        """
        hexstr = payload.get("hex")
        if isinstance(hexstr, str):
            h = hexstr.lstrip("#")
            if len(h) == 6:
                try:
                    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
                except ValueError:
                    pass
        try:
            return (
                clamp(int(payload["r"])),
                clamp(int(payload["g"])),
                clamp(int(payload["b"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _reset_target(self, target: str):
        """Clear the state belonging to ONE target after its mode changed.

        Deliberately narrow. The old engine-wide reset would have wiped the
        room's audio followers — and its hue, and its AGC peaks — every time
        the PC was switched to a different mode, which is audible as the room
        dropping to black mid-song. Only state the changed target owns is
        touched.
        """
        self._dsp[target].reset()
        self._pattern[target].reset()

        if target == "room":
            # Disarm all three strip filters so the room snaps to the new
            # mode's first frame instead of fading into it out of stale HSV —
            # and out of the previous mode's time constants.
            self._room_hsv.clear()

        # The screen frame hold is shared between the targets, so it is only
        # rearmed when nobody was already driving it. Clearing it while the
        # OTHER output is mid-screen-sync would blank a running effect.
        other = "pc" if target == "room" else "room"
        if (self._mode_for(target) == Mode.SCREEN_SYNC
                and self._mode_for(other) != Mode.SCREEN_SYNC):
            self._screen_current_colors = {
                "OA10 33": (0, 0, 0),
                "OA10 30": (0, 0, 0),
                "OA10 20": (0, 0, 0),
            }

    def _mode_for(self, target: str) -> Mode:
        return self.state.room_mode if target == "room" else self.state.pc_mode

    def _static_color_for(self, target: str) -> Tuple[int, int, int]:
        """Whichever picker belongs to this target."""
        return (self.state.room_static_color if target == "room"
                else self.state.pc_static_color)

    def _speed_for(self, target: str) -> float:
        return self.state.room_speed if target == "room" else self.state.pc_speed

    def _palette_for(self, target: str) -> str:
        return self.state.room_palette if target == "room" else self.state.pc_palette

    @staticmethod
    def _targets_in(payload: dict) -> Tuple[str, ...]:
        """The 'target' grammar, in one place.

        "room" | "pc" | "both", with absent or unrecognised meaning both. Every
        per-target command routes through this, so they cannot drift apart —
        and so one command with target "both" stays atomic. Two separate
        commands could land on different ticks and visibly split the outputs.
        """
        target = str(payload.get("target") or "both").lower()
        if target == "room":
            return ("room",)
        if target == "pc":
            return ("pc",)
        return ("room", "pc")

    def _compute_static(self, target: str) -> Dict[str, Tuple[int, int, int]]:
        # The exact RGB that target's colour picker sent. Brightness is applied
        # once, in _route, so it scales this identically to every other mode.
        color = self._static_color_for(target)
        return {name: color for name in STRIP_MIDPOINTS}

    def _compute_screen_sync(self, frame: Optional[ScreenFrame]) -> Dict[str, Tuple[int, int, int]]:
        """The raw screen edges, held across sampler gaps. No smoothing here.

        The old fixed 0.15-per-tick lerp lived at this level, which meant it
        applied to BOTH outputs — the PC was watching the screen through a
        ~0.2 s lag it never needed, since a loopback socket can take every
        frame. Smoothing is now the room's business alone and happens in
        _route, where it can use time constants chosen for the radio instead
        of a compromise between the two. That lerp was also frame-rate
        dependent (a fraction per tick, not per second); its replacement is
        not.

        The sampler runs at 20 Hz against a 30 Hz tick, so roughly one tick in
        three carries no new frame. Holding the last one keeps both outputs
        steady rather than strobing to black in the gaps.
        """
        if frame:
            self._screen_current_colors = {
                "OA10 30": frame.left_rgb,
                "OA10 20": frame.right_rgb,
                "OA10 33": frame.bottom_rgb,
            }
        return dict(self._screen_current_colors)

    # ── Audio DSP ────────────────────────────────────────────────

    def _agc(self, dsp: "_AudioDSP", band: str, raw: float, dt: float, cfg=None) -> float:
        """Adaptive gain: normalise against a slowly-bleeding running peak."""
        cfg = cfg if cfg is not None else settings.audio.reactive
        p = dsp.peak[band] * math.exp(-dt / cfg.agc_tau_s)     # slow bleed-down
        if raw > p:
            p = raw                                            # instant peak capture
        dsp.peak[band] = p
        if p < cfg.agc_floor:                                  # silence: stay dark
            return 0.0
        return min(1.0, raw / p)

    def _follow(self, dsp: "_AudioDSP", band: str, target: float, now: float, dt: float, cfg=None) -> float:
        """Zero-smoothing attack, one-transmit-interval peak hold, exponential release."""
        cur = dsp.env[band]
        cfg = cfg if cfg is not None else settings.audio.reactive

        if target >= cur:                          # ATTACK — literally 0 smoothing
            dsp.env[band] = target
            dsp.hold[band] = now + cfg.hold_s      # guarantee the 10 Hz TX loop sees it
            return target

        if now < dsp.hold[band]:                   # HOLD
            return cur

        tau = cfg.release_s[band]                  # RELEASE — dt-correct
        dsp.env[band] = cur + (target - cur) * (1.0 - math.exp(-dt / tau))
        return dsp.env[band]

    def _compute_audio_reactive(
        self,
        dsp: "_AudioDSP",
        frame: Optional[AudioFrame],
        now: float,
        dt: float,
    ) -> Dict[str, Tuple[int, int, int]]:
        """AUDIO_REACTIVE: one high-fidelity master colour, no spatial field.

        The travelling wave that used to offset hue and brightness per strip is
        gone. The strips were its only possible canvas, and at a 10 Hz radio
        ceiling a moving offset does not arrive as motion — it arrives as three
        strips disagreeing at random. What ships instead is a single colour
        carrying every transient, which _route hands raw to the PC and
        low-passed to the room.
        """
        cfg = settings.audio.reactive

        if frame is not None:
            fw = cfg.flux_weights
            flux_raw = (fw[0] * frame.flux_bass
                        + fw[1] * frame.flux_mids
                        + fw[2] * frame.flux_highs)
            env_bass = self._follow(dsp, "bass", self._agc(dsp, "bass", frame.bass, dt), now, dt)
            env_mids = self._follow(dsp, "mids", self._agc(dsp, "mids", frame.mids, dt), now, dt)
            env_highs = self._follow(dsp, "highs", self._agc(dsp, "highs", frame.highs, dt), now, dt)
            punch = self._follow(dsp, "punch", self._agc(dsp, "punch", flux_raw, dt), now, dt)
        else:
            # No frame: keep breathing down to base rather than freezing.
            env_bass = self._follow(dsp, "bass", 0.0, now, dt)
            env_mids = self._follow(dsp, "mids", 0.0, now, dt)
            env_highs = self._follow(dsp, "highs", 0.0, now, dt)
            punch = self._follow(dsp, "punch", 0.0, now, dt)

        # ── Brightness: weighted energy, perceptual gamma, floored base
        w = cfg.band_weights
        e = w[0] * env_bass + w[1] * env_mids + w[2] * env_highs
        v = cfg.v_floor + (1.0 - cfg.v_floor) * (max(0.0, e) ** cfg.gamma)

        # ── Hue: continuous spectral arc (no teleport)
        tot = env_bass + env_mids + env_highs
        pos = ((0.0 * env_bass + 0.5 * env_mids + 1.0 * env_highs) / tot) if tot > 1e-6 else 0.0
        arc_hue = (cfg.hue_start + pos * cfg.hue_span) % 1.0

        hue_target = _hue_lerp(cfg.base_hsv[0], arc_hue, min(1.0, e / cfg.hue_full_at))
        tau = cfg.hue_tau_punch_s + (cfg.hue_tau_s - cfg.hue_tau_punch_s) * (1.0 - punch)
        dsp.hue = _hue_lerp(dsp.hue, hue_target, 1.0 - math.exp(-dt / tau))

        # ── Saturation: white-hot punch
        sat = cfg.sat_base - (cfg.sat_base - cfg.sat_peak) * punch

        # Absolute unity: every strip is the same colour, every tick.
        color = hsv_to_rgb(dsp.hue, clamp01(sat), clamp01(v))
        return {name: color for name in STRIP_MIDPOINTS}

    def _compute_music(
        self,
        dsp: "_AudioDSP",
        frame: Optional[AudioFrame],
        now: float,
        dt: float,
    ) -> Dict[str, Tuple[int, int, int]]:
        """MUSIC: one unified club strobe.

        All three strips carry an identical colour — no spatial offset, no
        travelling wave. Colour snaps instantly to whichever band owns the
        transient (winner-takes-all, zero hue smoothing); brightness hits
        maximum on the hit and decays hard to black in the gaps.
        """
        cfg = settings.audio.music

        if frame is not None:
            fw = cfg.flux_weights
            flux_raw = (fw[0] * frame.flux_bass
                        + fw[1] * frame.flux_mids
                        + fw[2] * frame.flux_highs)
            env_bass = self._follow(dsp, "bass", self._agc(dsp, "bass", frame.bass, dt, cfg), now, dt, cfg)
            env_mids = self._follow(dsp, "mids", self._agc(dsp, "mids", frame.mids, dt, cfg), now, dt, cfg)
            env_highs = self._follow(dsp, "highs", self._agc(dsp, "highs", frame.highs, dt, cfg), now, dt, cfg)
            punch = self._follow(dsp, "punch", self._agc(dsp, "punch", flux_raw, dt, cfg), now, dt, cfg)
        else:
            # No frame: collapse to darkness rather than freezing on the last flash.
            env_bass = self._follow(dsp, "bass", 0.0, now, dt, cfg)
            env_mids = self._follow(dsp, "mids", 0.0, now, dt, cfg)
            env_highs = self._follow(dsp, "highs", 0.0, now, dt, cfg)
            punch = self._follow(dsp, "punch", 0.0, now, dt, cfg)

        # -- Brightness: hard gamma, near-zero floor, gated to true black
        w = cfg.band_weights
        e = w[0] * env_bass + w[1] * env_mids + w[2] * env_highs
        v = cfg.v_floor + (1.0 - cfg.v_floor) * (max(0.0, e) ** cfg.gamma)
        if v < cfg.v_gate:
            v = 0.0

        # -- Hue: winner-takes-all. No interpolation, no glide — it snaps.
        # Judged on RAW band magnitudes: the per-band AGC normalises every band
        # to ~1.0 on its own scale, so post-AGC envelopes carry no information
        # about which band actually owns the transient. band_bias compensates
        # for bass being intrinsically the loudest.
        bias = cfg.band_bias
        if frame is not None:
            scores = {
                "bass": frame.bass * bias[0],
                "mids": frame.mids * bias[1],
                "highs": frame.highs * bias[2],
            }
        else:
            scores = {"bass": 0.0, "mids": 0.0, "highs": 0.0}
        # Gate the contest on how loud this frame is relative to the loudest
        # recent frame, so the near-silent gaps between kicks cannot repaint
        # the room while the flash is decaying.
        tot = scores["bass"] + scores["mids"] + scores["highs"]
        dsp.music_peak = max(tot, dsp.music_peak * math.exp(-dt / cfg.agc_tau_s))

        challenger = max(scores, key=scores.get)
        if dsp.music_peak > cfg.agc_floor and tot >= cfg.dominance_floor * dsp.music_peak:
            # Hysteresis: only unseat the incumbent on a clearly stronger band,
            # so sustained material does not flicker between two colours.
            incumbent = scores.get(dsp.music_band, 0.0)
            if (challenger != dsp.music_band
                    and scores[challenger] >= incumbent * cfg.switch_margin):
                dsp.music_band = challenger
        hue = {
            "bass": cfg.hue_bass,
            "mids": cfg.hue_mids,
            "highs": cfg.hue_highs,
        }[dsp.music_band] % 1.0

        # -- Saturation: white-hot core on a hard transient
        sat = cfg.sat_base - (cfg.sat_base - cfg.sat_peak) * punch

        color = hsv_to_rgb(hue, clamp01(sat), clamp01(v))
        # Absolute unity: every strip is the same colour, every tick.
        return {name: color for name in STRIP_MIDPOINTS}

    def _compute_pulse(self, target: str, now: float, speed: float) -> Dict[str, Tuple[int, int, int]]:
        colors = {}
        width = 0.08
        base_color = (0, 0, 0)
        pulse_color = self._static_color_for(target)

        pulse_pos = (now * speed) % 1.0

        for name, t_mid in STRIP_MIDPOINTS.items():
            delta = abs(t_mid - pulse_pos)
            delta = min(delta, 1.0 - delta)
            intensity = math.exp(-(delta ** 2) / (2 * width ** 2))

            r = int(base_color[0] + (pulse_color[0] - base_color[0]) * intensity)
            g = int(base_color[1] + (pulse_color[1] - base_color[1]) * intensity)
            b = int(base_color[2] + (pulse_color[2] - base_color[2]) * intensity)
            colors[name] = (r, g, b)

        return colors

    def _compute_wave(self, now: float, speed: float, palette_name: str) -> Dict[str, Tuple[int, int, int]]:
        colors = {}
        palette = PALETTES.get(palette_name, PALETTES["rainbow"])

        for name, t_mid in STRIP_MIDPOINTS.items():
            phase = (t_mid + now * speed) % 1.0
            idx = int(phase * (len(palette) - 1))
            colors[name] = palette[idx]

        return colors

    # Below this s*v a frame carries no colour worth defending — a dim grey
    # desktop, a letterboxed black bar, a fade-out. Picking a "winner" out of
    # that noise would amplify sensor jitter into a random hue, so the mean is
    # the honest answer there.
    _VIBRANCE_FLOOR = 0.02

    def _dominant_color(self, colors: Dict[str, Tuple[int, int, int]]) -> Tuple[int, int, int]:
        """Collapse a per-strip colour field to the one colour the PC shows.

        A raw RGB mean is the wrong reduction for this. Averaging distinct hues
        moves the result towards the middle of the RGB cube, which is grey: a
        red left edge and a cyan right edge average to a muddy neutral, and the
        PC ends up dim white-blue through scenes the room renders as vivid.
        The information destroyed is exactly the thing we wanted to show.

        So pick instead of blend. Convert to HSV, score each candidate by
        vibrance (s * v — colourful AND bright, so a dark navy loses to a
        moderate orange and a pale sky loses to a deep teal), and hand the PC
        the winner outright. One zone can only be one colour; it should be the
        most characteristic one in the frame, not the average of all of them.

        The mean survives as the fallback for frames with nothing to pick from
        (empty, black, or uniformly washed out), where it is the correct answer
        and the argmax would just be amplifying noise.
        """
        if not colors:
            return BLACK

        best: Optional[Tuple[int, int, int]] = None
        best_vibrance = 0.0

        for rgb in colors.values():
            _, s, v = rgb_to_hsv(*rgb)
            vibrance = s * v
            if vibrance > best_vibrance:
                best_vibrance = vibrance
                best = rgb

        if best is not None and best_vibrance >= self._VIBRANCE_FLOOR:
            return best

        n = len(colors)
        return (
            sum(c[0] for c in colors.values()) // n,
            sum(c[1] for c in colors.values()) // n,
            sum(c[2] for c in colors.values()) // n,
        )

    async def _broadcast_ws(
        self,
        room_colors: Dict[str, Tuple[int, int, int]],
        pc_color: Tuple[int, int, int],
    ):
        if not self.ws_clients:
            return

        # The per-strip keys are unchanged; "pc" is purely additive, so a
        # client that does not know about it simply ignores it.
        payload = json.dumps({**room_colors, "pc": pc_color})
        dead_clients = set()

        for ws in self.ws_clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead_clients.add(ws)

        for ws in dead_clients:
            self.ws_clients.discard(ws)
