# Standalone Animation Patterns — Plan

Non-audio, non-screen animations, assignable independently to Room and PC.
Everything below is driven purely by `now` (monotonic clock) plus `state.speed`,
`state.palette`, and the per-target static colour — no sampler input at all.

---

## 1. Why two separate pattern families

The two outputs are not the same instrument, and a shared pattern list would be
a lie in both directions:

| | Room (BLE) | PC (OpenRGB) |
|---|---|---|
| Transport | 2.4 GHz radio, 3 strips | loopback socket, 1 zone |
| Practical rate | ~10 Hz ceiling | 30 Hz+, unthrottled |
| Spatial | 3 zones (back / left / right) | 1 |
| Failure mode | fast changes coalesce into smear | none worth naming |
| Reads best as | slow, continuous, unified | sharp, transient, discontinuous |

So: **Room patterns are continuous functions of time** (no jumps). **PC
patterns are allowed — encouraged — to be discontinuous.** A strobe on the
strips is a stutter; a strobe on the PC is a strobe.

Both families pass through `_route()` unchanged. Room patterns are clock-driven
like WAVE/PULSE, so they take the **untouched** branch — no low-pass. Their
smoothness comes from the maths, not from a filter, which is the right place
for it: the filter exists to tame content we do not control, and we fully
control these.

---

## 2. Room patterns (slow, ambient, continuous)

### 2.1 `BREATHE`
The room's baseline idle state. Hue fixed to the room colour picker; brightness
is a raised sine:

```
v = v_min + (v_max - v_min) * (0.5 - 0.5 * cos(2π * f * t))
f = 0.08 * speed        # ~12 s per full breath at speed 1.0
```

All three strips in lockstep. `cos` rather than `sin` so the cycle starts at the
trough — switching into BREATHE fades up rather than snapping to full.
Saturation is held constant, so the bottom of the breath is *dim orange*, not
*grey* — the same reasoning as `_smooth_strip`'s hue/sat hold.

**Params:** picker colour, `speed`, depth constant (`v_min` ≈ 0.15).

### 2.2 `PHASE`
Uniform slow drift around the colour wheel. One hue for the whole room:

```
h = (t * 0.02 * speed) % 1.0        # ~50 s per revolution at speed 1.0
s = 1.0
```

The obvious companion to BREATHE, and the one that most rewards the 10 Hz
ceiling: at this rate the strips receive far more distinct hues per revolution
than the eye resolves. Optionally constrained to a palette arc (see 2.4) rather
than the full wheel.

### 2.3 `DRIFT`  (recommended: the best of the four)
BREATHE and PHASE composed, plus a **per-strip phase offset** so the room stops
being a single lamp. Each strip's spatial position from `STRIP_MIDPOINTS`
becomes a phase shift:

```
for name, t_mid in STRIP_MIDPOINTS.items():
    h = (t * 0.02 * speed + t_mid * spread) % 1.0          # spread ≈ 0.25
    v = v_min + depth * (0.5 - 0.5*cos(2π*0.08*speed*t + t_mid*2π*0.3))
```

Back / left / right sit at slightly different points of the same slow cycle, so
the room has depth and a direction of travel without anything ever moving fast.
This is the one that actually gets left on.

### 2.4 `PALETTE_TIDE`
PHASE, but sampling `PALETTES[state.palette]` with **interpolation between
adjacent entries** rather than the nearest-index quantisation `_compute_wave`
currently does:

```
pos = (t * 0.05 * speed) % 1.0
idx = pos * len(palette)
c   = lerp_rgb(palette[floor(idx)], palette[(floor(idx)+1) % len], frac(idx))
```

Gives the existing palettes an ambient, non-spatial reading — and the extracted
`lerp_rgb` fixes the visible stepping in WAVE as a side effect.

---

## 3. PC patterns (sharp, fast, PC-exclusive)

These are the reason the two families are split. Each is explicitly *unsuitable*
for the strips.

### 3.1 `STROBE`
Hard square wave, no ramp:

