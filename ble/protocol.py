def build_color_payload(r: int, g: int, b: int) -> bytes:
    """
    Builds the GATT payload to set a specific RGB color for MELK-OA10.
    Format: 0x7E 0x07 0x05 0x03 R G B 0x10 0xEF
    """
    # Ensure RGB values are clamped between 0 and 255
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return bytes([0x7E, 0x07, 0x05, 0x03, r, g, b, 0x10, 0xEF])

def build_power_payload(on: bool) -> bytes:
    """
    Builds the GATT payload to turn the strip on or off.
    Format: 0x7E 0x07 0x04 <state> 0x00 0x00 0x00 0xFF 0x00 0xEF
    """
    state = 0x01 if on else 0x00
    return bytes([0x7E, 0x07, 0x04, state, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF])
