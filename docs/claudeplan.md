# RoomSync — Audio Reactive DSP Redesign

## Context

`_compute_audio_reactive` (`RoomSync/effects/engine.py:164-223`) looks like "a slow, soulless rainbow
crossfade" and the three strips read as disjointed lightbulbs. Four concrete causes, all confirmed in
the code:

1. **The transient is destroyed before the engine sees it.** `samplers/audio.py:31-33` applies an EMA
   (`smoothing_alpha = 0.3`) to every band inside the analyser. At 1024/48 kHz (≈21.3 ms/frame) that is
   τ ≈ 60 ms, ~140 ms to reach 90%. A kick's attack is 5–20 ms. The engine's `v_rate = 1.0`
   "instant attack" (`engine.py:197`) is already snapping — but onto pre-averaged mush.
2. **Brightness is railed.** `target_v = min(1.0, (total_mag / 0.15) ** 1.2)` (`engine.py:189`) clips
   whenever `bass+mids+highs > 0.15`. During normal music it sits pinned at 1.0 → zero dynamic range →
   reads as a flat crossfade.
3. **Hue teleports.** The winner-takes-all mapping (`engine.py:178-186`) picks between three *disjoint*
   hue wells (0.0 red / 0.33 green / 0.66 blue). Whenever band dominance flips — kick vs. hi-hat,
   many times a second — hue jumps a third of the wheel, and `h_rate = 1.0` on attack makes it
   instantaneous. That is the "cheap club lights" feel.
4. **The spatial field has a seam.** `hue_shift = (t_mid - 0.5) * 0.1` (`engine.py:217`) treats `t` as
   linear on [0,1], but `effects/topology.py` defines it as a **closed counter-clockwise loop** where
   t = 1.0 wraps to t = 0.0. So the physically adjacent points t≈0 and t≈1 get *opposite* offsets. The
   field is discontinuous exactly at the back-wall origin.

**Outcome wanted:** a single global colour driven by a fast-attack / slow-decay envelope that snaps on
a beat and breathes back down to a dim indigo base, projected onto the room as one continuous spatial
field with a beat-driven sweep — so the room reads as one organism, not three bulbs.

**Decisions taken** (from the clarifying questions): strip the analyser EMA and move all envelope
shaping into the engine; continuous spectral-arc hue; beat-kicked travelling wave; adaptive per-band
AGC. Scope is algorithmic only — no BLE, FastAPI, or transmit-loop changes, and the
`_target_color` state-overwrite pattern stays exactly as it is (no queues).

---

## The one non-obvious hardware constraint: 10 Hz aliasing

The engine computes at 30 Hz; `ble/worker.py:44-91` samples `_target_color` at 10 Hz. A true
zero-smoothing spike that decays in ~33 ms can land **entirely between two transmit samples and never
reach the LEDs at all**. Instant attack alone is not enough on this hardware.

Fix: a **peak hold** stage between attack and release. On attack, latch the peak and hold it for
`hold_s = 0.10` — exactly one transmit interval — guaranteeing at least one 10 Hz sample observes it.
This is standard attack/hold/release limiter architecture and it is what makes the punch actually
visible. Every release time constant is also kept ≥ 0.15 s for the same reason.

---

## Part 1 — `samplers/audio.py`: emit raw, add onset detection

**Remove** `_smooth()` (lines 31-33), `self.last_*` (lines 27-29), the EMA calls (lines 107-109), and
the hardcoded divisors `2.0 / 1.5 / 1.0` (lines 102-104). Per-band AGC in the engine makes the
divisors unnecessary — dividing by a band's own running peak cancels absolute scale, which also fixes
the structural problem that `highs` was a `.mean()` over ~341 mostly-empty bins while `bass` was a
mean over 6.

**Widen the contract** (defaults keep positional construction backward compatible):

```python
class AudioFrame(NamedTuple):
    bass: float
    mids: float
    highs: float
    ts: float
    flux_bass: float = 0.0
    flux_mids: float = 0.0
    flux_highs: float = 0.0
```

**Band energy — RMS instead of mean magnitude**, and skip the DC bin (`freq_to_bin(20, 48000, 1024)`
returns 0, so bin 0 — DC offset — currently leaks straight into bass):

```python
def _band_rms(mag: np.ndarray, lo: int, hi: int) -> float:
    lo = max(1, lo)                       # never include DC
    hi = max(lo + 1, hi)
    seg = mag[lo:hi]
    return float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0
```

**Spectral flux — the actual beat-drop detector.** Half-wave-rectified spectral flux is the standard
onset detection function: only *rising* bins count, so it fires on attacks and ignores decays. This is
what "snap when a beat drops" should key off, not raw level.

