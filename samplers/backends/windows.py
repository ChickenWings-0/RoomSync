"""Windows capture backends: WASAPI loopback for audio, mss for the screen.

Both were previously inlined into the samplers with their imports at module
scope, so `import samplers.audio` on a machine without PyAudioWPatch was an
ImportError during app startup rather than a missing feature. Here the imports
are deferred into `probe()`/`open()`, which is what lets the backend registry
ask "can this run?" and get an answer instead of a traceback.

mss is not actually Windows-only — it speaks X11 too — but it lives here
because this is the module that is tried on Windows, and the registry's Linux
entry names it separately so its probe failure reads as an X11/Wayland problem
rather than a Windows one.
"""

from typing import Optional, Tuple

import numpy as np
import structlog

from .base import BackendUnavailable, CaptureError

logger = structlog.get_logger()


class WasapiLoopback:
    """The Windows loopback capture path, via PyAudioWPatch.

    PyAudioWPatch rather than stock PyAudio: loopback capture is a WASAPI
    feature that upstream PyAudio does not expose at all, which is the entire
    reason for the fork.
    """

    name = "wasapi-loopback"

    def __init__(self):
        self._pa = None
        self._stream = None
        self.sample_rate = 0
        self.channels = 0

    @staticmethod
    def _import():
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:
            raise BackendUnavailable(
                "PyAudioWPatch is not installed (pip install PyAudioWPatch)."
            ) from exc
        return pyaudio

    @classmethod
    def probe(cls) -> None:
        """Can this machine do loopback capture? Raises BackendUnavailable if not.

        Fully opens and closes a PyAudio handle: the import succeeding proves
        nothing, since a machine can have the library and no loopback device —
        a headless VM, or a box with every audio endpoint disabled.
        """
        import sys

        if not sys.platform.startswith("win"):
            raise BackendUnavailable(
                "WASAPI loopback is Windows-only. On Linux this needs a "
                "PipeWire/PulseAudio monitor source; on macOS, a virtual "
                "output device such as BlackHole."
            )
        pyaudio = cls._import()
        pa = pyaudio.PyAudio()
        try:
            if cls._find_device(pa, pyaudio) is None:
                raise BackendUnavailable(
                    "No WASAPI loopback device found. Check that an audio "
                    "output device is enabled and not exclusively held."
                )
        finally:
            pa.terminate()

    @staticmethod
    def _find_device(pa, pyaudio) -> Optional[dict]:
        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return None
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and dev["hostApi"] == wasapi["index"]:
                return dev
        return None

    def open(self) -> None:
        pyaudio = self._import()
        self._pa = pyaudio.PyAudio()
        try:
            dev = self._find_device(self._pa, pyaudio)
            if dev is None:
                raise BackendUnavailable("No WASAPI loopback device found.")

            self.sample_rate = int(dev["defaultSampleRate"])
            self.channels = int(dev["maxInputChannels"])
            # frames_per_buffer is set per-read rather than here: the analyser
            # owns the FFT size, and a mismatch between the two would make
            # every read a partial buffer.
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=dev["index"],
            )
        except BaseException:
            # Leave nothing behind on a failed open — probing and retrying both
            # depend on this being clean.
            self.close()
            raise
        logger.info("WASAPI loopback opened", device=dev["name"],
                    sample_rate=self.sample_rate, channels=self.channels)

    def read(self, frames: int) -> np.ndarray:
        if self._stream is None:
            raise CaptureError("stream is not open")
        try:
            raw = self._stream.read(frames, exception_on_overflow=False)
        except Exception as exc:
            raise CaptureError(str(exc)) from exc

        data = np.frombuffer(raw, dtype=np.float32)
        # Mixdown here rather than in the analyser: only the backend knows the
        # device is interleaved and how many channels it has.
        if self.channels > 1:
            usable = (data.shape[0] // self.channels) * self.channels
            data = data[:usable].reshape(-1, self.channels).mean(axis=1)
        return data

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


class MssCapture:
    """Screen capture via mss. Fast on Windows (BitBlt) and X11 (SHM).

    One mss instance, held open across grabs. mss is explicitly NOT
    thread-safe, so the instance is created inside `open()` and every call must
    come from the same thread — which the ScreenSampler guarantees by pinning
    its executor to a single worker.
    """

    name = "mss"

    def __init__(self, monitor_index: int = 1):
        self._sct = None
        self._monitor = None
        self._monitor_index = monitor_index

    @staticmethod
    def _import():
        try:
            import mss
        except ImportError as exc:
            raise BackendUnavailable("mss is not installed (pip install mss).") from exc
        return mss

    @classmethod
    def probe(cls) -> None:
        mss = cls._import()
        try:
            with mss.mss() as sct:
                if len(sct.monitors) < 1:
                    raise BackendUnavailable("No displays were found.")
        except BackendUnavailable:
            raise
        except Exception as exc:
            # The Wayland case lands here: mss imports fine and then cannot
            # open a display connection at all.
            raise BackendUnavailable(
                "Screen capture is unavailable (%s). Under Wayland this needs "
                "a portal-based backend; X11 and Windows work as-is." % exc
            ) from exc

    def open(self) -> None:
        mss = self._import()
        try:
            self._sct = mss.mss()
            monitors = self._sct.monitors
            # monitors[0] is the union of every display; [1] is the primary.
            idx = self._monitor_index if len(monitors) > self._monitor_index else 0
            self._monitor = monitors[idx]
        except BaseException:
            self.close()
            raise
        logger.info("Screen capture opened", backend=self.name, monitor=self._monitor)

    def grab(self) -> np.ndarray:
        if self._sct is None:
            raise CaptureError("capture is not open")
        try:
            return np.asarray(self._sct.grab(self._monitor))
        except Exception as exc:
            # A resolution change, a display going to sleep, an RDP session
            # detaching. Recoverable: the sampler retries on the next tick.
            raise CaptureError(str(exc)) from exc

    def size(self) -> Tuple[int, int]:
        if self._monitor is None:
            return (0, 0)
        return (self._monitor["width"], self._monitor["height"])

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
        self._monitor = None
