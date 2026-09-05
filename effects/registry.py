"""The mode catalog: one ModeSpec per Mode, and the only place that knows
what a mode IS.

Before this file, mode metadata was scattered across three module-level
frozensets in engine.py (`_UNIFIED_MODES`, `_ROOM_SMOOTHED_MODES`,
`_COLOR_DRIVEN_MODES`), an if/elif chain in `_compute_mode`, two hand-written
`<button>` blocks in index.html, and two more constants in app.js. Adding a
mode meant editing eight places and remembering which of them silently changes
behaviour if you forget it -- `_COLOR_DRIVEN_MODES` in particular, where an
omission makes the engine paint the PC with the room's colours.

Now: add a Mode member, add a spec here. That is the whole checklist. The
engine reads its routing decisions off the spec, and `GET /api/modes` serves
this catalog to the browser so the UI cannot drift from the server's idea of
what a mode does.

The `render` callables for the source modes are thin adapters onto existing
EffectEngine methods. Those modes need engine-owned inputs (the screen frame
hold, the per-target audio DSP bundle) that no generative pattern wants, so
they stay where they are and the adapter unpacks RenderCtx for them. This file
deliberately does NOT import engine.py -- `ctx.engine` is duck-typed, which is
what keeps the import graph acyclic.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import patterns
from .modes import Mode
from .patterns import RenderCtx

RGB = Tuple[int, int, int]
Renderer = Callable[[RenderCtx], Dict[str, RGB]]

# Room smoothing classes, read by `EffectEngine._route`.
SMOOTH_NONE = "none"        # clock-driven: no cuts to smooth, passes through raw
SMOOTH_AUDIO = "audio"      # ROOM_COLOR_TAU_S / ROOM_VALUE_TAU_S
SMOOTH_SCREEN = "screen"    # SCREEN_COLOR_TAU_S / SCREEN_VALUE_TAU_S

# UI grouping, served to the browser. "source" modes are driven by external
# content and stay a button grid; the two pattern groups become a dropdown.
GROUP_SOURCE = "source"
GROUP_ROOM = "room"
GROUP_PC = "pc"

# Capture capabilities a mode can require. The strings match
# samplers.backends.base — they are declared here rather than imported so this
# module keeps depending on nothing but the effects package, which is what lets
# the pattern tests import it with no capture libraries installed at all.
CAP_AUDIO = "audio"
CAP_SCREEN = "screen"

# ── Runtime capability register ──────────────────────────────────
# What this MACHINE can actually capture, injected once during app startup by
# `web.app.lifespan` after samplers.backends has probed. Not imported and
# probed here, deliberately: importing a capture library from the mode registry
# would make `effects` depend on `samplers`, and probing at import time would
# open an audio device as a side effect of `import effects.registry`.
#
# Unset means "not probed yet", and everything reads as available — which is
# the right default for the test harness and for any caller that never boots
# the samplers at all. A capability is only ever REMOVED by a probe that ran
# and failed.
_CAPABILITIES: Dict[str, dict] = {}


def set_capabilities(caps: Dict[str, object]) -> None:
    """Record what the host can capture. Called once, during startup.

    Accepts anything with `.available` and `.reason` (a
    samplers.backends.Capability) or a plain dict with those keys, so the
    registry needs no import from the samplers package.
    """
    _CAPABILITIES.clear()
    for name, cap in (caps or {}).items():
        if isinstance(cap, dict):
            available = bool(cap.get("available", True))
            reason = str(cap.get("reason", ""))
        else:
            available = bool(getattr(cap, "available", True))
            reason = str(getattr(cap, "reason", ""))
        _CAPABILITIES[name] = {"available": available, "reason": reason}


def capability_available(name: str) -> bool:
    """Unprobed capabilities read as available — see the register's comment."""
    entry = _CAPABILITIES.get(name)
    return True if entry is None else entry["available"]


def capability_reason(name: str) -> str:
    entry = _CAPABILITIES.get(name)
    return entry["reason"] if entry else ""


def is_available(mode: Mode) -> bool:
    """Whether this mode's required capture capability exists on this host."""
    spec = SPECS.get(mode)
    if spec is None or spec.requires is None:
        return True
    return capability_available(spec.requires)


def unavailable_reason(mode: Mode) -> str:
    """Why this mode cannot run here, or "" if it can.

    This string is the tooltip on the greyed-out button, so it has to be a
    sentence a user can act on rather than an exception's repr.
    """
    spec = SPECS.get(mode)
    if spec is None or spec.requires is None or capability_available(spec.requires):
        return ""
    return capability_reason(spec.requires) or (
        "This machine has no %s capture available." % spec.requires
    )


