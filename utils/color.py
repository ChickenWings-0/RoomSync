import colorsys
from typing import Tuple

def hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
    """Convert HSV (0.0-1.0) to RGB (0-255)."""
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))

def rgb_to_hsv(r: int, g: int, b: int) -> Tuple[float, float, float]:
    """Convert RGB (0-255) to HSV (0.0-1.0). Exact inverse of hsv_to_rgb."""
    return colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)

def clamp(value: int, min_val: int = 0, max_val: int = 255) -> int:
    """Clamps an integer value between min_val and max_val."""
    return max(min_val, min(max_val, value))

def clamp01(x: float) -> float:
    """Clamps a float into the unit interval [0.0, 1.0]."""
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else float(x))

def apply_brightness(color: Tuple[int, int, int], brightness: float) -> Tuple[int, int, int]:
    """Applies a brightness multiplier (0.0-1.0) and clamps the result to 0-255."""
    r = clamp(int(color[0] * brightness))
    g = clamp(int(color[1] * brightness))
    b = clamp(int(color[2] * brightness))
    return (r, g, b)
