"""Headless verification for the generative pattern library.

Drives EffectEngine's render path directly — no radio, no OpenRGB, no event
loop — so every claim in effects/patterns.py is checked as arithmetic rather
than by eye.

    python test_patterns.py

The band-limit test is the important one. Everything else in this file is a
regression guard; that one is the reason the Room/PC split exists at all, and
it is written so that pointing it at a PC pattern MUST fail.
"""

import math
import sys
from collections import namedtuple

import numpy as np

from config import settings
from effects.engine import AppState, Command, EffectEngine
from effects.modes import Mode
from effects.registry import SPECS, allows
from effects.topology import STRIP_MIDPOINTS
from utils.color import rgb_to_hsv

TICK_HZ = 30.0
BLE_WORST_HZ = 5.0          # UNACKED_TRANSMIT_INTERVAL — the blind write path
NYQUIST = BLE_WORST_HZ / 2.0

# Introduced by this change: these MUST hold the band limit.
NEW_ROOM_PATTERNS = [Mode.BREATHE, Mode.PHASE, Mode.TIDE, Mode.AMBIENT,
                     Mode.LIGHTHOUSE, Mode.RAINBOW, Mode.SHADES]

# Room-exclusive: the PC's single zone would average the spatial content away.
ROOM_ONLY_PATTERNS = [Mode.LIGHTHOUSE]

# Pre-existing. Reported, not asserted: their traverse rates predate this work
# and retuning them would change behaviour nobody asked to change. See [1b].
LEGACY_ROOM_PATTERNS = [Mode.PULSE, Mode.WAVE]

ROOM_PATTERNS = NEW_ROOM_PATTERNS + LEGACY_ROOM_PATTERNS
PC_PATTERNS = [Mode.STROBE, Mode.FLICKER, Mode.PLASMA, Mode.COMET, Mode.HEARTBEAT]

_failures = []


