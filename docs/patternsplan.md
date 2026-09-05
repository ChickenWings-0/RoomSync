# Custom Animation Patterns — Library & Architecture

## Context

RoomSync now drives two genuinely independent outputs (`AppState.room_mode` / `AppState.pc_mode`,
`effects/engine.py:83-84`), each rendered separately every tick and routed to its own hardware
(`EffectEngine._compute_mode` / `_route`). Everything currently in the `Mode` enum is either
*content-driven* (Screen Sync, Audio Reactive, Music) or trivial (Static, Pulse, Wave). There are no
standalone generative patterns — nothing you can put on the room at 2am with the PC off, and nothing
that exploits the fact that the PC path is ~6× faster than the radio.

This adds a library of self-contained mathematical animation patterns, assignable independently to
Room or PC, plus the small amount of engine plumbing needed to keep two different patterns running
against two different parameter sets without bleeding into each other.

---

## 1. The hardware constraint, stated numerically

This is the axis the whole library is organised around, and it is sharper than "10Hz vs 30Hz":

| | Engine render | Actually delivered | Worst case |
|---|---|---|---|
| **Room** (BLE) | 30 Hz | 10 Hz — `TRANSMIT_INTERVAL = 0.1` (`ble/worker.py:18`) | **5 Hz** — `UNACKED_TRANSMIT_INTERVAL = 0.2` when the characteristic is write-without-response (`ble/worker.py:25`) |
| **PC** (OpenRGB) | 30 Hz | 30 Hz — `PUSH_HZ = settings.general.tick_hz` (`peripherals/openrgb_bridge.py:15`) | 30 Hz |

The engine renders at 30 Hz and the BLE worker *decimates* it — `set_target_color` overwrites,
never queues (`ble/worker.py:210-222`). So a room pattern is a 30 Hz signal sampled at 5–10 Hz.

**Room design rule:** fundamental frequency ≤ 0.5 Hz, and the pattern must be *band-limited* below
the 2.5 Hz worst-case Nyquist. Decimating a band-limited signal is lossless; decimating anything
else aliases, and aliasing on a light strip reads exactly as the "glitchy mess" we're avoiding. A
useful secondary check: no more than ~0.05 change in V and ~0.02 in hue per 200 ms sample.

**PC design rule:** the honest ceiling is ~7.5 Hz for anything with a duty cycle (4 frames/period is
the minimum for a stable on/off ratio at 30 Hz), and features can be as short as ~33 ms. That is the
whole PC-exclusive budget: sub-100 ms transients.

Also relevant: **the PC is one logical zone.** `OpenRGBBridge._write_blocking` calls
`client.set_color(RGBColor(*rgb))` (`peripherals/openrgb_bridge.py:109-111`), and `_route` collapses
a per-strip frame via `_dominant_color`, which is a plain mean (`effects/engine.py:799-805`).
So **PC patterns must be purely temporal** — a spatial PC pattern averages to mud. Conversely the
room has three addressable zones at `t = 0.1008 / 0.4006 / 0.7998` (`effects/topology.py`), so room
patterns *can* be coarsely spatial, and the good ones are.

---

## 2. Room patterns (4)

All four are C¹-continuous, clock-driven, and read their base hue from that target's existing colour
picker via `_static_color_for(target)` — so they inherit the Link Colors UI for free. All are exempt
from the room low-pass (they belong in the `else` pass-through branch of `_route`,
`effects/engine.py:378-386`): they are already smooth by construction, and filtering them would only
add lag.

### R1 — `BREATHE`
A single raised-cosine on the brightness channel. Unified across all three strips.

```
v(t) = v_min + (v_max - v_min) · (0.5 - 0.5·cos(2π·f·t))^γ
f = 0.12 Hz · speed        # ~8 s per breath
γ = 2.2                    # perceptual curve, same idea as audio.reactive.gamma
v_min = 0.15, v_max = 1.0
```

Raised cosine rather than a raw sine so the cycle starts at its minimum — no discontinuity at t=0,
and no phase pop when the mode is switched on. Max |dv/dt| = π·f·(v_max−v_min) ≈ 0.32 /s → 0.064 per
200 ms sample. Comfortably band-limited.