```
on = ((t * rate * speed) % 1.0) < duty      # rate ≈ 6 Hz, duty ≈ 0.15
color = picker if on else BLACK
```

Duty-cycled short so it reads as a flash, not a flicker. Needs a rate ceiling
(cap ≈ 12 Hz even at max speed) — a genuine photosensitivity consideration, and
the cap belongs in the pattern, not the UI.

### 3.2 `CYBERPUNK_FLICKER`  (recommended: the signature PC pattern)
A failing neon sign. Per-tick randomised state machine, three outcomes:

```
r = random()
if   r < 0.02: DROPOUT for 1-3 ticks     # near-black
elif r < 0.06: SURGE   for 1-2 ticks     # 1.4x overdrive, clamped
else:          NOMINAL                   # base + small noise on v
```

Base colour alternates between two picker-adjacent hues (`h` and `h ± 0.5` is a
cheap, good default — magenta/cyan if the picker is magenta). At 30 Hz the
dropouts are ~30–100 ms, exactly the length that reads as *electrical fault*
rather than *animation*. At 10 Hz on the strips it would read as random
blinking, which is why this one never goes to the room.

### 3.3 `SCANLINE`
CRT roll. A narrow bright band sweeps through a dim base, fast:

```
pos = (t * 1.5 * speed) % 1.0
d   = min(|phase - pos|, 1 - |phase - pos|)          # phase = 0.5 on one zone
v   = v_base + (1 - v_base) * exp(-d² / 2σ²)         # σ ≈ 0.05
```

On one zone this is a fast repeating swell, deliberately at a rate the strips
could not carry. Generalises for free if PC zones are ever split.

### 3.4 `GLITCH_SHIFT`
Holds a stable colour, then on a Poisson-ish trigger (~1 event / 2 s) jumps to a
**random hue at full saturation** for 2–5 ticks before snapping back. No
interpolation on either edge — the discontinuity *is* the effect. Optionally
tears: R, G and B each take an independent 1-tick delay during the event.

### 3.5 `SPECTRUM_RUSH`
PHASE at 20–40× the room's rate — a full wheel revolution in ~1.5 s. At 30 Hz
that is smooth; at 10 Hz it aliases into visible colour banding. Same maths as
`PHASE`, different constant, and worth shipping precisely to demonstrate what
the extra bandwidth buys.

---

## 4. Integration

### 4.1 Modes vs. patterns — one decision, taken up front

**Recommendation: add each pattern as a `Mode` enum member, not as a separate
"pattern" axis of state.**

- `_compute_mode()` already dispatches on mode and already receives `target`,
  `now` and `dt`. Every pattern above is a pure function of exactly those.
- `_route()` already classifies modes by set membership (`_UNIFIED_MODES`,
  `_ROOM_SMOOTHED_MODES`, `_COLOR_DRIVEN_MODES`). Patterns slot in as
  set-membership facts, not as new branches.
- A second axis (`mode=PATTERN` + `pattern=BREATHE`) doubles the state, the
  reset paths, the WS payload and the UI's notion of "what is this output
  doing" — for no expressive gain.

```python
class Mode(str, Enum):
    ...
    # ── Room-oriented ambient patterns ──
    BREATHE       = "BREATHE"
    PHASE         = "PHASE"
    DRIFT         = "DRIFT"
    PALETTE_TIDE  = "PALETTE_TIDE"
    # ── PC-exclusive high-rate patterns ──
    STROBE            = "STROBE"
    CYBERPUNK_FLICKER = "CYBERPUNK_FLICKER"
    SCANLINE          = "SCANLINE"
    GLITCH_SHIFT      = "GLITCH_SHIFT"
    SPECTRUM_RUSH     = "SPECTRUM_RUSH"
```

New engine constants:

```python
_PATTERN_MODES = frozenset({BREATHE, PHASE, DRIFT, PALETTE_TIDE,
                            STROBE, CYBERPUNK_FLICKER, SCANLINE,
                            GLITCH_SHIFT, SPECTRUM_RUSH})
_PC_ONLY_MODES = frozenset({STROBE, CYBERPUNK_FLICKER, SCANLINE,
                            GLITCH_SHIFT, SPECTRUM_RUSH})
# Patterns that read the picker join STATIC/PULSE/AMBIENT in defeating the
# render-once fast path when both targets happen to share a mode.
_COLOR_DRIVEN_MODES |= {BREATHE, DRIFT, STROBE, CYBERPUNK_FLICKER,
                        SCANLINE, GLITCH_SHIFT}
```

### 4.2 Routing

Patterns are clock-driven, so they fall through `_route()`'s existing final
`else` branch — brightness applied, no low-pass, filters disarmed. **No change
to `_route()` is required.** That is the strongest argument for this design:
the decoupling already done is exactly the structure these need.

Two additions:

- **Stateful patterns need per-target state.** `CYBERPUNK_FLICKER` and
  `GLITCH_SHIFT` carry an event timer and an RNG. Follow the `_AudioDSP`
  precedent: a `_PatternState` dataclass, one per target in
  `self._pattern: Dict[str, _PatternState]`, cleared by `_reset_target()`.
  Sharing one would make two targets on the same pattern flicker in unison —
  wrong, and unfixable after the fact.
- **Each PC pattern owns its own rate cap**, expressed in Hz against the real
  `dt`, so `state.speed` cannot push STROBE somewhere unsafe.

### 4.3 Commands

**No new command types.** `SET_MODE` / `SET_ROOM_MODE` / `SET_PC_MODE` already
carry a mode string plus a target, and already reject unknown modes by leaving
both outputs untouched. Adding enum members is the entire backend surface for
selection.

The one guard worth adding, in `_apply_command`, is refusing PC-only patterns on
the room:

```python
if target in ("room", "both") and mode not in _PC_ONLY_MODES:
    self.state.room_mode = mode
    self._reset_target("room")
```

So `SET_MODE {mode: STROBE, target: both}` strobes the PC and leaves the room
where it was, rather than aiming 6 Hz at a 10 Hz radio. `PUT /api/mode` returns
400 for an explicit `target=room` plus a PC-only pattern; `both` is tolerated as
a broadcast. `GET /api/state` needs nothing new — it already reports
`room_mode` / `pc_mode` as strings.

### 4.4 UI

Nine more buttons in each `.mode-grid` would swamp them (7 → 16). Instead keep
the grid for the six sampler/colour modes and add **one `<select>` per output
beneath it**, labelled "Pattern":

```html
<div class="mode-grid" id="roomModeGrid" data-target="room"> ...existing... </div>
<select class="pattern-select" data-target="room">
  <option value="">— Pattern —</option>
  <option value="BREATHE">Breathe</option>
  <option value="DRIFT">Drift</option>
  ...
</select>
```

Behaviour in `app.js`:

- The dropdown and the grid are **one selection between them**. Choosing a
  pattern clears `.active` from that grid; clicking a grid button resets the
  dropdown to its placeholder. Both dispatch the same `PUT /api/mode`.
- The room's dropdown carries the room patterns only; the PC's carries both
  lists, with the exclusives under `<optgroup label="PC only">`. The room simply
  never offers what the backend would refuse — the guard in 4.3 is the backstop,
  not the UI.
- On load, `/api/state` returns a mode string; if it is in the pattern set the
  page selects it in the dropdown instead of lighting a grid button. One lookup
  table, shared by both outputs.
- `speed` already exists and becomes the primary pattern control — worth
  relabelling per-mode ("Breath rate", "Flicker rate") from that same table.

### 4.5 Build order

1. `Mode` members + the three frozensets. (No behaviour yet.)
2. `_compute_breathe` / `_compute_phase` / `_compute_drift` /
   `_compute_palette_tide`, plus the `lerp_rgb` helper retrofitted into
   `_compute_wave`.
3. `_PatternState`, then the five PC patterns.
4. The `_PC_ONLY_MODES` guard in `_apply_command` and the 400 in `routes.py`.
5. UI dropdowns and the mode↔control lookup table.

Steps 1–2 are shippable on their own: BREATHE and DRIFT alone are most of the
value here.
