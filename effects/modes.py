from enum import Enum

class Mode(str, Enum):
    # ── Sources: driven by external content ───────────────────────
    STATIC = "STATIC"
    SCREEN_SYNC = "SCREEN_SYNC"
    AUDIO_REACTIVE = "AUDIO_REACTIVE"
    MUSIC = "MUSIC"

    # ── Room patterns: slow, continuous, band-limited ─────────────
    # Safe on the BLE strips AND on the PC. Every one of these is a
    # clock-driven mathematical function whose fundamental sits far below
    # the radio's worst-case 2.5 Hz Nyquist floor (see effects/patterns.py).
    PULSE = "PULSE"
    WAVE = "WAVE"
    BREATHE = "BREATHE"
    PHASE = "PHASE"
    TIDE = "TIDE"
    AMBIENT = "AMBIENT"        # Ember: Ornstein-Uhlenbeck hearth drift
    RAINBOW = "RAINBOW"        # full-spectrum cycle, the whole room in unison
    SHADES = "SHADES"          # monochromatic drift within the picked colour

    # ── Room-exclusive patterns: spatial, and only meaningful on 3 pixels ──
    # pc_ok=False is the mirror of room_ok=False, and just as physical: the PC
    # is ONE logical zone, so `_route` collapses a per-strip frame through
    # `_dominant_color`. A pattern whose content IS the difference between the
    # strips averages to a flat colour there -- not worse, absent.
    LIGHTHOUSE = "LIGHTHOUSE"  # a rotating beam over the three spatial pixels

    # ── PC-exclusive patterns: sharp, fast, sub-100 ms features ───
    # Refused on the room target by the registry, not by convention: at a
    # 5-10 Hz delivered frame rate these do not merely look worse, they are
    # not representable. See ModeSpec.room_ok in effects/registry.py.
    STROBE = "STROBE"
    FLICKER = "FLICKER"
    PLASMA = "PLASMA"
    COMET = "COMET"
    HEARTBEAT = "HEARTBEAT"