Optional refinement: offset each strip by `φ_i = t_mid_i · 0.15` so the room *inflates* rather than
blinking in unison. 0.15 of a cycle at 0.12 Hz is a 1.25 s lag — slow enough to survive decimation.

### R2 — `PHASE` (slow hue drift)
Continuous rotation through the colour wheel with a small spatial phase offset per strip.

```
h_i(t) = (h₀ + f·t + k·t_mid_i) mod 1
f = 0.02 Hz · speed        # ~50 s per full revolution
k = 0.08                   # strips sit 8% of the wheel apart
s = 0.85, v = 0.9          # fixed
```

This is the elegant counterpart to the existing `WAVE`, which indexes a 256-entry discrete palette
and moves fast (`_compute_wave`, `effects/engine.py:784-794`). `PHASE` is continuous, uses a tiny
hue spread so the three walls are always *analogous* colours rather than a rainbow clash, and moves
slowly enough that each 200 ms sample advances hue by 0.004 (≈1.4°) — invisible stepping. Reuses
`STRIP_MIDPOINTS` exactly as `_compute_wave` does.

### R3 — `TIDE` (two-wave interference)
Sum of two incommensurate sines per strip. No randomness, no perceptible loop.

```
v_i(t) = 0.5 + 0.25·sin(2π·f₁·t + 2π·t_mid_i) + 0.25·sin(2π·f₂·t - 2π·1.7·t_mid_i)
h_i(t) = (h₀ + 0.03·sin(2π·f₂·t)) mod 1
f₁ = 0.070 Hz · speed
f₂ = 0.043 Hz · speed      # f₁/f₂ ≈ φ, so the beat never closes
```

The beat envelope has a period of ~37 s and, because the ratio is near-irrational, the *combined*
three-strip state does not repeat on any human timescale. The bright region wanders around the room
continuously. Max |dv/dt| = 2π(0.25f₁ + 0.25f₂) ≈ 0.18 /s → 0.035 per sample.

### R4 — `AMBIENT` / Ember (Ornstein–Uhlenbeck drift)
A mean-reverting random walk on V, run independently per strip.

This one **reuses the existing `AMBIENT` enum member** rather than adding a new one. `AMBIENT` is
currently a stub that falls straight through to `_compute_static` (`effects/engine.py:796-797`) — a
mode that claims to be something it isn't. Giving it this implementation is what the name already
promises, keeps the button where users expect it, and leaves no dead mode behind.

```
dx = -(x - μ)/τ · dt + σ·√dt · N(0,1)
μ = 0.55, τ = 4.0 s, σ = 0.0707        # stationary std = σ√(τ/2) ≈ 0.10
h_i = h₀ + 0.02·(x_i - μ)/0.10          # voltage sag: dimmer runs warmer
```

The reason this is the *right* stochastic process for the radio rather than plain noise: an OU
process is a first-order low-pass applied to white noise, with a Lorentzian spectrum whose corner
sits at `1/(2πτ)` = **0.04 Hz**. Choosing τ = 4 s therefore *guarantees* the output is band-limited
far below the 2.5 Hz Nyquist floor with no extra filtering — the pattern is organic but physically
cannot flicker. Reads as a fireplace.

Note this is deliberately the same mathematical family as the PC's `FLICKER` below, with the time
constant at the opposite extreme. τ → 4 s is a hearth; τ → 0 is a failing neon tube. One codebase
idea, two hardware realities.

**Enum cost of §2:** three new members (`BREATHE`, `PHASE`, `TIDE`) plus a real body for `AMBIENT`.

---

## 3. PC-exclusive patterns (5)

All unified single colours (see §1 — the PC is one zone), all with features below the room's Nyquist
floor, all gated to `pc_ok` only.

### P1 — `STROBE`
Square wave with duty cycle, driven by a **phase accumulator** rather than `now % T`.

```
phase = (phase + f·dt) mod 1
v = 1.0 if phase < duty else 0.0
f = clamp(4.0 · speed, 0.5, 7.5) Hz
duty = 0.12
```

Accumulator rather than wall-clock modulo because at 30 Hz a 7.5 Hz strobe is only 4 frames per
period; sampling `now % T` gives an unstable duty that visibly jitters. The 7.5 Hz clamp is where
the duty stops being representable.

