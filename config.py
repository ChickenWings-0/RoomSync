from typing import List, Dict, Tuple
from pydantic import BaseModel
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, TomlConfigSettingsSource

from utils.paths import config_path

class GeneralConfig(BaseModel):
    mode: str
    brightness: float
    tick_hz: int
    log_level: str

class StripConfig(BaseModel):
    name: str
    mac: str
    pixels: int

class BLEConfig(BaseModel):
    write_uuid: str
    max_write_hz: int
    reconnect_attempts: int
    stagger_delay_ms: int
    strips: List[StripConfig]

class ScreenConfig(BaseModel):
    sample_hz: int
    subsample_skip: int
    edge_fraction: float

class AudioReactiveConfig(BaseModel):
    """DSP tuning for AUDIO_REACTIVE. Every field MUST have a default:
    settings are otherwise required, so a missing TOML key is a startup crash."""

    # ── Adaptive gain control (per-band running peak) ─────────────
    agc_tau_s: float = 5.0          # peak bleed-down time constant
    agc_floor: float = 0.004        # below this running peak, treat as silence

    # ── Envelope follower ─────────────────────────────────────────
    hold_s: float = 0.10            # one 10 Hz transmit interval — the punch must be sampled
    release_s: Dict[str, float] = {
        "bass": 0.35,
        "mids": 0.25,
        "highs": 0.18,
        "punch": 0.12,
    }

    # ── Brightness ────────────────────────────────────────────────
    band_weights: Tuple[float, float, float] = (0.55, 0.30, 0.15)
    gamma: float = 2.0              # perceptual curve; lower = more constant glow
    v_floor: float = 0.10           # base glow, keeps gamma from crushing to black

    # ── Hue ───────────────────────────────────────────────────────
    base_hsv: Tuple[float, float, float] = (0.72, 1.0, 0.10)   # deep indigo base
    hue_start: float = 0.08         # amber
    hue_span: float = -0.46         # travels downward: amber -> red -> magenta -> violet -> blue
    hue_full_at: float = 0.35       # energy at which hue fully leaves base
    hue_tau_s: float = 0.10         # normal glide
    hue_tau_punch_s: float = 0.03   # tightened glide under a strong onset

    # ── Saturation ────────────────────────────────────────────────
    sat_base: float = 1.0
    sat_peak: float = 0.70          # white-hot on a transient

    # ── Spatial travelling wave ───────────────────────────────────
    wave_drift_hz: float = 0.08     # idle drift (multiplied by state.speed)
    wave_kick_hz: float = 1.20      # onset shove
    hue_spread: float = 0.04        # +/- hue offset across the room
    wave_depth: float = 0.35        # brightness depth of the travelling lobe

    # ── Onset weighting (spectral flux mix) ───────────────────────
    flux_weights: Tuple[float, float, float] = (1.0, 0.7, 0.4)

class MusicConfig(BaseModel):
    """DSP tuning for MUSIC — one unified, winner-takes-all club strobe.
    Every field MUST have a default: settings are otherwise required,
    so a missing TOML key is a startup crash."""

    # ── Adaptive gain control (per-band running peak) ─────────────
    agc_tau_s: float = 3.0          # faster bleed than AUDIO_REACTIVE: hugs the mix
    agc_floor: float = 0.004        # below this running peak, treat as silence

    # ── Envelope follower ─────────────────────────────────────────
    hold_s: float = 0.10            # one 10 Hz transmit interval — the flash MUST be sampled
    release_s: Dict[str, float] = {
        "bass": 0.15,               # aggressive: fully dark again between kicks
        "mids": 0.12,
        "highs": 0.09,
        "punch": 0.08,
    }

    # ── Brightness ────────────────────────────────────────────────
    band_weights: Tuple[float, float, float] = (0.60, 0.28, 0.12)
    gamma: float = 3.2              # hard flash curve; crushes the decay tail to black
    v_floor: float = 0.02           # near-black between hits
    v_gate: float = 0.06            # below this, snap fully dark (no smouldering glow)

    # ── Winner-takes-all hue (zero hue smoothing) ─────────────────
    hue_bass: float = 0.00          # red
    hue_mids: float = 0.33          # green
    hue_highs: float = 0.60         # blue
    band_bias: Tuple[float, float, float] = (1.00, 1.60, 2.20)  # lifts mids/highs vs. raw bass
    switch_margin: float = 1.10     # a challenger must beat the incumbent by this factor
    dominance_floor: float = 0.05   # below this energy, hold the last colour

    # ── Saturation ────────────────────────────────────────────────
    sat_base: float = 1.0
    sat_peak: float = 0.80          # white-hot core on a hard transient

    # ── Onset weighting (spectral flux mix) ───────────────────────
    flux_weights: Tuple[float, float, float] = (1.0, 0.7, 0.4)

class AudioConfig(BaseModel):
    chunk_size: int
    bands: Dict[str, List[int]]
    reactive: AudioReactiveConfig = AudioReactiveConfig()
    music: MusicConfig = MusicConfig()

class OpenRGBConfig(BaseModel):
    enabled: bool
    host: str
    port: int