@dataclass(frozen=True)
class ModeSpec:
    """What a mode is, how it must be routed, and what it needs from the user.

    The capability flags are not documentation -- every one of them is read by
    the engine on the hot path or by the share fast-path, and `room_ok` is
    enforced at the command layer.
    """

    mode: Mode
    label: str
    group: str
    render: Renderer

    # ── Hardware capability ──────────────────────────────────────
    # room_ok=False is a statement about physics, not taste: the BLE path
    # delivers 5-10 Hz, so a pattern whose features are shorter than 200 ms
    # cannot be represented there at all. Enforced in `_apply_command`.
    room_ok: bool = True
    pc_ok: bool = True

    # ── What the user controls ───────────────────────────────────
    # Read by `_render_key` to decide whether two targets would genuinely
    # produce the same frame, and by the UI to decide which controls to show.
    color_driven: bool = False      # reads that target's colour picker
    speed_driven: bool = False      # reads that target's speed
    palette_driven: bool = False    # reads that target's palette

    # Stochastic modes are never shared between targets: a shared render
    # advances only one target's RNG, so unlinking later would resume the
    # other from cold state. Two renders per tick is the cheaper problem.
    stochastic: bool = False

    # ── Host capability ──────────────────────────────────────────
    # Which capture capability this mode cannot run without: "audio", "screen",
    # or None for a mode that needs nothing but a clock. Distinct from
    # room_ok/pc_ok, which are about the OUTPUT hardware and are fixed at
    # import; this is about the INPUT and is only knowable at runtime, on the
    # machine the app happens to be running on.
    requires: Optional[str] = None

    # ── Room routing ─────────────────────────────────────────────
    # SMOOTH_AUDIO also means "collapse to one master colour first": a single
    # musical colour IS the mode's output, so there is no spatial field for the
    # room to lose. SMOOTH_SCREEN keeps the three-strip field and filters each
    # strip on its own accumulator.
    room_smoothing: str = SMOOTH_NONE

    hint: str = ""


# ── Source adapters ──────────────────────────────────────────────
# Named functions rather than lambdas so a traceback through the render call
# names the mode that raised.