*Flag for config:* 3–30 Hz high-contrast flashing is the photosensitive-seizure band. Worth a
hard ceiling in `config.toml` and a one-line note in the UI hint, not a blocker.

### P2 — `FLICKER` (Cyberpunk / failing neon)
Poisson-arrival glitch bursts on a stable neon base.

```
# per tick, arrival test:
P(burst | dt) = 1 - exp(-λ·dt),  λ = 1.5 · speed  events/s
burst duration ~ U(0.04, 0.16) s
during burst:  v ~ Beta(0.4, 0.4), resampled every ~66 ms; h += U(-0.02, 0.02)
between bursts: v = 0.88 + 0.02·drift
```

The two choices that make this look like broken hardware instead of TV static:

- **Poisson arrivals, not per-frame randomness.** A uniform coin flip every frame produces
  memoryless hash at 30 Hz. Poisson produces long clean stretches punctuated by clustered bursts,
  which is what a failing tube actually does.
- **Beta(0.4, 0.4) is U-shaped** — it concentrates mass near 0 and near 1 and almost never returns a
  mid value. So a burst is a sequence of *hard* on/off/on steps, not a mushy fade.

Both properties die on the room: a 40 ms burst is 0.2–0.4 of a single BLE sample, so the strips would
either miss it entirely or catch one frame of it at random.

### P3 — `PLASMA`
The demoscene sum-of-sines, evaluated at a single moving point (since the PC is one zone).

```
p(t) = sin(ω₁t) + sin(ω₂t + sin(ω₃t)) + sin(ω₄t)·cos(ω₅t)        # p ∈ [-3, 3]
ω = 2π · (0.31, 0.47, 0.11, 0.73, 0.29) · speed  rad/s
h(t) = (h₀ + 0.5·p/3) mod 1        # or index PALETTES[state.palette] by (p+3)/6
v(t) = 0.55 + 0.35·sin(2π·0.19·t + p)
s = 0.95
```

The five rates are mutually irrational, so the trajectory is dense on a 5-torus and never exactly
repeats — a genuinely non-looping liquid colour field. This is the pattern that most directly
justifies 30 Hz: `p(t)` has meaningful structure up to ~0.7 Hz *in each of five superposed terms*,
and the composite crosses the room's Nyquist floor.

Good candidate to read `state.palette` and reuse `PALETTES` from `effects/palettes.py` rather than
raw hue.

### P4 — `COMET` (golden-angle impulse train)
Exponentially-decaying flashes, each a maximally-distinct colour from the last.

```
fire at f = 3.0 Hz · speed
on fire:   h += 0.381966          # 1/φ² — the golden angle on the hue wheel
           t_last = t
always:    v = exp(-(t - t_last)/τ),  τ = 0.09 s
```

Golden-angle stepping is the phyllotaxis trick: for *any* number of consecutive flashes the hues are
as evenly spread around the wheel as possible, so the sequence never falls into a visible repeating
colour cycle the way a fixed increment does.

Hard-blocked on the room by arithmetic: at 3 Hz there are 3.3 BLE samples between flashes and the
90 ms decay tail is 0.9 of one sample. The room would show three unrelated colours stuttering.

### P5 — `HEARTBEAT`
Double-Gaussian pump — a *lub-dub*, not a pulse.

```
T = 60 / BPM,  BPM = 72 · speed
τ = t mod T
v(τ) = G(τ, σ₁) + 0.62·G(τ - 0.19, σ₂)      G(x,σ) = exp(-x²/(2σ²))
σ₁ = 0.045 s, σ₂ = 0.055 s
```

Evaluate the first Gaussian at both `τ` and `τ - T` so the wrap is continuous.

σ₁ = 45 ms gives an FWHM of ~106 ms — **3.2 frames on the PC, 1.06 frames on the room.** This is the
cleanest single demonstrator of why the split exists: the pattern is not merely *worse* on the
strips, it is not representable there at all.

---

## 4. Architecture

### 4.1 One catalog, capability-tagged — not a second state axis

Patterns become new members of the existing `Mode` enum (`effects/modes.py`), **not** a parallel
`room_pattern` / `pc_pattern` field. The engine already dispatches per-target on `Mode`, the UI
already has two independent mode banks, and `_reset_target` already exists — a second orthogonal
axis would duplicate all of it and break the `share` fast path for no gain.

