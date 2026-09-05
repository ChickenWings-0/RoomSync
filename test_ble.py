"""Standalone BLE smoke test — no engine, no audio, no web server.

Two things this has to respect about the current BLE architecture:

1. `BLEManager.set_color()` is SYNCHRONOUS (`ble/manager.py:34`). It only
   overwrites the worker's `_target_color`; the worker's own 10 Hz transmit
   loop does the actual GATT write. Awaiting it raises TypeError.
2. `BLEManager.create()` returns immediately — workers connect in the
   background so the app boots instantly with strips offline. Setting a
   colour before a worker is connected is a no-op, so we wait first.
"""

import asyncio
from config import Settings
from ble.manager import BLEManager

CONNECT_TIMEOUT = 30.0
HOLD_S = 2.0            # >> the worker's 0.1 s transmit interval


async def _wait_for_connections(ble_mgr: BLEManager, timeout: float) -> list[str]:
    """Poll the workers until each is connected or the timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    workers = ble_mgr._workers          # test-only introspection
    while asyncio.get_running_loop().time() < deadline:
        connected = [n for n, w in workers.items() if w._connected]
        if len(connected) == len(workers):
            return connected
        await asyncio.sleep(0.5)
    return [n for n, w in workers.items() if w._connected]


async def _flash(ble_mgr: BLEManager, names: list[str], label: str, rgb: tuple[int, int, int]):
    print(f"Turning strips {label}...")
    for name in names:
        ble_mgr.set_color(name, *rgb)   # SYNCHRONOUS — do not await
    await asyncio.sleep(HOLD_S)


async def main():
    settings = Settings()

    print("Starting strip workers (they connect in the background)...")
    ble_mgr = await BLEManager.create(settings.ble.strips)

    print(f"Waiting up to {CONNECT_TIMEOUT:.0f}s for connections...")
    connected = await _wait_for_connections(ble_mgr, CONNECT_TIMEOUT)

    all_names = [s.name for s in settings.ble.strips]
    missing = [n for n in all_names if n not in connected]
    if missing:
        print(f"  WARNING: not connected: {', '.join(missing)}")
    if not connected:
        print("  No strips connected — nothing to test.")
        await ble_mgr.disconnect_all()
        return
    print(f"  Connected: {', '.join(connected)}")

    try:
        await _flash(ble_mgr, connected, "RED", (255, 0, 0))
        await _flash(ble_mgr, connected, "GREEN", (0, 255, 0))
        await _flash(ble_mgr, connected, "BLUE", (0, 0, 255))
        await _flash(ble_mgr, connected, "OFF", (0, 0, 0))
    finally:
        print("Disconnecting...")
        await ble_mgr.disconnect_all()

    print("BLE smoke test complete.")


if __name__ == "__main__":
    asyncio.run(main())
