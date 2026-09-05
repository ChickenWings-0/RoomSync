import queue
import threading
import time
from typing import NamedTuple, Optional

import janus
import numpy as np
import structlog

from config import settings
from utils.helpers import freq_to_bin
from .backends import BackendUnavailable, CaptureError, open_loopback

logger = structlog.get_logger()


class AudioFrame(NamedTuple):
    bass: float
    mids: float
    highs: float
    ts: float
    flux_bass: float = 0.0
    flux_mids: float = 0.0
    flux_highs: float = 0.0


def _band_rms(mag: np.ndarray, lo: int, hi: int) -> float:
    """RMS magnitude over a bin range, never including the DC bin."""
    lo = max(1, lo)                       # never include DC
    hi = max(lo + 1, hi)
    seg = mag[lo:hi]
    return float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0


class AudioAnalyser(threading.Thread):
    """FFT band energy + spectral flux, from whatever loopback the host offers.

    Still a daemon thread, but no longer ONLY a daemon thread. Daemon status is
    the safety net for a hard kill; it is not a shutdown strategy, because a
    daemon thread is terminated wherever it happens to be — which for this
    thread is usually inside a blocking WASAPI read holding an open device
    handle. The stop Event below is the clean path: the loop checks it every
    buffer, closes the device itself, and the interpreter exits with nothing
    left half-open.
    """

    # A read is `chunk` frames at the device rate — about 21 ms for a 1024
    # chunk at 48 kHz. Two of those plus the close is the realistic worst case,
    # so half a second is generous without making Quit feel slow if the device
    # has genuinely wedged.
    JOIN_TIMEOUT_S = 0.5

    def __init__(self, janus_queue: janus.Queue):
        super().__init__(daemon=True, name="audio-analyser")
        self._q = janus_queue.sync_q

        # Set by stop() from the asyncio thread, read by run() from this one.
        # An Event rather than a bool: it is the one flag that must be visible
        # across threads without a lock, and it is also what makes a future
        # "wait for the thread to notice" possible without a sleep-poll.
        self._stop = threading.Event()

        # Spectral flux state / cached window (no per-frame reallocation)
        self._prev_mag: Optional[np.ndarray] = None
        self._window: Optional[np.ndarray] = None

        self._source = None
        self.available = False
        self.unavailable_reason = ""

    def stop(self, timeout: Optional[float] = None) -> bool:
        """Ask the thread to finish its current buffer and exit. Returns joined.

        Called from the shutdown path on the event loop thread. Never blocks
        for longer than `timeout`: a wedged audio device must not be able to
        hold the whole application open, which is precisely what a bare
        `join()` with no timeout would allow.
        """
        self._stop.set()
        if not self.is_alive():
            return True
        self.join(timeout if timeout is not None else self.JOIN_TIMEOUT_S)
        if self.is_alive():
            # Daemon status now does its actual job: the interpreter will not
            # wait for this thread at exit.
            logger.warning("Audio thread did not stop in time; leaving it to the "
                           "interpreter exit", timeout_s=self.JOIN_TIMEOUT_S)
            return False
        return True

    def run(self):
        try:
            self._source = open_loopback()
        except BackendUnavailable as exc:
            # Not an error, and not a crash: this machine simply has no
            # loopback. The capability was already probed and reported at
            # startup, so the UI has greyed the audio modes out and this line
            # is only the confirmation.
            self.unavailable_reason = exc.reason
            logger.warning("Audio capture unavailable — audio modes are disabled",
                           reason=exc.reason)
            return
        except Exception as exc:
            self.unavailable_reason = str(exc)
            logger.error("Audio capture failed to start", error=str(exc))
            return

        self.available = True
        try:
            self._loop()
        finally:
            self.available = False
            try:
                self._source.close()
            except Exception:
                pass
            logger.info("Audio thread stopped")

    def _loop(self):
        sr = self._source.sample_rate
        chunk = settings.audio.chunk_size

        # Bin indices and window are invariant — hoisted out of the loop.
        bass_start = max(1, freq_to_bin(settings.audio.bands["bass"][0], sr, chunk))
        bass_end = freq_to_bin(settings.audio.bands["bass"][1], sr, chunk)

        mids_start = freq_to_bin(settings.audio.bands["mids"][0], sr, chunk)
        mids_end = freq_to_bin(settings.audio.bands["mids"][1], sr, chunk)

        highs_start = freq_to_bin(settings.audio.bands["highs"][0], sr, chunk)
        highs_end = freq_to_bin(settings.audio.bands["highs"][1], sr, chunk)

        self._window = np.hanning(chunk)

        logger.info("AudioAnalyser started successfully",
                    backend=self._source.name,
                    sample_rate=sr, chunk=chunk,
                    bass_bins=(bass_start, bass_end),
                    mids_bins=(mids_start, mids_end),
                    highs_bins=(highs_start, highs_end))

        while not self._stop.is_set():
            try:
                data = self._source.read(chunk)
            except CaptureError as e:
                logger.error("Error reading audio stream", error=str(e))
                # Interruptible sleep: `time.sleep` here would hold shutdown up
                # for the full backoff on a device that is failing every read.
                self._stop.wait(0.1)
                continue

            # Hann window + FFT. The backend already mixed down to mono.
            window = self._window
            if window is None or window.shape[0] != data.shape[0]:
                window = np.hanning(len(data))
                self._window = window
            fft_mag = np.abs(np.fft.rfft(data * window))

            # ── Band energy: RMS, DC excluded ───────────────────
            bass = _band_rms(fft_mag, bass_start, bass_end)
            mids = _band_rms(fft_mag, mids_start, mids_end)
            highs = _band_rms(fft_mag, highs_start, highs_end)

            # ── Half-wave rectified spectral flux (onset function)
            if self._prev_mag is not None and self._prev_mag.shape == fft_mag.shape:
                d = fft_mag - self._prev_mag
                np.maximum(d, 0.0, out=d)          # half-wave rectify
                flux_bass = float(d[bass_start:max(bass_start + 1, bass_end)].sum())
                flux_mids = float(d[mids_start:max(mids_start + 1, mids_end)].sum())
                flux_highs = float(d[highs_start:max(highs_start + 1, highs_end)].sum())
            else:
                flux_bass = flux_mids = flux_highs = 0.0
            self._prev_mag = fft_mag

            # Raw, unbounded values — all envelope shaping lives in the engine.
            frame = AudioFrame(bass, mids, highs, time.monotonic(),
                               flux_bass, flux_mids, flux_highs)

            logger.debug("audio_frame", bass=bass, mids=mids, highs=highs,
                         flux_bass=flux_bass)

            try:
                self._q.put_nowait(frame)
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(frame)
                except queue.Full:
                    pass