What *does* change is that mode metadata stops being scattered across three module-level frozensets
(`_UNIFIED_MODES`, `_ROOM_SMOOTHED_MODES`, `_COLOR_DRIVEN_MODES`, `effects/engine.py:40-70`) and one
`if/elif` chain (`_compute_mode`, `effects/engine.py:293-307`), and becomes a single registry.

**New file `effects/patterns.py`** — the nine render functions, each a pure function of
`(now, dt, ctx)` where `ctx` carries that target's base colour, speed, palette and mutable
`_PatternState`. No engine access, so they are unit-testable standalone.

**New file `effects/registry.py`** — the catalog:

```python
@dataclass(frozen=True)
class ModeSpec:
    mode: Mode
    label: str
    render: Callable[..., Dict[str, Tuple[int, int, int]]]
    room_ok: bool = True          # may be assigned to the BLE target
    pc_ok: bool = True
    color_driven: bool = False    # reads that target's picker
    speed_driven: bool = False
    palette_driven: bool = False
    stochastic: bool = False      # never share a render between targets
    room_smoothing: str = "none"  # "none" | "audio" | "screen"

SPECS: Dict[Mode, ModeSpec] = {...}   # the 7 existing modes + the 9 new ones
```

The seven existing modes get entries that reproduce today's behaviour exactly — e.g. `MUSIC` is
`room_smoothing="audio"`, `AUDIO_REACTIVE` likewise, `SCREEN_SYNC` is `"screen"`, `STATIC`/`PULSE`/
`AMBIENT` are `color_driven=True`. The frozensets and the `if/elif` chain are then deleted, and
`_compute_mode` becomes a dict lookup, `_route` reads `spec.room_smoothing`.

`_ROOM_SMOOTHED_MODES` is currently defined but never read — it dies with this refactor.

### 4.2 Routing Room and PC simultaneously

**This already works and needs no new mechanism.** `EffectEngine.run` calls `_compute_mode` twice
with different `target` strings (`effects/engine.py:237-243`), and `_route` already takes both
frames and sends them down two independent paths. Three additions:

1. **Per-target pattern state**, mirroring the existing `_dsp: Dict[str, _AudioDSP]`
   (`effects/engine.py:168-171`) — the file already documents why sharing DSP state between targets
   was a bug (`_AudioDSP` docstring, lines 125-137), and the same reasoning applies verbatim to
   phase accumulators and RNG streams:

   ```python
   self._pattern: Dict[str, _PatternState] = {"room": _PatternState(), "pc": _PatternState()}
   ```

   Cleared per-target in `_reset_target` (`effects/engine.py:531-540`), which already does exactly
   this for `_dsp`.

2. **Guard PC-only modes at the command layer.** `_apply_command`'s `SET_MODE` branch already
   silently returns on an unparseable mode (`effects/engine.py:460-463`); extend that to reject a
   mode whose spec disallows the requested target, leaving state untouched. `PUT /api/mode` returns
   400 for the same case, alongside the existing invalid-mode and invalid-target checks
   (`web/routes.py:53-60`).

3. **Widen the `share` fast path.** Today it is `room_mode == pc_mode and (not color_driven or
   colors equal)` (`effects/engine.py:223-226`). Once patterns are speed- and palette-driven that
   test is wrong — two targets on `BREATHE` at different speeds would silently render one frame and
   paint both. Replace with a render key:

   ```python
   def _render_key(self, target):
       spec = SPECS[self._mode_for(target)]
       if spec.stochastic:
           return object()          # never share: keeps the two RNG streams honest
       return (spec.mode,
               self._static_color_for(target) if spec.color_driven  else None,
               self._speed_for(target)        if spec.speed_driven  else None,
               self._palette_for(target)      if spec.palette_driven else None)

   share = self._render_key("room") == self._render_key("pc")
   ```

   Excluding stochastic modes from sharing costs one extra render per tick and removes a whole class
   of stale-state bugs (a shared render advances only one target's RNG; unlinking later resumes the
   other from cold state).

### 4.3 Per-target parameters

`speed` and `palette` are currently single global fields (`effects/engine.py:86-87`) — a leftover
from when there was one mode. With independent patterns they are an active conflict: Room `BREATHE`
at 0.3× and PC `STROBE` at 3× cannot both exist.

