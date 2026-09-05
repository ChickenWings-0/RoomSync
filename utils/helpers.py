import asyncio
import atexit
import queue
import sys
from typing import List, Any

_TIMER_PERIOD_MS = 1
_timer_raised = False


def enable_high_resolution_timer() -> bool:
    """Raise the Windows system timer resolution to 1 ms.

    Windows' default timer granularity is ~15.6 ms, and `asyncio.sleep()` rounds
    UP to the next tick. A 33.3 ms sleep (30 Hz) therefore takes 46.8 ms — the
    engine silently runs at ~21 Hz no matter what `tick_hz` says, and every
    downstream consumer inherits that choppiness.

    This MUST be called before the event loop is created; calling it afterwards
    has no effect on an already-running proactor. Returns True if raised.

    This is the same thing media players and games do. It is process-wide and
    costs a little idle power, which is the correct trade for a real-time
    light-sync app whose entire job is smooth motion.
    """
    global _timer_raised
    if _timer_raised or not sys.platform.startswith("win"):
        return False

    try:
        import ctypes

        if ctypes.windll.winmm.timeBeginPeriod(_TIMER_PERIOD_MS) != 0:
            return False
    except Exception:
        return False

    _timer_raised = True
    atexit.register(_release_high_resolution_timer)
    return True


def _release_high_resolution_timer():
    global _timer_raised
    if not _timer_raised:
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeEndPeriod(_TIMER_PERIOD_MS)
    except Exception:
        pass
    _timer_raised = False

def drain_latest(q) -> Any:
    """Drains a queue and returns only the most recent item, dropping older ones. Returns None if empty."""
    item = None
    while True:
        try:
            item = q.get_nowait()
        except (asyncio.QueueEmpty, queue.Empty, Exception):
            break
    return item

def drain_all(q) -> List[Any]:
    """Drains a queue and returns all items in order."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except (asyncio.QueueEmpty, queue.Empty, Exception):
            break
    return items

def freq_to_bin(freq: float, sample_rate: int, chunk_size: int) -> int:
    """Converts a frequency in Hz to the corresponding FFT bin index."""
    return int(freq * chunk_size / sample_rate)