def _render_static(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_static(ctx.target)


def _render_screen_sync(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_screen_sync(ctx.screen_frame)


def _render_audio_reactive(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_audio_reactive(ctx.dsp, ctx.audio_frame, ctx.now, ctx.dt)


def _render_music(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_music(ctx.dsp, ctx.audio_frame, ctx.now, ctx.dt)


def _render_pulse(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_pulse(ctx.target, ctx.now, ctx.speed)


def _render_wave(ctx: RenderCtx) -> Dict[str, RGB]:
    return ctx.engine._compute_wave(ctx.now, ctx.speed, ctx.palette)


_ALL: List[ModeSpec] = [
    # ══ Sources ══════════════════════════════════════════════════
    ModeSpec(
        mode=Mode.STATIC, label="Static", group=GROUP_SOURCE,
        render=_render_static,
        color_driven=True,
        hint="A flat colour, exactly as picked.",
    ),
    ModeSpec(
        mode=Mode.SCREEN_SYNC, label="Screen Sync", group=GROUP_SOURCE,
        render=_render_screen_sync,
        room_smoothing=SMOOTH_SCREEN, requires=CAP_SCREEN,
        hint="Screen edges. The room washes across a scene cut; the PC takes it raw.",
    ),
    ModeSpec(
        mode=Mode.AUDIO_REACTIVE, label="Audio Reactive", group=GROUP_SOURCE,
        render=_render_audio_reactive,
        room_smoothing=SMOOTH_AUDIO, requires=CAP_AUDIO,
        hint="One musical colour carrying every transient, low-passed for the radio.",
    ),
    ModeSpec(
        mode=Mode.MUSIC, label="Music", group=GROUP_SOURCE,
        render=_render_music,
        room_smoothing=SMOOTH_AUDIO, requires=CAP_AUDIO,
        hint="Club strobe. Winner-takes-all colour, hard decay to black.",
    ),

    # ══ Room patterns ════════════════════════════════════════════
    # Safe on both targets: band-limited far below the radio's Nyquist floor,
    # and simply gentler on the PC.
    ModeSpec(
        mode=Mode.PULSE, label="Pulse", group=GROUP_ROOM,
        render=_render_pulse,
        color_driven=True, speed_driven=True,
        hint="A Gaussian lobe travelling around the room.",
    ),
    ModeSpec(
        mode=Mode.WAVE, label="Wave", group=GROUP_ROOM,
        render=_render_wave,
        speed_driven=True, palette_driven=True,
        hint="The palette cycling around the room.",
    ),
    ModeSpec(
        mode=Mode.BREATHE, label="Breathe", group=GROUP_ROOM,
        render=patterns.breathe,
        color_driven=True, speed_driven=True,
        hint="A raised cosine on brightness. ~8 s per breath.",
    ),
    ModeSpec(
        mode=Mode.PHASE, label="Phase", group=GROUP_ROOM,
        render=patterns.phase_drift,
        color_driven=True, speed_driven=True,
        hint="Continuous hue drift, ~50 s per revolution. The walls stay analogous.",
    ),
    ModeSpec(
        mode=Mode.TIDE, label="Tide", group=GROUP_ROOM,
        render=patterns.tide,
        color_driven=True, speed_driven=True,
        hint="Two incommensurate waves. The bright region wanders and never loops.",
    ),
    ModeSpec(
        mode=Mode.AMBIENT, label="Ambient (Ember)", group=GROUP_ROOM,
        render=patterns.ember,
        color_driven=True, speed_driven=True, stochastic=True,
        hint="An Ornstein-Uhlenbeck hearth. Organic, and mathematically unable to flicker.",
    ),

    ModeSpec(
        mode=Mode.RAINBOW, label="Rainbow", group=GROUP_ROOM,
        render=patterns.rainbow,
        speed_driven=True,
        hint="The full colour wheel, whole room in unison. ~29 s per revolution.",
    ),
    ModeSpec(
        mode=Mode.SHADES, label="Shades", group=GROUP_ROOM,
        render=patterns.shades,
        color_driven=True, speed_driven=True,
        hint="Shades of the colour you picked. Hue never strays more than 14 degrees.",
    ),

    # ══ Room-exclusive patterns ══════════════════════════════════
    # pc_ok=False: the content IS the difference between the strips, and the
    # PC's single zone averages that away.
    ModeSpec(
        mode=Mode.LIGHTHOUSE, label="Lighthouse", group=GROUP_ROOM,
        render=patterns.lighthouse, pc_ok=False,
        color_driven=True, speed_driven=True,
        hint="A beam sweeping Left to Back to Right. ~11 s per lap, capped at 0.25 Hz.",
    ),

    # ══ PC-exclusive patterns ════════════════════════════════════
    # room_ok=False. Features shorter than one BLE sample.
    ModeSpec(
        mode=Mode.STROBE, label="Strobe", group=GROUP_PC,
        render=patterns.strobe, room_ok=False,
        color_driven=True, speed_driven=True,
        hint="Square wave, capped at 7.5 Hz. Photosensitivity ceiling, not a taste ceiling.",
    ),
    ModeSpec(
        mode=Mode.FLICKER, label="Cyberpunk Flicker", group=GROUP_PC,
        render=patterns.flicker, room_ok=False,
        color_driven=True, speed_driven=True, stochastic=True,
        hint="Poisson glitch bursts on a neon base. A failing tube, not TV static.",
    ),
    ModeSpec(
        mode=Mode.PLASMA, label="Plasma", group=GROUP_PC,
        render=patterns.plasma, room_ok=False,
        speed_driven=True, palette_driven=True,
        hint="Quasi-periodic sum of sines. Never exactly repeats.",
    ),
    ModeSpec(
        mode=Mode.COMET, label="Comet", group=GROUP_PC,
        render=patterns.comet, room_ok=False,
        speed_driven=True, palette_driven=True,
        hint="Decaying flashes, each a golden angle around the wheel from the last.",
    ),
    ModeSpec(
        mode=Mode.HEARTBEAT, label="Heartbeat", group=GROUP_PC,
        render=patterns.heartbeat, room_ok=False,
        color_driven=True, speed_driven=True,
        hint="A double-Gaussian lub-dub. 45 ms pulses: one BLE sample wide.",
    ),
]

SPECS: Dict[Mode, ModeSpec] = {spec.mode: spec for spec in _ALL}

# Every mode must have a spec, or `_compute_mode` would fall through to a
# KeyError on the hot path. Caught at import rather than at 30 Hz.
_missing = [m for m in Mode if m not in SPECS]
assert not _missing, "Mode(s) with no ModeSpec: %s" % _missing


def spec_for(mode: Mode) -> ModeSpec:
    return SPECS[mode]


def allows(mode: Mode, target: str) -> bool:
    """Whether this mode may be assigned to this target.

    The single authority behind the command-layer guard, the REST 400 and the
    UI's filtering, so all three necessarily agree.
    """
    spec = SPECS.get(mode)
    if spec is None:
        return False
    return spec.room_ok if target == "room" else spec.pc_ok


def catalog() -> List[dict]:
    """The catalog as JSON-ready dicts, for `GET /api/modes`.

    The browser builds both mode banks from this and filters each by
    room_ok / pc_ok, which is what lets index.html stop hard-coding two
    identical button grids and app.js stop mirroring the server's capability
    sets by hand.
    """
    return [
        {
            "mode": spec.mode.value,
            "label": spec.label,
            "group": spec.group,
            "room_ok": spec.room_ok,
            "pc_ok": spec.pc_ok,
            "color_driven": spec.color_driven,
            "speed_driven": spec.speed_driven,
            "palette_driven": spec.palette_driven,
            "hint": spec.hint,
            # Runtime, not structural. room_ok/pc_ok say what the OUTPUT can
            # represent and never change; `available` says whether this machine
            # has the INPUT this mode reads. The UI greys the button out and
            # shows `unavailable_reason` as its tooltip rather than hiding it,
            # because a missing mode with no explanation reads as a bug.
            "requires": spec.requires,
            "available": is_available(spec.mode),
            "unavailable_reason": unavailable_reason(spec.mode),
        }
        for spec in _ALL
    ]
