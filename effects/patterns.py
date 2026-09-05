"""Standalone generative animation patterns.

Every function here is a pure function of (clock, that target's parameters,
that target's mutable state). None of them touch the engine, the samplers or
the hardware, so they are testable in isolation and two targets can run the
same pattern at different speeds without interfering.

    render(ctx: RenderCtx) -> Dict[strip_name, (r, g, b)]

Brightness is NOT applied here. `EffectEngine._route` applies it once, after
the room low-pass, so dragging the slider is instant on both paths -- see the
docstring there. Patterns return the mode's own colour at full scale.


THE ONE CONSTRAINT THAT ORGANISES THIS FILE
-------------------------------------------
The engine renders at 30 Hz. The PC gets all of it. The room does not: the BLE
worker decimates to 10 Hz (`TRANSMIT_INTERVAL`) and to 5 Hz on the blind
write-without-response path (`UNACKED_TRANSMIT_INTERVAL`), overwriting rather
than queueing. So a room pattern is a 30 Hz signal sampled at 5-10 Hz.

Decimating a band-limited signal is lossless. Decimating anything else aliases,
and aliasing on a light strip is exactly the glitchy mess we are avoiding.

    Room patterns   fundamental <= 0.5 Hz, band-limited under a 2.5 Hz Nyquist
                    floor. Rule of thumb: <= 0.05 change in V and <= 0.02 in
                    hue per 200 ms sample.
    PC patterns     features down to ~33 ms, duty cycles up to ~7.5 Hz. These
                    are refused on the room by the registry, not by convention:
                    at 5-10 Hz they are not merely worse, they are not
                    representable.

The PC is also a SINGLE logical zone -- `OpenRGBBridge` calls
`client.set_color()` once, and `_route` collapses a per-strip frame through
`_dominant_color`, which is a plain mean. So PC patterns are purely temporal
and return one colour for all three keys; a spatial PC pattern would average to
mud. The room has three addressable zones at STRIP_MIDPOINTS, and the room
patterns use them.


PHASE ACCUMULATORS, NOT WALL CLOCK
----------------------------------
Everything periodic advances a phase accumulator (`PatternState.tick`) rather
than evaluating `now * freq`. Two reasons, both of which show up immediately on
screen otherwise:

  * Changing `speed` mid-pattern would teleport the phase. Accumulating keeps
    the waveform continuous through a slider drag.
  * At 30 Hz a 7.5 Hz strobe is four frames per period; sampling `now % T`
    gives a duty cycle that visibly jitters frame to frame.

Rates are in cycles per second and every update is scaled by the measured `dt`,
so all of this is frame-rate independent -- the same property `_smooth_strip`
and `_follow` already have, obtained the same way.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from utils.color import hsv_to_rgb, rgb_to_hsv, clamp01
from .topology import STRIP_MIDPOINTS
from .palettes import PALETTES

RGB = Tuple[int, int, int]
BLACK: RGB = (0, 0, 0)
TAU = 2.0 * math.pi


@dataclass
class PatternState:
    """One target's mutable pattern scratch space.

    One bundle per target, exactly like `_AudioDSP`, and for exactly the same
    reason: phase accumulators and RNG streams are per-target state, and
    sharing them between two outputs running the same pattern at different
    speeds means neither output is the pattern it claims to be.

    The RNG is a private `random.Random` rather than the module-level
    functions, so the room's Ember and the PC's Flicker draw from independent
    streams and neither can be perturbed by anything else in the process.
    """

    phases: Dict[str, float] = field(default_factory=dict)   # cycles, [0, 1)
    ou: Dict[str, float] = field(default_factory=dict)       # EMBER, per strip
    hue: float = 0.0                                         # COMET incumbent
    last_fire: float = 0.0                                   # COMET, monotonic
    burst_until: float = 0.0                                 # FLICKER
    burst_resample_at: float = 0.0
    burst_v: float = 0.0
    rng: random.Random = field(default_factory=random.Random)

    def tick(self, key: str, rate_hz: float, dt: float) -> Tuple[float, bool]:
        """Advance one accumulator. Returns (phase in cycles, wrapped this tick).

        The wrap flag is the impulse trigger: COMET fires on it. A rate high
        enough to wrap more than once per tick still reports a single fire,
        which is correct -- the hardware cannot show two flashes in one frame.
        """
        p = self.phases.get(key, 0.0) + rate_hz * dt
        wrapped = p >= 1.0
        self.phases[key] = p % 1.0
        return self.phases[key], wrapped

    def advance(self, key: str, rate_hz: float, dt: float) -> float:
        return self.tick(key, rate_hz, dt)[0]

    def reset(self):
        """Wipe everything the previous pattern owned.

        Called from `EffectEngine._reset_target` when THIS target's mode
        changes, so a new pattern starts from its own first frame instead of
        inheriting a half-wound phase or a stale OU position. The RNG object
        survives -- reseeding it would be pure cost with no behavioural benefit.
        """
        self.phases.clear()
        self.ou.clear()
        self.hue = 0.0
        self.last_fire = 0.0
        self.burst_until = 0.0
        self.burst_resample_at = 0.0
        self.burst_v = 0.0


@dataclass(frozen=True)
class RenderCtx:
    """Everything a mode is allowed to see, resolved for ONE target.

    The generative patterns use the top block only. The source modes
    (Screen Sync, the two audio modes) additionally need engine-owned inputs,
    which is what the bottom block carries -- see the adapters in registry.py.
    """

    target: str                 # "room" | "pc"
    now: float                  # time.monotonic() at the top of this tick
    dt: float                   # measured, clamped to [1e-3, 0.25] by the engine
    base_color: RGB             # this target's colour picker
    speed: float                # this target's speed
    palette: str                # this target's palette
    state: PatternState         # this target's pattern scratch space
    cfg: Any                    # settings.patterns

    engine: Any = None
    screen_frame: Any = None
    audio_frame: Any = None
    dsp: Any = None


def unified(color: RGB) -> Dict[str, RGB]:
    """One colour on every strip. What every PC pattern returns."""
    return {name: color for name in STRIP_MIDPOINTS}


def _base_hs(color: RGB) -> Tuple[float, float]:
    """The picker's hue and saturation, discarding its brightness.

    Patterns that drive V themselves take only the chroma from the wheel, so
    picking a dim colour does not silently halve the pattern's range.
    """
    h, s, _ = rgb_to_hsv(*color)
    return h, s


def _palette_at(name: str, u: float) -> RGB:
    """Sample a palette at u in [0, 1]. Reuses effects/palettes.py verbatim."""
    palette = PALETTES.get(name) or PALETTES["rainbow"]
    idx = int(clamp01(u) * (len(palette) - 1))
    return palette[idx]


# =========================================================================
#  ROOM PATTERNS
#
#  Slow, C1-continuous, band-limited far below the radio's Nyquist floor.
#  All four are safe on the PC too (they are simply gentler there), so the
#  registry marks them room_ok AND pc_ok.
# =========================================================================

def breathe(ctx: RenderCtx) -> Dict[str, RGB]:
    """R1 BREATHE - a raised cosine on the brightness channel.

        v(t) = v_min + (v_max - v_min) * (0.5 - 0.5*cos(2*pi*phase)) ** gamma

    Raised cosine rather than a raw sine so the cycle starts at its MINIMUM:
    no discontinuity at phase 0, so switching the mode on does not pop.

    At the default 0.12 Hz the peak rate of change is pi*f*(v_max - v_min) =
    0.32 per second, i.e. 0.064 per 200 ms sample. Comfortably band-limited.

    `strip_offset` lags each strip by a fraction of a cycle along the room's 1D
    axis, so the room inflates rather than blinking in unison. At 0.15 cycles
    of an 8 s breath that is a 1.25 s spread -- slow enough to survive
    decimation.
    """
    cfg = ctx.cfg.breathe
    p = ctx.state.advance("breathe", cfg.freq_hz * ctx.speed, ctx.dt)
    h, s = _base_hs(ctx.base_color)
    span = cfg.v_max - cfg.v_min

    out: Dict[str, RGB] = {}
    for name, t_mid in STRIP_MIDPOINTS.items():
        ph = (p + cfg.strip_offset * t_mid) % 1.0
        x = 0.5 - 0.5 * math.cos(TAU * ph)          # [0, 1], starts at 0
        v = cfg.v_min + span * (x ** cfg.gamma)
        out[name] = hsv_to_rgb(h, s, clamp01(v))
    return out


def phase_drift(ctx: RenderCtx) -> Dict[str, RGB]:
    """R2 PHASE - continuous rotation through the colour wheel.

        h_i = (h0 + phase + spread * t_mid_i) mod 1

    The elegant counterpart to WAVE, which indexes a 256-entry discrete palette
    and moves fast. This is continuous, starts from the picker's hue, and uses a
    deliberately tiny spread so the three walls are always ANALOGOUS colours
    rather than a rainbow clash.

    At 0.02 Hz a 200 ms sample advances the hue by 0.004 of the wheel -- about
    1.4 degrees. There is no step to see.
    """
    cfg = ctx.cfg.phase
    p = ctx.state.advance("phase", cfg.freq_hz * ctx.speed, ctx.dt)
    h0, _ = _base_hs(ctx.base_color)

    return {
        name: hsv_to_rgb((h0 + p + cfg.spread * t_mid) % 1.0, cfg.sat, cfg.val)
        for name, t_mid in STRIP_MIDPOINTS.items()
    }


def tide(ctx: RenderCtx) -> Dict[str, RGB]:
    """R3 TIDE - two incommensurate travelling waves, summed.

        v_i = v_mid + amp*sin(2*pi*(p1 + t_i)) + amp*sin(2*pi*(p2 - 1.7*t_i))

    freq_a / freq_b is near the golden ratio, so the beat envelope never closes:
    the bright region wanders around the room continuously and the three-strip
    state does not repeat on any human timescale. No randomness involved.

    Peak rate of change is 2*pi*amp*(f_a + f_b) = 0.18 per second, or 0.035 per
    200 ms sample.
    """
    cfg = ctx.cfg.tide
    st = ctx.state
    p1 = st.advance("tide_a", cfg.freq_a_hz * ctx.speed, ctx.dt)
    p2 = st.advance("tide_b", cfg.freq_b_hz * ctx.speed, ctx.dt)
    h0, s0 = _base_hs(ctx.base_color)

    out: Dict[str, RGB] = {}
    for name, t_mid in STRIP_MIDPOINTS.items():
        a = math.sin(TAU * (p1 + t_mid))
        b = math.sin(TAU * (p2 - cfg.counter_rate * t_mid))
        v = cfg.v_mid + cfg.amp * (a + b)
        h = (h0 + cfg.hue_swing * b) % 1.0
        out[name] = hsv_to_rgb(h, s0, clamp01(v))
    return out


def ember(ctx: RenderCtx) -> Dict[str, RGB]:
    """R4 AMBIENT - an Ornstein-Uhlenbeck hearth, one walk per strip.

        x' = mu + (x - mu)*a + N(0, sigma*sqrt(1 - a^2)),   a = exp(-dt/tau)

    Why this and not plain noise: an OU process is a first-order low-pass
    applied to white noise. Its spectrum is Lorentzian with a corner at
    1/(2*pi*tau) -- 0.04 Hz at tau = 4 s. Choosing tau therefore GUARANTEES the
    output is band-limited far below the radio's 2.5 Hz Nyquist floor, with no
    additional filtering. The result is organic but physically cannot flicker.

    `speed` shortens tau, but only down to `tau_floor_s`, which holds the corner
    at or below 0.2 Hz however far the slider is dragged. The band-limit is a
    property of the pattern, not of how it happens to be configured.

    This is the EXACT discretisation, not Euler-Maruyama: it is unconditionally
    stable at any dt and reproduces the stationary std exactly, which is what
    makes the pattern look identical at tick_hz 30 and 60.

    Deliberately the same mathematical family as FLICKER with the time constant
    at the opposite extreme. tau -> 4 s is a hearth; tau -> 0 is a failing neon
    tube. One idea, two hardware realities.
    """
    cfg = ctx.cfg.ember
    st = ctx.state
    rng = st.rng

    tau = max(cfg.tau_s / max(ctx.speed, 0.1), cfg.tau_floor_s)
    a = math.exp(-ctx.dt / tau)
    step_sd = cfg.sigma * math.sqrt(max(0.0, 1.0 - a * a))
    h0, s0 = _base_hs(ctx.base_color)
    inv_sigma = 1.0 / max(cfg.sigma, 1e-6)

    out: Dict[str, RGB] = {}
    for name in STRIP_MIDPOINTS:
        x = st.ou.get(name)
        if x is None:
            # Seed from the stationary distribution, so the room opens already
            # warm instead of every strip ramping up from the mean together.
            x = cfg.mu + rng.gauss(0.0, cfg.sigma)
        x = cfg.mu + (x - cfg.mu) * a + rng.gauss(0.0, step_sd)
        st.ou[name] = x

        h = (h0 + cfg.hue_sag * (x - cfg.mu) * inv_sigma) % 1.0
        out[name] = hsv_to_rgb(h, s0, clamp01(x))
    return out


def lighthouse(ctx: RenderCtx) -> Dict[str, RGB]:
    """R5 LIGHTHOUSE - a rotating beam over the room's three spatial pixels.

        d_i = circular_distance(phase, t_mid_i)      on the [0, 1) room loop
        v_i = v_floor + (v_beam - v_floor) * exp(-d_i^2 / (2*sigma^2))

    The room is a closed 1D loop (STRIP_MIDPOINTS runs counter-clockwise from
    the back wall), so "circular" is load-bearing: the distance wraps, and the
    beam leaves the right wall straight back onto the back wall with no seam.

    Why a Gaussian and not a hard sector: with only three pixels, a sector test
    is a three-state switch and every transition is a step edge -- infinite
    bandwidth, which decimation turns into a stutter that arrives at a
    different wall each lap. A Gaussian wider than the 1/3 midpoint spacing
    means two strips are always partly lit and the crossfade IS the motion.

    Band limit: the ceiling of 0.25 Hz puts a full transit of the lobe at
    2*sigma/f = 1.36 s, ~7 samples even on the 5 Hz blind-write path. Peak dV
    is (v_beam - v_floor)*f/(sigma*sqrt(e)) = 0.79 per second at the ceiling,
    or 0.16 per 200 ms sample -- and 0.057 at the 0.09 Hz default. Room-only in
    spirit but harmless on the PC; the registry marks it room-exclusive because
    `_dominant_color` would average the beam and the floor into one flat
    colour, which is not this pattern.
    """
    cfg = ctx.cfg.lighthouse
    f = min(cfg.freq_hz * ctx.speed, cfg.max_hz)
    p = ctx.state.advance("lighthouse", f, ctx.dt)
    h0, s0 = _base_hs(ctx.base_color)
    span = cfg.v_beam - cfg.v_floor
    denom = 2.0 * cfg.sigma * cfg.sigma

    out: Dict[str, RGB] = {}
    for name, t_mid in STRIP_MIDPOINTS.items():
        d = abs(p - t_mid) % 1.0
        d = min(d, 1.0 - d)                          # wrap: the room is a loop
        lobe = math.exp(-(d * d) / denom)            # [0, 1]
        v = cfg.v_floor + span * lobe
        h = (h0 + cfg.hue_lead * lobe) % 1.0
        out[name] = hsv_to_rgb(h, s0, clamp01(v))
    return out


def rainbow(ctx: RenderCtx) -> Dict[str, RGB]:
    """R6 RAINBOW - the classic full-spectrum cycle, whole-room unison.

        h_i = (phase + spread * t_mid_i) mod 1,   at fixed S and V

    Mathematically PHASE's sibling, and deliberately kept as its own mode
    rather than a preset of it: PHASE starts from the picker's hue and holds a
    spread so the walls stay analogous, whereas RAINBOW ignores the picker
    entirely, runs full saturation, and defaults spread to 0.0. That last one
    is the whole point on this hardware -- the three strips hold one hue, so
    the room reads as a single continuous strip walking the wheel rather than
    as three walls in different colours.

    At 0.035 Hz a 200 ms sample steps the hue by 0.007, ~2.5 degrees. The V
    channel is constant, so the only thing decimation can touch is that hue
    ramp, and it is two orders of magnitude inside the budget.
    """
    cfg = ctx.cfg.rainbow
    p = ctx.state.advance("rainbow", cfg.freq_hz * ctx.speed, ctx.dt)

    return {
        name: hsv_to_rgb((p + cfg.spread * t_mid) % 1.0, cfg.sat, cfg.val)
        for name, t_mid in STRIP_MIDPOINTS.items()
    }


def shades(ctx: RenderCtx) -> Dict[str, RGB]:
    """R7 SHADES - a monochromatic drift inside the picked colour's family.

        h = h0 + hue_swing * sin(2*pi*p_h)
        s = lerp(sat_min, sat_max, 0.5 - 0.5*cos(2*pi*p_s))
        v = lerp(val_min, val_max, 0.5 - 0.5*cos(2*pi*p_v))

    The hue comes from the target's own colour picker and NEVER leaves a
    +/-0.04 window around it, so the mode is a colour rotator within one
    family, not a second rainbow: pick blue and you get midnight through icy,
    pick amber and you get ember through candlelight.

    S and V get raised cosines (starting at their minima, so enabling the mode
    does not pop) while the hue gets a plain sine centred on h0 -- the picked
    colour should sit in the MIDDLE of the drift, not at one end of it.

    The three rates are mutually irrational, so the (h, s, v) trajectory is
    dense on a 3-torus and never retraces: the gradient keeps breathing instead
    of settling into a visible loop. Same trick as TIDE and PLASMA, at TIDE's
    end of the speed range.

    Peak rates at speed 1.0: 0.13 per second in V, 0.078 in S, 0.008 in hue.
    Per 200 ms sample that is 0.026 in V and 0.0016 in hue, both inside the
    file's <=0.05 V / <=0.02 hue rule of thumb.
    """
    cfg = ctx.cfg.shades
    st = ctx.state
    ph = st.advance("shades_h", cfg.hue_rate_hz * ctx.speed, ctx.dt)
    ps = st.advance("shades_s", cfg.sat_rate_hz * ctx.speed, ctx.dt)
    pv = st.advance("shades_v", cfg.val_rate_hz * ctx.speed, ctx.dt)
    h0, _ = _base_hs(ctx.base_color)
    s_span = cfg.sat_max - cfg.sat_min
    v_span = cfg.val_max - cfg.val_min

    out: Dict[str, RGB] = {}
    for name, t_mid in STRIP_MIDPOINTS.items():
        lag = cfg.strip_offset * t_mid
        h = (h0 + cfg.hue_swing * math.sin(TAU * ((ph + lag) % 1.0))) % 1.0
        s = cfg.sat_min + s_span * (0.5 - 0.5 * math.cos(TAU * ((ps + lag) % 1.0)))
        v = cfg.val_min + v_span * (0.5 - 0.5 * math.cos(TAU * ((pv + lag) % 1.0)))
        out[name] = hsv_to_rgb(h, clamp01(s), clamp01(v))
    return out


# =========================================================================
#  PC-EXCLUSIVE PATTERNS
#
#  Unified single colours (the PC is one zone) with features below the room's
#  Nyquist floor. registry.py marks these room_ok=False and the engine refuses
#  to assign them to the BLE target.
# =========================================================================

def strobe(ctx: RenderCtx) -> Dict[str, RGB]:
    """P1 STROBE - square wave with a duty cycle.

    Driven by the accumulator rather than `now % T` because at 30 Hz a 7.5 Hz
    strobe is only four frames per period, and sampling wall clock gives a duty
    ratio that visibly jitters. max_hz is where the duty stops being
    representable at all -- and it doubles as the photosensitivity ceiling.
    """
    cfg = ctx.cfg.strobe
    f = min(max(cfg.freq_hz * ctx.speed, cfg.min_hz), cfg.max_hz)
    p = ctx.state.advance("strobe", f, ctx.dt)
    return unified(ctx.base_color if p < cfg.duty else BLACK)


def flicker(ctx: RenderCtx) -> Dict[str, RGB]:
    """P2 FLICKER - Poisson-arrival glitch bursts on a stable neon base.

    Two choices make this read as broken hardware rather than TV static:

      * POISSON ARRIVALS, not a per-frame coin flip. P(burst in dt) =
        1 - exp(-lambda*dt). A coin flip every frame is memoryless hash at
        30 Hz; Poisson gives long clean stretches punctuated by clustered
        bursts, which is what a failing tube actually does.
      * BETA(0.4, 0.4) IS U-SHAPED. It concentrates mass near 0 and near 1 and
        almost never returns a middle value, so a burst is a sequence of hard
        on/off/on steps rather than a mushy fade.

    Both properties die on the radio: a 40 ms burst is 0.2-0.4 of a single BLE
    sample, so the strips would either miss it entirely or catch one frame of it
    at random.
    """
    cfg = ctx.cfg.flicker
    st = ctx.state
    rng = st.rng
    now = ctx.now
    h0, s0 = _base_hs(ctx.base_color)

    if now >= st.burst_until:
        lam = cfg.rate_hz * ctx.speed
        if rng.random() < 1.0 - math.exp(-lam * ctx.dt):
            st.burst_until = now + rng.uniform(cfg.burst_min_s, cfg.burst_max_s)
            st.burst_resample_at = 0.0          # force a sample on this tick

    if now < st.burst_until:
        if now >= st.burst_resample_at:
            st.burst_v = rng.betavariate(cfg.beta_a, cfg.beta_b)
            st.burst_resample_at = now + cfg.resample_s
        v = st.burst_v
        h = (h0 + rng.uniform(-cfg.hue_jitter, cfg.hue_jitter)) % 1.0
    else:
        v = cfg.base_v
        h = h0

    return unified(hsv_to_rgb(h, s0, clamp01(v)))


def plasma(ctx: RenderCtx) -> Dict[str, RGB]:
    """P3 PLASMA - the demoscene sum-of-sines, evaluated at a single point.

        p = sin(a) + sin(b + sin(c)) + sin(d)*cos(e)          p in [-3, 3]

    The PC is one zone, so the classic 2D field is sampled along a trajectory
    through it rather than rasterised. The five rates are mutually irrational,
    so that trajectory is dense on a 5-torus and never exactly repeats: a
    genuinely non-looping liquid colour field.

    This is the pattern that most directly justifies 30 Hz -- but only because
    of how fast the rates are set, not because sums of sines are inherently
    broadband. The fastest term runs at 2.92 Hz and the product term throws
    sidebands out to 4.1 Hz, so roughly a third of the signal's power sits above
    the radio's 2.5 Hz Nyquist floor. On the strips that third would fold back
    down as beat frequencies that are in the signal nowhere.

    Reads the palette rather than raw hue, so Sunset and Ocean give the same
    motion inside a restricted range.
    """
    cfg = ctx.cfg.plasma
    st = ctx.state
    r = cfg.rates_hz
    ph = [st.advance("plasma%d" % i, r[i] * ctx.speed, ctx.dt) for i in range(5)]

    p = (math.sin(TAU * ph[0])
         + math.sin(TAU * ph[1] + math.sin(TAU * ph[2]))
         + math.sin(TAU * ph[3]) * math.cos(TAU * ph[4]))

    h, _ = _base_hs(_palette_at(ctx.palette, (p + 3.0) / 6.0))
    v = cfg.v_mid + cfg.v_amp * math.sin(TAU * ph[0] + p)
    return unified(hsv_to_rgb(h, cfg.sat, clamp01(v)))


def comet(ctx: RenderCtx) -> Dict[str, RGB]:
    """P4 COMET - an impulse train with golden-angle hue stepping.

    Each flash snaps V to 1.0 and advances the hue by 1/phi^2 = 0.381966 of the
    wheel, then decays as exp(-age/tau). The golden angle is the phyllotaxis
    trick: for ANY number of consecutive flashes the hues are as evenly spread
    around the wheel as possible, so the sequence never settles into a visible
    repeating colour cycle the way a fixed increment does.

    Hard-blocked on the room by arithmetic. At 3 Hz there are 3.3 BLE samples
    between flashes and the 90 ms tail is 0.9 of one sample: the strips would
    show three unrelated colours stuttering, not a comet.
    """
    cfg = ctx.cfg.comet
    st = ctx.state

    if st.last_fire <= 0.0:
        # Self-seeding: `last_fire` resets to 0 while `now` is a monotonic
        # clock, so this is the first frame after a mode change. Without it the
        # age would be enormous and the pattern would open dark.
        st.last_fire = ctx.now
        st.hue = st.rng.random()

    _, fired = st.tick("comet", cfg.rate_hz * ctx.speed, ctx.dt)
    if fired:
        st.hue = (st.hue + cfg.hue_step) % 1.0
        st.last_fire = ctx.now

    v = math.exp(-max(0.0, ctx.now - st.last_fire) / cfg.tau_s)
    h, s = _base_hs(_palette_at(ctx.palette, st.hue))
    return unified(hsv_to_rgb(h, s, clamp01(v)))


def heartbeat(ctx: RenderCtx) -> Dict[str, RGB]:
    """P5 HEARTBEAT - a double-Gaussian pump. A lub-dub, not a pulse.

        v(t) = G(t, s1) + G(t - T, s1) + dub_gain * G(t - delay, s2)

    The second term evaluates the lub one period early so its leading edge
    appears at the end of the previous cycle and the wrap is continuous.

    sigma1 = 45 ms is an FWHM of ~106 ms: 3.2 frames on the PC, 1.06 on the
    room. This is the cleanest single demonstrator of why the split exists --
    the pattern is not merely worse on the strips, it is not representable
    there. The peak may sample slightly below 1.0 when no frame lands exactly
    on it, which is honest rather than worth correcting.
    """
    cfg = ctx.cfg.heartbeat
    bpm = max(cfg.bpm * ctx.speed, 1.0)
    period = 60.0 / bpm
    t = ctx.state.advance("heartbeat", bpm / 60.0, ctx.dt) * period

    def g(x: float, sd: float) -> float:
        return math.exp(-(x * x) / (2.0 * sd * sd))

    v = (g(t, cfg.sigma1_s)
         + g(t - period, cfg.sigma1_s)
         + cfg.dub_gain * g(t - cfg.dub_delay_s, cfg.sigma2_s))

    h, s = _base_hs(ctx.base_color)
    return unified(hsv_to_rgb(h, s, clamp01(v)))