```python
# in __init__
self._prev_mag: np.ndarray | None = None
self._window = None                       # cache np.hanning; currently reallocated every frame

# per frame, after fft_mag is computed
if self._prev_mag is not None and self._prev_mag.shape == fft_mag.shape:
    d = fft_mag - self._prev_mag
    np.maximum(d, 0.0, out=d)             # half-wave rectify
    flux_bass  = float(d[max(1, bass_start):bass_end].sum())
    flux_mids  = float(d[mids_start:mids_end].sum())
    flux_highs = float(d[highs_start:highs_end].sum())
else:
    flux_bass = flux_mids = flux_highs = 0.0
self._prev_mag = fft_mag
```

Also hoist the bin-index computation out of the loop — `sr` and `chunk` never change, so the six
`freq_to_bin` calls (lines 83-90) belong before `while True`. Cache `np.hanning(chunk)` the same way.

Values are now unbounded rather than clamped to [0,1]; nothing else consumes `AudioFrame`
(`_compute_ambient` ignores its audio argument, and `web/ws.py` never sees it).

---

## Part 2 — engine plumbing: don't drop the transient you just preserved

Audio arrives at ≈46.9 Hz into a `janus.Queue(maxsize=2)` (`web/app.py:33`); the engine ticks at
30 Hz and `drain_latest` (`engine.py:77`) **throws away the older frame** — which may be exactly the
peak of the kick. Switch audio to the existing `drain_all` (`utils/helpers.py:15`) and reduce with an
elementwise max — peak-preserving decimation. Screen stays on `drain_latest` (no transients there).

```python
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
```

**Measured `dt`, not per-tick constants.** Every existing rate (`0.08`, `0.05`, `0.15`) is a per-tick
multiplier silently coupled to `tick_hz`, so decay speed changes whenever a tick overruns — and ticks
*do* overrun, because `engine.py:109` awaits a blocking OpenRGB socket write every tick. In `run()`:

```python
now = tick_start
dt = (1.0 / settings.general.tick_hz) if self._last_tick is None else (now - self._last_tick)
dt = min(max(dt, 1e-3), 0.25)     # guard first tick, stalls, and debugger pauses
self._last_tick = now
```

Pass `now` and `dt` into `_compute_audio_reactive(frame, now, dt)`.

---

## Part 3 — the DSP core

New state in `__init__` (and mirrored in `_reset_state`, `engine.py:131-137`), replacing
`self._audio_current_hsv`:

```python
self._last_tick: Optional[float] = None
self._env   = {"bass": 0.0, "mids": 0.0, "highs": 0.0, "punch": 0.0}   # post-AGC, 0..1
self._peak  = {"bass": 0.0, "mids": 0.0, "highs": 0.0, "punch": 0.0}   # AGC running peaks
self._hold  = {"bass": 0.0, "mids": 0.0, "highs": 0.0, "punch": 0.0}   # hold-until timestamps
self._audio_hue = settings.audio.reactive.base_hsv[0]
self._wave_phase = 0.0
```

### 3a. Adaptive gain — one running peak per band

```python
def _agc(self, band: str, raw: float, dt: float) -> float:
    cfg = settings.audio.reactive
    p = self._peak[band] * math.exp(-dt / cfg.agc_tau_s)   # slow bleed-down, ~5 s
    if raw > p:
        p = raw                                            # instant peak capture
    self._peak[band] = p
    if p < cfg.agc_floor:                                  # silence: no gain, stay dark
        return 0.0
    return min(1.0, raw / p)
```

This is what unrails brightness: the full 0..1 range is always in use regardless of source volume, and
the magic `0.15` normalizer and `0.03` gate both disappear.

### 3b. Fast-attack / hold / slow-decay envelope follower — the requested core

```python
def _follow(self, band: str, target: float, now: float, dt: float) -> float:
    """Zero-smoothing attack, one-transmit-interval peak hold, exponential release."""
    cur = self._env[band]
    cfg = settings.audio.reactive

    if target >= cur:                          # ATTACK — literally 0 smoothing
        self._env[band] = target
        self._hold[band] = now + cfg.hold_s    # guarantee the 10 Hz TX loop sees the peak
        return target

    if now < self._hold[band]:                 # HOLD
        return cur

    tau = cfg.release_s[band]                  # RELEASE — dt-correct, frame-rate independent
    self._env[band] += (target - cur) * (1.0 - math.exp(-dt / tau))
    return self._env[band]
```

Per-band release: `bass 0.35 s` (the breathe), `mids 0.25 s`, `highs 0.18 s` (hi-hats sparkle),
`punch 0.12 s` (a stab, not a body). `punch` rides the same follower — the onset signal is just a
fourth band, driven by weighted spectral flux through the same AGC:

```python
flux_raw = frame.flux_bass + 0.7 * frame.flux_mids + 0.4 * frame.flux_highs
punch    = self._follow("punch", self._agc("punch", flux_raw, dt), now, dt)
```

When `frame is None`, do **not** freeze — call `_follow(band, 0.0, now, dt)` for each band so the room
keeps breathing down to base.

### 3c. Brightness — weighted energy, perceptual gamma, floored base

```python
e = (cfg.band_weights[0] * env_bass
     + cfg.band_weights[1] * env_mids
     + cfg.band_weights[2] * env_highs)            # 0.55 / 0.30 / 0.15 — bass owns the room
v = cfg.v_floor + (1.0 - cfg.v_floor) * (e ** cfg.gamma)
```

`gamma = 2.0` because LED output is linear in PWM while perception is roughly a 2.2 power law. Applying
it makes the decay tail *look* smooth instead of falling off a cliff, and — since AGC normalises to the
track peak while musical RMS sits well below it — it produces exactly the "mostly dim, big flashes on
hits" behaviour asked for. `v_floor = 0.10` keeps the base glow visible instead of gamma crushing it
to black. Lower gamma = more constant glow.

### 3d. Hue — continuous spectral arc (no teleport)

Bands become *positions on one arc* rather than three disjoint wells, so hue moves proportionally with
the mix and is continuous by construction:

```python
tot = env_bass + env_mids + env_highs
pos = ((0.0 * env_bass + 0.5 * env_mids + 1.0 * env_highs) / tot) if tot > 1e-6 else 0.0
arc_hue = (cfg.hue_start + pos * cfg.hue_span) % 1.0
```

Defaults `hue_start = 0.08`, `hue_span = -0.46` sweep **amber → red → magenta → violet → blue**
(0.08 → 0.62 travelling *downward*). Chosen deliberately over a red→green→cyan arc: it never passes
through green (harsh and institutional on LED strips), it is warm-for-bass / cool-for-highs, and it
*contains* the deep-indigo base at 0.72 — so decaying to base is a short hue move, not a wheel
crossing.

Blend toward base as energy falls, then glide — reusing the existing shortest-path wrap logic from
`engine.py:205-207`, extracted as a helper:

```python
def _hue_lerp(a: float, b: float, t: float) -> float:
    d = ((b - a) + 0.5) % 1.0 - 0.5        # shortest path around the wheel
    return (a + d * t) % 1.0

hue_target = _hue_lerp(cfg.base_hsv[0], arc_hue, min(1.0, e / cfg.hue_full_at))
tau = cfg.hue_tau_punch_s + (cfg.hue_tau_s - cfg.hue_tau_punch_s) * (1.0 - punch)
self._audio_hue = _hue_lerp(self._audio_hue, hue_target, 1.0 - math.exp(-dt / tau))
```

Hue gets a 100 ms glide normally, tightening to 30 ms under a strong onset — so a drop *does* snap its
colour without the constant teleporting.

### 3e. Saturation — white-hot punch

Replaces the unconditional `s = 1.0` (`engine.py:210`). Pulling saturation *down* on a transient makes
a big hit read as a bright flash rather than merely "more purple":

```python
sat = cfg.sat_base - (cfg.sat_base - cfg.sat_peak) * punch     # 1.0 → 0.70 on a hit
```

### 3f. Spatial field — circular, beat-kicked

One global colour, sampled at three points of a **continuous periodic field**. Using `2π·t` makes the
field genuinely periodic on the loop, which is the fix for the seam in cause (4):

```python
self._wave_phase = (self._wave_phase
                    - (cfg.wave_drift_hz * self.state.speed
                       + cfg.wave_kick_hz * punch) * dt) % 1.0

colors = {}
for name, t_mid in STRIP_MIDPOINTS.items():
    theta   = 2.0 * math.pi * (t_mid - self._wave_phase)
    hue_off = cfg.hue_spread * math.sin(theta)                  # ±0.04
    lobe    = 0.5 + 0.5 * math.cos(theta)                       # 0..1 travelling lobe
    v_gain  = 1.0 - cfg.wave_depth * (1.0 - lobe)               # depth 0.35
    colors[name] = hsv_to_rgb((self._audio_hue + hue_off) % 1.0,
                              sat, clamp01(v * v_gain))
return colors
```

**The phase must be *subtracted*.** `STRIP_MIDPOINTS` places Back = 0.1008, Left = 0.4006,
Right = 0.7998, so decreasing `t` visits Left → Back → Right (wrapping) — the room order stated in the
request. Advancing the phase the other way sweeps Back → Left → Right and looks wrong.

`wave_kick_hz = 1.20` means each onset shoves the lobe forward, so a beat visibly propagates across
the walls; `wave_drift_hz = 0.08` keeps it slowly alive between beats and is multiplied by
`self.state.speed`, which finally makes the existing `SET_SPEED` control do something in this mode.