**Decision: split both**, the same way the colour pickers were already split — the codebase's own
established precedent (`room_static_color` / `pc_static_color` + the Link Colors checkbox):

- `AppState` gains `room_speed` / `pc_speed` **and** `room_palette` / `pc_palette`, with accessors
  `_speed_for(target)` / `_palette_for(target)` alongside the existing `_static_color_for`.
- `SET_SPEED` / `SET_PALETTE` gain the **same `target` grammar** as `SET_STATIC_COLOR` — `"room"` /
  `"pc"` / absent-means-`"both"` (`effects/engine.py:486-498`). Every existing caller keeps working
  untouched, exactly as `SET_MODE`'s target parameter did.
- `GET /api/state` keeps `speed` / `palette` as aliases for the room's value, the same way `mode` is
  already kept as an alias for `room_mode` (`web/routes.py:36-38`).

### 4.4 Config

New `[patterns]` section in `config.toml`, one Pydantic model per pattern group in `config.py`.
**Every field must carry a default** — `AudioReactiveConfig`'s docstring makes this a house rule
("a missing TOML key is a startup crash", `config.py:29-30`). Constants that belong here: R1's `f`,
`γ`, `v_min`; R2's `f`, `k`; R3's `f₁`, `f₂`; R4's `μ`, `τ`, `σ`; P1's `duty` and Hz ceiling; P2's
`λ`, burst bounds, Beta shape; P3's five ω; P4's `f`, `τ`; P5's BPM and σ.

### 4.5 UI

`index.html` currently hard-codes the same seven `<button>`s twice (`web/static/index.html:40-46`
and `54-60`), and `app.js` hard-codes `MODE_LABELS` and `COLOR_DRIVEN` (`app.js:35-48`). Sixteen
modes duplicated across two grids is not maintainable, and the Room grid must not show PC-only
patterns at all.

**Decision: server-driven catalog, grouped Sources + Patterns.**

- **Serve the catalog.** Add `GET /api/modes` returning the registry as JSON (mode, label, group,
  `room_ok`, `pc_ok`, `color_driven`, `speed_driven`, `palette_driven`). `app.js` builds both mode
  banks from it and filters each by `room_ok` / `pc_ok`, deleting the duplicated markup and the
  hard-coded `MODE_LABELS` / `COLOR_DRIVEN` constants — `updateDerivedUI` (`app.js:315-331`) then
  reads capability flags from the server instead of a local `Set` that can drift. This is the change
  that makes the whole feature maintainable: today a new mode has to be added in eight places
  (enum, dispatch, two frozensets, two `<button>`s, `MODE_LABELS`, `COLOR_DRIVEN`); after it, one.
- **Grouping.** *Sources* (Static, Screen Sync, Audio Reactive, Music) stay a button grid — they are
  the things you switch between constantly. *Patterns* become an `<optgroup>` dropdown beneath them,
  labelled by group so the PC bank shows both sections and the Room bank only shows Room-safe ones:

  ```
  Room Mode                    Breathe
    Sources  [Static][Screen Sync][Audio Reactive][Music]
    Pattern  ( Breathe                    v )
               ── Room ──  Breathe · Phase · Tide · Ambient (Ember)

  PC Mode                      Plasma
    Sources  [Static][Screen Sync][Audio Reactive][Music]
    Pattern  ( Plasma                     v )
               ── Room ──  Breathe · Phase · Tide · Ambient (Ember)
               ── PC ──    Strobe · Flicker · Plasma · Comet · Heartbeat
  ```

  Selecting a Source clears the Pattern dropdown and vice versa — they are one `Mode` field, so the
  two controls are two views of the same value and `setModeUI` drives both.
- **Speed and palette live with the mode.** Move both controls into each mode bank, one per target,
  each with a Link checkbox mirroring the existing Custom Colors block (`index.html:88-112`). Each
  is shown only when that target's spec declares `speed_driven` / `palette_driven` — a generalisation
  of the palette selector's existing "hidden unless a target is on WAVE" toggle (`app.js:316-317`).
