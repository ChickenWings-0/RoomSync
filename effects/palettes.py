from typing import List, Tuple
from utils.color import hsv_to_rgb

def generate_rainbow_palette(steps: int = 256) -> List[Tuple[int, int, int]]:
    """Generates a full rainbow spectrum."""
    return [hsv_to_rgb(i / steps, 1.0, 1.0) for i in range(steps)]

def generate_sunset_palette(steps: int = 256) -> List[Tuple[int, int, int]]:
    """Generates a sunset gradient (deep red to bright orange/yellow)."""
    # Hue ranges roughly from 0.0 (red) to 0.15 (yellow)
    return [hsv_to_rgb(0.15 * (i / steps), 1.0, 1.0) for i in range(steps)]

def generate_ocean_palette(steps: int = 256) -> List[Tuple[int, int, int]]:
    """Generates an ocean gradient (deep blue to cyan)."""
    # Hue ranges roughly from 0.65 (blue) to 0.5 (cyan)
    return [hsv_to_rgb(0.65 - 0.15 * (i / steps), 1.0, 1.0) for i in range(steps)]

PALETTES = {
    "rainbow": generate_rainbow_palette(),
    "sunset": generate_sunset_palette(),
    "ocean": generate_ocean_palette(),
}