class WebConfig(BaseModel):
    host: str
    port: int

class TrayConfig(BaseModel):
    """System tray icon. Every field MUST have a default: settings are
    otherwise required, so a missing TOML section would be a startup crash on
    every existing install."""
    enabled: bool = True

class BreatheConfig(BaseModel):
    """R1 - raised cosine on the brightness channel. Every field MUST have a
    default: settings are otherwise required, so a missing TOML key is a
    startup crash."""
    freq_hz: float = 0.12       # ~8 s per breath at speed 1.0
    gamma: float = 2.2          # perceptual curve, same idea as audio.reactive.gamma
    v_min: float = 0.15
    v_max: float = 1.0
    strip_offset: float = 0.15  # cycles of lag across the room; 0.0 = perfect unison

class PhaseConfig(BaseModel):
    """R2 - continuous hue rotation with a small spatial spread."""
    freq_hz: float = 0.02       # ~50 s per full revolution of the wheel
    spread: float = 0.08        # strips sit 8% of the wheel apart (analogous, never clashing)
    sat: float = 0.85
    val: float = 0.90

class TideConfig(BaseModel):
    """R3 - two incommensurate sines. freq_a/freq_b is near the golden ratio,
    so the beat envelope never closes and the room never visibly loops."""
    freq_a_hz: float = 0.070
    freq_b_hz: float = 0.043
    counter_rate: float = 1.7   # the second wave travels the other way, faster
    v_mid: float = 0.50
    amp: float = 0.25           # per wave; v spans v_mid +/- 2*amp before clamping
    hue_swing: float = 0.03

class EmberConfig(BaseModel):
    """R4 (AMBIENT) - Ornstein-Uhlenbeck hearth drift.

    sigma is the STATIONARY standard deviation of the process, not the SDE's
    diffusion coefficient: the exact discretisation in patterns.ember() holds
    that std at any tick rate. tau_floor_s caps how far `speed` may shorten
    tau, which is what keeps the Lorentzian corner at 1/(2*pi*tau) below the
    radio's Nyquist floor no matter where the speed slider is dragged.
    """
    mu: float = 0.55            # mean brightness
    sigma: float = 0.10         # stationary std of the brightness walk
    tau_s: float = 4.0          # mean-reversion time constant -> corner at 0.04 Hz
    tau_floor_s: float = 0.8    # hard floor once speed shortens tau -> corner <= 0.2 Hz
    hue_sag: float = 0.02       # hue shift per 1 sigma of brightness (dimmer runs warmer)

class LighthouseConfig(BaseModel):
    """R5 - a macro-scale rotating beam over the room's three spatial pixels.

    The strips ARE the pixels: a beam is a Gaussian in the circular distance
    between the beam's angular position and each strip's midpoint on the room's
    1D [0, 1) loop. sigma is expressed in units of that loop, and 0.17 is
    slightly wider than the 1/3 spacing between midpoints, so the handover from
    one wall to the next is a crossfade rather than a cut -- the only way three
    pixels can read as motion instead of as three switches.

    max_hz is the band-limit guarantee, not a taste ceiling. One revolution
    sweeps the beam past a strip in ~2*sigma/f seconds; at the 0.25 Hz ceiling
    that is a 1.36 s transit, i.e. ~7 BLE samples across the lobe even on the
    5 Hz blind-write path. The default 0.09 Hz is an 11 s revolution.
    """
    freq_hz: float = 0.09       # ~11 s per lap of the room at speed 1.0
    max_hz: float = 0.25        # hard ceiling; keeps the lobe >= ~7 BLE samples wide
    sigma: float = 0.17         # beam half-width, in units of the room loop
    v_beam: float = 1.0
    v_floor: float = 0.12       # the darker background the beam travels over
    hue_lead: float = 0.03      # the beam runs marginally hotter than the floor

class RainbowConfig(BaseModel):
    """R6 - the classic full-spectrum cycle, whole-room unison.

    Deliberately NOT phase_drift's spatial spread: the intent users have when
    they pick "Rainbow" is one continuous strip, so spread defaults to 0.0 and
    the three walls hold the same hue. At 0.035 Hz (~29 s per revolution) a
    200 ms BLE sample advances the hue by 0.007 of the wheel, ~2.5 degrees.
    """
    freq_hz: float = 0.035      # ~29 s per full revolution at speed 1.0
    spread: float = 0.0         # 0.0 = the room is one pixel; raise for a spatial sweep
    sat: float = 1.0
    val: float = 1.0

class ShadesConfig(BaseModel):
    """R7 - monochromatic drift within the picked colour's family.

    Saturation and value each ride their own slow sine, and the hue rides a
    third at +/- hue_swing. The three rates are mutually irrational, so the
    (h, s, v) point never retraces the same path and the colour "breathes"
    without ever leaving the family -- dark blue to icy blue, never to green.

    hue_swing is 0.04 = 14.4 degrees of the wheel, small enough that the result
    still names as one colour. Worst-case hue rate is 2*pi*0.04*0.031 = 0.008
    per second, two orders of magnitude inside the room's budget.
    """
    hue_swing: float = 0.04     # +/- fraction of the wheel; 0.04 = +/-14.4 deg
    hue_rate_hz: float = 0.031
    sat_rate_hz: float = 0.019
    val_rate_hz: float = 0.047  # mutually irrational with the other two
    sat_min: float = 0.35
    sat_max: float = 1.0
    val_min: float = 0.25
    val_max: float = 1.0
    strip_offset: float = 0.10  # cycles of lag along the room; 0.0 = unison