- Both new controls go over the websocket first with a REST fallback, the pattern every draggable
  control already uses (`app.js:213-226`); `ALLOWED_COMMANDS` in `web/ws.py:14-25` already lists
  `SET_SPEED` and `SET_PALETTE`, so no change there. (That set has `SET_ROOM_MODE` / `SET_PC_MODE`
  duplicated — worth tidying while we're in the file.)

---

## 5. Files touched

| File | Change |
|---|---|
| `effects/patterns.py` | **new** — the 9 render functions + `_PatternState` |
| `effects/registry.py` | **new** — `ModeSpec` + `SPECS` catalog |
| `effects/modes.py` | 8 new `Mode` members (`AMBIENT` is reused, not added) |
| `effects/engine.py` | `_compute_mode` → registry lookup; `_route` reads `spec.room_smoothing`; delete the 3 frozensets; `_pattern` per-target state; `_render_key` share test; per-target speed/palette in `AppState` + accessors + `_apply_command` |
| `config.py`, `config.toml` | `PatternsConfig` with defaults for every field |
| `web/routes.py` | `GET /api/modes`; per-target validation on `PUT /api/mode`; target grammar on speed/palette; expose the new per-target state in `GET /api/state` |
| `web/ws.py` | dedupe `ALLOWED_COMMANDS` |
| `web/static/index.html`, `app.js` | catalog-driven mode banks, grouped Sources/Patterns, per-target speed + palette |

**Explicitly out of scope:** persistence. There is none today — `config.toml` is load-only
(`config.py:133-142`) and never written back, so every UI change is already lost on restart and the
app reboots to `[general] mode = "STATIC"`. Patterns inherit that; making state durable is a
separate piece of work and shouldn't be smuggled in here.

## 6. Verification

1. **Headless tick harness** (extend the `test_ble.py` precedent): instantiate `EffectEngine` with
   stub BLE manager / screen sampler / audio queue, drive N ticks at fixed `dt`, capture frames.
2. **Band-limit assertion for room patterns** — decimate the 30 Hz output to 5 Hz (worst case) and
   assert max per-sample ΔV ≤ 0.05 and Δhue ≤ 0.02 for R1–R4. This is the test that would have
   caught the aliasing problem by construction; it should *fail* if you point it at `STROBE`.
3. **Cross-target isolation** — set Room and PC to the same pattern at different speeds, assert the
   two frame streams differ (catches a `share` regression); set both to `FLICKER`, assert the two
   RNG streams diverge.
4. **Guard test** — `SET_ROOM_MODE` with `HEARTBEAT` leaves `state.room_mode` unchanged;
   `PUT /api/mode {mode: "HEARTBEAT", target: "room"}` returns 400; `GET /api/modes` never lists a
   PC-only pattern with `room_ok: true`.
4b. **Regression on the existing seven** — the registry entries must reproduce today's behaviour
   exactly. Assert `MUSIC` and `AUDIO_REACTIVE` still route through the audio taus, `SCREEN_SYNC`
   through the screen taus, and that `STATIC` on both targets with *different* picker colours still
   produces two different frames (the case the current `share` test exists to protect).
5. **Frame-rate independence** — run the same pattern at `tick_hz` 30 and 60, assert the colour at a
   given wall-clock time matches within tolerance. Every rate above is expressed per-second and every
   filter uses the `1 - exp(-dt/τ)` idiom already used throughout `engine.py`, so this should hold.
6. **Live** — `python main.py`, open the dashboard, and use the existing websocket visualiser
   (`app.js:131-159`, which already previews the three strips and the PC separately) to eyeball
   Room `EMBER` + PC `FLICKER` running simultaneously. That single combination exercises the whole
   feature: two stochastic patterns, two time constants, two targets, no sharing.

## 7. Reuse

- `utils/color.py` — `hsv_to_rgb`, `rgb_to_hsv`, `clamp01`, `apply_brightness` (brightness is applied
  once in `_route`; patterns must **not** apply it themselves)
- `effects/engine.py:103` — `_hue_lerp`, shortest-path-around-the-wheel interpolation
- `effects/topology.py` — `STRIP_MIDPOINTS` for R1's phase offset, R2 and R3's spatial term
- `effects/palettes.py` — `PALETTES` for P3
- `effects/engine.py:563-566` — `_static_color_for(target)`, the per-target picker accessor
- The `a = 1 - exp(-dt/τ)` dt-correct one-pole used in `_smooth_strip` and `_follow`
