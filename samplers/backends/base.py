"""The two capture contracts, and the vocabulary for a backend that cannot run.

RoomSync needs exactly two things from the host operating system: the audio
that is currently playing, and the pixels that are currently on screen. Both
are supplied by APIs that do not exist on every platform, are not always
permitted, and can vanish while the app is running (an audio device is
unplugged, a laptop lid closes and takes the display with it).

Until now both were hard-coded to a single Windows implementation and imported
at module scope, which means the honest description of RoomSync on Linux was
"crashes on import". These protocols are the seam that fixes that: a backend is
selected at runtime, its absence is a REPORTED CAPABILITY rather than an
exception, and a mode that needs a capability the host does not have is greyed
out in the UI instead of being a button that kills the tick loop.

Two exception types, and the distinction between them matters:

  BackendUnavailable  this backend cannot run here AT ALL — wrong platform, the
                      library is not installed, no loopback device exists. It
                      is asked once, at startup, and the answer is a capability
                      flag. Not an error; a fact about the machine.
  CaptureError        the backend was working and one specific read failed. The
                      sampler retries. A display-mode change, a device reset,
                      an audio buffer overrun.

Anything that raises BackendUnavailable during `probe()` or `open()` must leave
nothing behind to clean up — that is what makes probing safe to do at startup
against every backend in turn.
"""

from typing import Protocol, Tuple, runtime_checkable

import numpy as np


class BackendUnavailable(RuntimeError):
    """This backend cannot run on this machine. Carries a human-readable reason.

    The reason string is shown to the user — it becomes the tooltip on the
    greyed-out mode button — so it must say what is missing and, where there is
    one, what to do about it. "No WASAPI loopback device" is a good reason;
    "OSError" is not.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CaptureError(RuntimeError):
    """One capture attempt failed. The backend is still considered usable."""


# ── Capability names ─────────────────────────────────────────────────
# The strings the mode registry gates on. Deliberately coarse: a mode needs
# "the audio that is playing" or "the pixels on screen", and does not care
# which of WASAPI, PipeWire or CoreAudio is behind it.
CAP_AUDIO = "audio"
CAP_SCREEN = "screen"


@runtime_checkable
class LoopbackSource(Protocol):
    """A source of the audio currently being PLAYED, not recorded.

    Loopback, not a microphone: the whole point is to react to what the machine
    is outputting. Every implementation must deliver mono float32 in roughly
    [-1, 1], whatever the device's native channel count — mixdown is the
    backend's job, because only the backend knows the channel layout.
    """

    name: str
    sample_rate: int
    channels: int

    def open(self) -> None:
        """Acquire the device. Raises BackendUnavailable if it cannot."""

    def read(self, frames: int) -> np.ndarray:
        """Block for `frames` samples and return them as mono float32.

        Raises CaptureError on a recoverable read failure.
        """

    def close(self) -> None:
        """Release the device. Must be safe to call twice, and after a failure."""


@runtime_checkable
class ScreenSource(Protocol):
    """A source of the pixels currently on the primary display.

    `grab()` returns a full frame as a uint8 array in BGR(A) order with shape
    (h, w, >=3) — BGRA because that is what every platform's fast path actually
    hands over, and converting it here would mean touching every pixel for no
    reason. The sampler slices edge regions out of it and never looks at the
    fourth channel.
    """

    name: str

    def open(self) -> None:
        """Acquire the capture handle. Raises BackendUnavailable if it cannot."""

    def grab(self) -> np.ndarray:
        """Capture one frame. Raises CaptureError on a recoverable failure."""

    def size(self) -> Tuple[int, int]:
        """(width, height) of the captured region."""

    def close(self) -> None:
        """Release the handle. Must be safe to call twice."""