---

## Part 4 — small supporting changes

- **`utils/color.py`**: add `clamp01(x: float) -> float`. The existing `clamp` is int-typed (0-255) and
  cannot floor a float V.
- **`effects/engine.py`**: use the already-imported-but-unused `hsv_to_rgb` from `utils.color`
  (`engine.py:12`) instead of calling `colorsys.hsv_to_rgb` directly (line 220); the `colorsys` import
  at line 5 then becomes dead and can go.
- **`config.py` / `config.toml`**: new `AudioReactiveConfig(BaseModel)` with **every field defaulted**,
  attached as `reactive: AudioReactiveConfig = AudioReactiveConfig()` on `AudioConfig`. Defaults are
  mandatory here: every existing settings field is required, so a missing TOML key is a hard startup
  crash. Drop the now-dead `smoothing_alpha` from both files.
- **`engine.py:107-109`** (recommended, 3 lines): `await self.rgb_bridge.set_unified_color(dom)` runs a
  blocking socket write through an executor on *every* tick with no change detection. It stalls the
  tick loop and injects timing jitter straight into the transient path. Skip the call when `dom` is
  unchanged, and throttle it to ~10 Hz to match the strips.
- `_dominant_color` (`engine.py:261-267`) needs no change — with only ±0.04 of hue spread its mean is
  visually identical to the global colour, so peripherals stay cohesive with the walls for free.

---

## Files to modify

| File | Change |
|---|---|
| `RoomSync/samplers/audio.py` | Drop EMA; RMS bands excluding DC; spectral flux; widen `AudioFrame`; hoist bin/window computation |
| `RoomSync/effects/engine.py` | Rewrite `_compute_audio_reactive`; add `_agc` / `_follow` / `_reduce_audio` / `_hue_lerp`; `dt` in `run()`; new state in `__init__` + `_reset_state`; `drain_all` for audio |
| `RoomSync/config.py` | Add `AudioReactiveConfig`; remove `smoothing_alpha` |
| `RoomSync/config.toml` | Add `[audio.reactive]`; remove `smoothing_alpha` |
| `RoomSync/utils/color.py` | Add `clamp01` |

---

## Verification

**1. Offline DSP harness (no hardware, no audio device).** Write a throwaway probe in the scratchpad
that instantiates `EffectEngine` with stub `ble_mgr`/`screen`/queue and drives
`_compute_audio_reactive` with synthetic `AudioFrame`s at 30 Hz. Assert the properties, don't
eyeball them:

- **Attack**: a step from silence to full bass reaches peak `v` in **exactly one tick**.
- **Hold**: after that step, `v` is unchanged for ≥ 0.10 s — this is the property that proves the punch
  survives the 10 Hz transmit sampling. Sample the output at 10 Hz and confirm the peak appears.
- **Release**: fit the decay; the measured τ must match `release_bass_s` within ~10%. Re-run at a
  simulated 15 Hz tick rate — τ must be **unchanged** (proves `dt` correctness).
- **No teleport**: over a 120 BPM kick+hat pattern, max per-tick hue delta stays small (≪ 0.1); the old
  code jumps ~0.33.
- **AGC**: feed the same pattern at 1.0× and 0.05× amplitude — peak `v` should converge to the same
  value within ~2 × `agc_tau_s`.
- **Silence**: after ~2 s of zero frames, all three strips settle at the base indigo, all within a few
  units of each other.
- **Spatial continuity**: sweep `_wave_phase` across 0→1 and confirm each strip's colour is continuous
  at the wrap — the current `(t_mid - 0.5)` form fails this.

**2. Live, without BLE.** `python main.py`, `POST` mode `AUDIO_REACTIVE`, open `http://127.0.0.1:8420`.
`web/static/app.js:44-59` already renders all three strip colours from the 30 Hz WebSocket broadcast —
a free visual monitor of the real pipeline with real audio and no strips connected. Play a track with a
hard drop and confirm: snap on the beat, smooth breathe down, visible sweep across the three panels.

**3. `agc_floor` is the one number that needs calibrating by ear.** Log raw band RMS at DEBUG for a few
seconds of silence and a few of music, then set the floor between the two. Everything else should be
usable at the defaults above.

**4. Hardware.** Reconnect the strips and confirm the punch lands. If it feels like it strobes, raise
`release_bass_s`; if it feels sluggish, lower `gamma` before touching the release times.

**Note:** `test_ble.py` is stale — lines 18/23 `await ble_mgr.set_color(...)`, but that method is
synchronous (`ble/manager.py:34`), so it raises `TypeError`. Don't use it as a smoke test. Separately,
`janus` is imported (`samplers/audio.py:7`, `web/app.py:3`) but missing from `requirements.txt`.