def check(name, ok, detail=""):
    print("  %-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        _failures.append(name)
    return ok


# ── Stubs ────────────────────────────────────────────────────────
# The engine only touches these on the transmit path, which no test drives.

class _NullQueue:
    def get_nowait(self):
        raise Exception("empty")


class _StubBLE:
    def set_color(self, *_):
        pass


class _StubScreen:
    def __init__(self):
        self.screen_queue = _NullQueue()


def new_engine():
    return EffectEngine(_StubBLE(), _StubScreen(), _NullQueue())


def run_ticks(engine, target, seconds, tick_hz=TICK_HZ, t0=1000.0):
    """Render one target for `seconds`, returning the per-strip frames.

    Mirrors EffectEngine.run's clock exactly: a fixed dt and a monotonic-style
    `now`, so the patterns see the same thing they see in production.
    """
    dt = 1.0 / tick_hz
    frames = []
    mode = engine._mode_for(target)
    for i in range(int(seconds * tick_hz)):
        now = t0 + i * dt
        frames.append(engine._compute_mode(target, mode, None, None, now, dt))
    return frames


def value_series(frames, strip):
    return np.array([rgb_to_hsv(*f[strip])[2] for f in frames])


def hue_series(frames, strip):
    return np.array([rgb_to_hsv(*f[strip])[0] for f in frames])


def hue_deltas(h, v=None, floor=0.02):
    """Shortest-path hue steps, matching engine._hue_lerp's wrap handling.

    Steps into or out of near-black are dropped. Pure black converts back to
    hue 0 regardless of what colour it faded from, so counting those measures
    the colour space rather than the pattern -- and it is precisely why
    `_smooth_strip` HOLDS hue rather than tracking it below v = 1e-3.
    """
    d = np.abs((np.diff(h) + 0.5) % 1.0 - 0.5)
    if v is not None:
        lit = (v[:-1] > floor) & (v[1:] > floor)
        d = d[lit]
    return d if len(d) else np.zeros(1)


def low_freq_fraction(v, tick_hz=TICK_HZ, cutoff=NYQUIST):
    """Fraction of the signal's AC power below `cutoff`.

    This is the band-limit claim stated directly. A signal with essentially all
    of its energy under the radio's Nyquist floor survives decimation to 5-10 Hz
    intact; one without it aliases, and aliasing on a light strip is the glitchy
    mess the split exists to prevent.
    """
    v = v - v.mean()
    if not np.any(v):
        return 1.0
    spectrum = np.abs(np.fft.rfft(v * np.hanning(len(v)))) ** 2
    freqs = np.fft.rfftfreq(len(v), 1.0 / tick_hz)
    total = spectrum.sum()
    return float(spectrum[freqs <= cutoff].sum() / total) if total else 1.0


# ── 1. Band limit ────────────────────────────────────────────────

def test_band_limit():
    print("\n[1] Band limit — room patterns must survive decimation to %.0f Hz" % BLE_WORST_HZ)
    print("    (>= 98%% of AC power below the %.2f Hz Nyquist floor)" % NYQUIST)

    for mode in NEW_ROOM_PATTERNS:
        frac, slew, dh = measure_room(mode)
        check(mode.value, frac >= 0.98,
              "%.4f of power < %.1f Hz | max dV/sample %.3f | max dH %.4f"
              % (frac, NYQUIST, slew, dh))


def measure_room(mode):
    eng = new_engine()
    eng.state.room_mode = mode
    frames = run_ticks(eng, "room", 120.0)
    step = int(TICK_HZ / BLE_WORST_HZ)
    fracs, slews, hues = [], [], []
    for strip in STRIP_MIDPOINTS:
        v = value_series(frames, strip)
        fracs.append(low_freq_fraction(v))
        # Worst change across one 200 ms BLE sample on the blind path.
        slews.append(float(np.abs(np.diff(v[::step])).max()))
        hues.append(float(hue_deltas(hue_series(frames, strip)[::step], v[::step]).max()))
    return min(fracs), max(slews), max(hues)


def test_legacy_room_bandwidth():
    print("\n[1b] Pre-existing room modes — DIAGNOSTIC, not asserted")
    print("     Their default traverse rates predate this change.")
    for mode in LEGACY_ROOM_PATTERNS:
        frac, slew, dh = measure_room(mode)
        verdict = "within band limit" if frac >= 0.98 else "ALIASES on the radio"
        print("  --   %-46s %.4f of power < %.1f Hz | max dV/sample %.3f "
              "| max dH %.4f  -> %s"
              % (mode.value, frac, NYQUIST, slew, dh, verdict))


def test_pc_patterns_would_fail():
    print("\n[2] The same test must REJECT the PC-exclusive patterns")
    print("    (if one of these passed, room_ok=False would be arbitrary)")

    for mode in PC_PATTERNS:
        eng = new_engine()
        eng.state.pc_mode = mode
        frames = run_ticks(eng, "pc", 120.0)
        v = value_series(frames, next(iter(STRIP_MIDPOINTS)))
        frac = low_freq_fraction(v)
        check(mode.value + " rejected", frac < 0.98,
              "%.4f of power < %.1f Hz" % (frac, NYQUIST))


# ── 3. Cross-target isolation ────────────────────────────────────

def test_speed_isolation():
    print("\n[3] Two targets, one pattern, two speeds — no shared state")

    for mode in [Mode.BREATHE, Mode.TIDE, Mode.PHASE]:
        eng = new_engine()
        eng.state.room_mode = eng.state.pc_mode = mode
        eng.state.room_speed, eng.state.pc_speed = 0.3, 3.0

        dt = 1.0 / TICK_HZ
        room, pc = [], []
        for i in range(600):
            now = 1000.0 + i * dt
            room.append(eng._compute_mode("room", mode, None, None, now, dt))
            pc.append(eng._compute_mode("pc", mode, None, None, now, dt))

        # Measured in RGB, not V: PHASE holds brightness flat and moves only
        # hue, so a brightness-only metric would call it identical.
        strip = next(iter(STRIP_MIDPOINTS))
        a = np.array([f[strip] for f in room], dtype=float)
        b = np.array([f[strip] for f in pc], dtype=float)
        diff = float(np.abs(a - b).max())
        check(mode.value + " diverges", diff > 16.0, "max channel delta = %.0f/255" % diff)

    # And the fast path must know it: sharing here would paint one over the other.
    eng = new_engine()
    eng.state.room_mode = eng.state.pc_mode = Mode.BREATHE
    eng.state.room_speed, eng.state.pc_speed = 0.3, 3.0
    check("share refused on unequal speed",
          eng._render_key("room") != eng._render_key("pc"))

    eng.state.pc_speed = 0.3
    check("share allowed when every parameter agrees",
          eng._render_key("room") == eng._render_key("pc"))


def test_rng_isolation():
    print("\n[4] Stochastic patterns draw from independent streams")

    eng = new_engine()
    eng.state.room_mode = eng.state.pc_mode = Mode.AMBIENT
    dt = 1.0 / TICK_HZ
    room, pc = [], []
    for i in range(900):
        now = 1000.0 + i * dt
        room.append(eng._compute_mode("room", Mode.AMBIENT, None, None, now, dt))
        pc.append(eng._compute_mode("pc", Mode.AMBIENT, None, None, now, dt))

    strip = next(iter(STRIP_MIDPOINTS))
    a, b = value_series(room, strip), value_series(pc, strip)
    corr = float(np.corrcoef(a, b)[0, 1])
    check("AMBIENT streams uncorrelated", abs(corr) < 0.3, "r = %+.3f" % corr)
    check("stochastic modes never share",
          eng._render_key("room") != eng._render_key("pc"))

    # The OU process must reproduce its configured stationary spread, which is
    # what makes it frame-rate independent.
    cfg = settings.patterns.ember
    sd = float(np.std(a))
    check("OU stationary std ~ sigma", abs(sd - cfg.sigma) < 0.045,
          "measured %.4f vs configured %.4f" % (sd, cfg.sigma))


# ── 5. Hardware guard ────────────────────────────────────────────

def test_hardware_guard():
    print("\n[5] The room refuses what it cannot represent")

    for mode in PC_PATTERNS:
        eng = new_engine()
        before = eng.state.room_mode
        eng._apply_command(Command(type="SET_ROOM_MODE", payload={"mode": mode.value}))
        check(mode.value + " refused on room", eng.state.room_mode == before,
              "room stayed on %s" % eng.state.room_mode.value)

    # target "both" must still apply the half that is legal.
    eng = new_engine()
    eng._apply_command(Command(type="SET_MODE", payload={"mode": "PLASMA", "target": "both"}))
    check("both -> PC applies, room untouched",
          eng.state.pc_mode == Mode.PLASMA and eng.state.room_mode == Mode.STATIC,
          "room=%s pc=%s" % (eng.state.room_mode.value, eng.state.pc_mode.value))

    # And every room pattern must actually be accepted.
    eng = new_engine()
    ok = True
    for mode in ROOM_PATTERNS:
        eng._apply_command(Command(type="SET_ROOM_MODE", payload={"mode": mode.value}))
        ok = ok and eng.state.room_mode == mode
    check("all room patterns accepted", ok)

    check("allows() agrees with the catalog",
          all(allows(m, "pc") for m in Mode if m not in ROOM_ONLY_PATTERNS) and
          not any(allows(m, "room") for m in PC_PATTERNS) and
          not any(allows(m, "pc") for m in ROOM_ONLY_PATTERNS))


# ── 6. Frame-rate independence ───────────────────────────────────

def test_frame_rate_independence():
    print("\n[6] Same wall-clock time, same colour, at 30 Hz and 60 Hz")

    # HEARTBEAT's lub is 45 ms wide -- about 1.4 frames at 30 Hz. Its PHASE is
    # frame-rate independent (the accumulator guarantees that), but which point
    # of a sub-frame Gaussian a tick lands on cannot be, so it gets a threshold
    # that reflects sampling a narrow peak rather than a drifting clock.
    tolerance = {Mode.HEARTBEAT: 0.25}

    for mode in [Mode.BREATHE, Mode.PHASE, Mode.TIDE, Mode.PULSE, Mode.WAVE, Mode.HEARTBEAT]:
        target = "room" if allows(mode, "room") else "pc"
        out = []
        for hz in (30.0, 60.0):
            eng = new_engine()
            setattr(eng.state, target + "_mode", mode)
            frames = run_ticks(eng, target, 20.0, tick_hz=hz)
            # Sample both runs at the same 10 wall-clock instants.
            step = int(hz * 2.0)
            out.append(np.array([rgb_to_hsv(*frames[i][next(iter(STRIP_MIDPOINTS))])[2]
                                 for i in range(step, len(frames), step)]))
        n = min(len(out[0]), len(out[1]))
        drift = float(np.abs(out[0][:n] - out[1][:n]).max())
        limit = tolerance.get(mode, 0.03)
        check(mode.value, drift < limit,
              "max |30Hz - 60Hz| = %.4f (limit %.2f)" % (drift, limit))


# ── 7. Regressions on the pre-existing modes ─────────────────────

def test_legacy_regressions():
    print("\n[7] The seven original modes still behave exactly as before")

    eng = new_engine()
    eng.state.room_mode = eng.state.pc_mode = Mode.STATIC
    eng.state.room_static_color = (255, 0, 0)
    eng.state.pc_static_color = (0, 0, 255)
    check("STATIC on both, two colours -> no share",
          eng._render_key("room") != eng._render_key("pc"))

    room = eng._compute_mode("room", Mode.STATIC, None, None, 1000.0, 0.033)
    pc = eng._compute_mode("pc", Mode.STATIC, None, None, 1000.0, 0.033)
    strip = next(iter(STRIP_MIDPOINTS))
    check("each picker reaches its own target",
          room[strip] == (255, 0, 0) and pc[strip] == (0, 0, 255),
          "room=%s pc=%s" % (room[strip], pc[strip]))

    from effects.registry import SMOOTH_AUDIO, SMOOTH_SCREEN, SMOOTH_NONE
    check("audio modes still collapse + smooth",
          SPECS[Mode.AUDIO_REACTIVE].room_smoothing == SMOOTH_AUDIO and
          SPECS[Mode.MUSIC].room_smoothing == SMOOTH_AUDIO)
    check("SCREEN_SYNC still keeps its spatial field",
          SPECS[Mode.SCREEN_SYNC].room_smoothing == SMOOTH_SCREEN)
    check("patterns pass through unsmoothed",
          all(SPECS[m].room_smoothing == SMOOTH_NONE for m in ROOM_PATTERNS))

    # WAVE and PULSE now read per-target parameters rather than a global.
    eng = new_engine()
    eng.state.room_mode = eng.state.pc_mode = Mode.WAVE
    eng.state.room_palette, eng.state.pc_palette = "ocean", "sunset"
    room = eng._compute_mode("room", Mode.WAVE, None, None, 1000.0, 0.033)
    pc = eng._compute_mode("pc", Mode.WAVE, None, None, 1000.0, 0.033)
    check("WAVE reads its own target's palette", room[strip] != pc[strip],
          "ocean=%s sunset=%s" % (room[strip], pc[strip]))

    # The target grammar: absent still means both.
    eng = new_engine()
    eng._apply_command(Command(type="SET_SPEED", payload={"value": 2.5}))
    check("SET_SPEED without a target still means both",
          eng.state.room_speed == 2.5 and eng.state.pc_speed == 2.5)
    eng._apply_command(Command(type="SET_PALETTE", payload={"name": "ocean", "target": "pc"}))
    check("SET_PALETTE honours an explicit target",
          eng.state.room_palette == "rainbow" and eng.state.pc_palette == "ocean")

    # A mode change must not leave the other target's pattern state behind.
    eng = new_engine()
    eng.state.room_mode = Mode.BREATHE
    eng._compute_mode("room", Mode.BREATHE, None, None, 1000.0, 0.033)
    eng._apply_command(Command(type="SET_ROOM_MODE", payload={"mode": "TIDE"}))
    check("_reset_target clears pattern state", not eng._pattern["room"].phases)


def test_pattern_output_shape():
    print("\n[8] Every pattern returns a well-formed frame")

    ok_keys = ok_range = True
    for mode in ROOM_PATTERNS + PC_PATTERNS:
        target = "room" if allows(mode, "room") else "pc"
        eng = new_engine()
        setattr(eng.state, target + "_mode", mode)
        for f in run_ticks(eng, target, 5.0):
            if set(f) != set(STRIP_MIDPOINTS):
                ok_keys = False
            for c in f.values():
                if not all(isinstance(x, int) and 0 <= x <= 255 for x in c):
                    ok_range = False
    check("all three strips, every tick", ok_keys)
    check("channels are ints in 0-255", ok_range)

    # PC patterns must be unified: _dominant_color averages, so a spatial PC
    # pattern would arrive as mud rather than as the pattern.
    unified = True
    for mode in PC_PATTERNS:
        eng = new_engine()
        eng.state.pc_mode = mode
        for f in run_ticks(eng, "pc", 5.0):
            if len(set(f.values())) != 1:
                unified = False
    check("PC patterns are a single zone", unified)


def test_routing():
    print("\n[9] _route sends each target's own frame to its own hardware")

    # Every mode must survive a real route, on both targets, through whichever
    # smoothing branch its spec selects. This is the path the spec lookup
    # replaced three frozenset membership tests on.
    ok = True
    for mode in Mode:
        eng = new_engine()
        target = "room" if allows(mode, "room") else "pc"
        setattr(eng.state, target + "_mode", mode)
        dt = 1.0 / TICK_HZ
        for i in range(20):
            now = 1000.0 + i * dt
            src = eng._compute_mode(target, mode, None, None, now, dt)
            room, pc = eng._route(src, eng.state.room_mode, src, eng.state.pc_mode, dt)
            if set(room) != set(STRIP_MIDPOINTS) or len(pc) != 3:
                ok = False
    check("every mode routes on both targets", ok)

    # Brightness is applied in _route, once, after the room filter.
    eng = new_engine()
    eng.state.room_mode = eng.state.pc_mode = Mode.STATIC
    eng.state.room_static_color = eng.state.pc_static_color = (200, 100, 50)
    eng.state.brightness = 0.5
    src = eng._compute_mode("room", Mode.STATIC, None, None, 1000.0, 0.033)
    room, pc = eng._route(src, Mode.STATIC, src, Mode.STATIC, 0.033)
    check("brightness scales both paths", pc == (100, 50, 25),
          "pc=%s room=%s" % (pc, room[next(iter(STRIP_MIDPOINTS))]))

    # ...and each target scales by its OWN brightness once they are split.
    eng.state.room_brightness = 0.25
    eng.state.pc_brightness = 1.0
    room, pc = eng._route(src, Mode.STATIC, src, Mode.STATIC, 0.033)
    check("brightness is per-target",
          pc == (200, 100, 50) and room[next(iter(STRIP_MIDPOINTS))] == (50, 25, 12),
          "pc=%s room=%s" % (pc, room[next(iter(STRIP_MIDPOINTS))]))

    # SET_BRIGHTNESS follows the same target grammar as SET_SPEED.
    eng._apply_command(Command(type="SET_BRIGHTNESS", payload={"value": 0.4, "target": "pc"}))
    check("SET_BRIGHTNESS honours an explicit target",
          eng.state.pc_brightness == 0.4 and eng.state.room_brightness == 0.25)
    eng._apply_command(Command(type="SET_BRIGHTNESS", payload={"value": 0.9}))
    check("SET_BRIGHTNESS without a target still means both",
          eng.state.pc_brightness == 0.9 and eng.state.room_brightness == 0.9)
    eng.state.brightness = 0.5

    # A disabled target goes to exact black at any slider position.
    eng.state.sync_room = eng.state.sync_pc = False
    room, pc = eng._route(src, Mode.STATIC, src, Mode.STATIC, 0.033)
    check("disabled targets are exactly black",
          pc == (0, 0, 0) and all(c == (0, 0, 0) for c in room.values()))

    # The room low-pass must still engage for the audio and screen classes and
    # stay out of the way for the clock-driven patterns.
    eng = new_engine()
    eng.state.room_mode = Mode.BREATHE
    src = eng._compute_mode("room", Mode.BREATHE, None, None, 1000.0, 0.033)
    eng._route(src, Mode.BREATHE, src, Mode.STATIC, 0.033)
    check("patterns leave the room filter disarmed", not eng._room_hsv)

    eng.state.room_mode = Mode.MUSIC
    src = eng._compute_mode("room", Mode.MUSIC, None, None, 1000.0, 0.033)
    eng._route(src, Mode.MUSIC, src, Mode.STATIC, 0.033)
    check("audio modes still arm the room filter",
          set(eng._room_hsv) == set(STRIP_MIDPOINTS))


if __name__ == "__main__":
    print("RoomSync pattern verification  (tick %.0f Hz, worst-case radio %.0f Hz)"
          % (TICK_HZ, BLE_WORST_HZ))
    test_band_limit()
    test_legacy_room_bandwidth()
    test_pc_patterns_would_fail()
    test_speed_isolation()
    test_rng_isolation()
    test_hardware_guard()
    test_frame_rate_independence()
    test_legacy_regressions()
    test_pattern_output_shape()
    test_routing()

    print("\n" + "=" * 62)
    if _failures:
        print("FAILED (%d): %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("All checks passed.")