class StrobeConfig(BaseModel):
    """P1 - square wave. max_hz is where a duty cycle stops being representable
    at a 30 Hz tick (4 frames per period).

    NOTE: 3-30 Hz high-contrast flashing is the photosensitive-seizure band.
    max_hz is the safety ceiling; lower it rather than raising it.
    """
    freq_hz: float = 4.0
    duty: float = 0.12
    min_hz: float = 0.5
    max_hz: float = 7.5

class FlickerConfig(BaseModel):
    """P2 - Poisson-arrival glitch bursts on a stable neon base.

    Poisson arrivals rather than a per-frame coin flip: a coin flip at 30 Hz is
    memoryless hash, Poisson gives long clean stretches punctuated by clustered
    bursts, which is what a failing tube actually does. Beta(0.4, 0.4) is
    U-shaped, so a burst is a sequence of hard on/off steps rather than a fade.
    """
    rate_hz: float = 1.5        # lambda: burst arrivals per second
    burst_min_s: float = 0.04
    burst_max_s: float = 0.16
    resample_s: float = 0.066   # ~2 frames at 30 Hz: discrete steps, not noise
    beta_a: float = 0.4
    beta_b: float = 0.4
    hue_jitter: float = 0.02    # voltage sag during a burst
    base_v: float = 0.88        # between bursts

class PlasmaConfig(BaseModel):
    """P3 - quasi-periodic sum of sines. The five rates are mutually irrational,
    so the trajectory is dense on a 5-torus and never exactly repeats.

    The ratios are what make it non-repeating; the SCALE is what makes it
    PC-exclusive. At a quarter of these rates the pattern measures as
    band-limited under the radio's Nyquist floor, i.e. it would have been
    perfectly representable on the strips and room_ok=False would have been an
    arbitrary label. Scaled up, the sidebands of sin(d)*cos(e) reach 4.1 Hz and
    only ~69% of the power stays under 2.5 Hz. See test_patterns.py [2].
    """
    rates_hz: Tuple[float, float, float, float, float] = (1.24, 1.88, 0.44, 2.92, 1.16)
    v_mid: float = 0.55
    v_amp: float = 0.35
    sat: float = 0.95

class CometConfig(BaseModel):
    """P4 - impulse train with golden-angle hue stepping.

    0.381966 = 1/phi^2. For ANY number of consecutive flashes the hues are as
    evenly spread around the wheel as possible, so the sequence never falls into
    a visible repeating colour cycle the way a fixed increment does.
    """
    rate_hz: float = 3.0
    tau_s: float = 0.09         # decay tail: ~3 frames at 30 Hz, 0.9 at 10 Hz
    hue_step: float = 0.381966

class HeartbeatConfig(BaseModel):
    """P5 - double-Gaussian pump. sigma1 = 45 ms is an FWHM of ~106 ms: 3.2
    frames on the PC, 1.06 on the room. Not merely worse there - not
    representable there."""
    bpm: float = 72.0
    sigma1_s: float = 0.045     # lub
    sigma2_s: float = 0.055     # dub
    dub_delay_s: float = 0.19
    dub_gain: float = 0.62

class PatternsConfig(BaseModel):
    """Tuning for the standalone generative patterns. Every nested model must
    itself default, so the whole section may be absent from config.toml."""
    breathe: BreatheConfig = BreatheConfig()
    phase: PhaseConfig = PhaseConfig()
    tide: TideConfig = TideConfig()
    ember: EmberConfig = EmberConfig()
    lighthouse: LighthouseConfig = LighthouseConfig()
    rainbow: RainbowConfig = RainbowConfig()
    shades: ShadesConfig = ShadesConfig()
    strobe: StrobeConfig = StrobeConfig()
    flicker: FlickerConfig = FlickerConfig()
    plasma: PlasmaConfig = PlasmaConfig()
    comet: CometConfig = CometConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()

class Settings(BaseSettings):
    general: GeneralConfig
    ble: BLEConfig
    screen: ScreenConfig
    audio: AudioConfig
    openrgb: OpenRGBConfig
    patterns: PatternsConfig = PatternsConfig()
    web: WebConfig
    tray: TrayConfig = TrayConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # An ABSOLUTE path, resolved by utils.paths rather than by the
        # working directory. "config.toml" is only correct when the process
        # happens to be launched from the repo root, which a service started
        # by the Task Scheduler, a shortcut or a tray app never is — and the
        # failure mode is a crash at import time with an empty-looking
        # validation error naming every required field at once.
        return (TomlConfigSettingsSource(settings_cls, config_path()),)

# Global settings instance
settings = Settings()
