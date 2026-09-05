import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple, Optional, Tuple

import numpy as np
import structlog

from config import settings
from .backends import BackendUnavailable, CaptureError, open_screen

logger = structlog.get_logger()

# How long to wait before retrying after a capture failure. A display-mode
# change, a monitor sleeping, an RDP session detaching — all recover on their
# own, so this is a pause rather than a teardown.
RETRY_DELAY_S = 1.0

# Consecutive failures before the sampler rebuilds its capture handle. mss can
# be left holding a stale device context after a resolution change, and no
# amount of retrying on the same handle fixes that.
REOPEN_AFTER_FAILURES = 5


class ScreenFrame(NamedTuple):
    left_rgb: Tuple[int, int, int]
    right_rgb: Tuple[int, int, int]
    bottom_rgb: Tuple[int, int, int]
    ts: float


class ScreenSampler:
    """Edge colours of the primary display, at `screen.sample_hz`.

    The capture library is now behind a backend (see samplers/backends), so a
    machine that cannot capture the screen reports that as a capability and
    disables SCREEN_SYNC, instead of raising on import as it used to.
    """

    def __init__(self):
        self.screen_queue = asyncio.Queue(maxsize=2)
        # A dedicated SINGLE worker, not the default executor. mss is not
        # thread-safe and its handle must be used from the one thread that
        # created it; the default executor would move grabs between threads and
        # the failure is intermittent, platform-dependent corruption rather
        # than a clean error.
        self._thread_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="screen"
        )
        self._source = None
        self.available = False
        self.unavailable_reason = ""

    # ── Capture ────────────────────────────────────────────────────

    def _open(self) -> None:
        """Runs on the capture thread: mss must be created where it is used."""
        self._source = open_screen()

    def _close(self) -> None:
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                pass
            self._source = None

    def _capture_and_compute(self) -> ScreenFrame:
        img = self._source.grab()
        h, w = img.shape[0], img.shape[1]

        edge_frac = settings.screen.edge_fraction
        skip = max(1, settings.screen.subsample_skip)

        left_w = max(1, int(w * edge_frac))
        right_w = min(w - 1, int(w * (1.0 - edge_frac)))
        bottom_h = min(h - 1, int(h * 0.9))   # Bottom 10%

        # Slice regions and subsample. mss hands back BGRA, so channels 0, 1, 2
        # are B, G, R and the alpha is never touched.
        left_region = img[:, :left_w, :3][::skip, ::skip]
        left_b, left_g, left_r = left_region.mean(axis=(0, 1))

        right_region = img[:, right_w:, :3][::skip, ::skip]
        right_b, right_g, right_r = right_region.mean(axis=(0, 1))

        bottom_region = img[bottom_h:, :, :3][::skip, ::skip]
        bottom_b, bottom_g, bottom_r = bottom_region.mean(axis=(0, 1))

        return ScreenFrame(
            left_rgb=(int(left_r), int(left_g), int(left_b)),
            right_rgb=(int(right_r), int(right_g), int(right_b)),
            bottom_rgb=(int(bottom_r), int(bottom_g), int(bottom_b)),
            ts=time.monotonic(),
        )

    # ── Loop ───────────────────────────────────────────────────────

    async def run(self):
        loop = asyncio.get_running_loop()
        interval = 1.0 / settings.screen.sample_hz

        try:
            await loop.run_in_executor(self._thread_pool, self._open)
        except BackendUnavailable as exc:
            # A supported configuration with one fewer mode, not a failure.
            # The capability probe has already greyed SCREEN_SYNC out in the UI.
            self.unavailable_reason = exc.reason
            logger.warning("Screen capture unavailable — SCREEN_SYNC is disabled",
                           reason=exc.reason)
            return
        except Exception as exc:
            self.unavailable_reason = str(exc)
            logger.error("Screen capture failed to start", error=str(exc))
            return

        self.available = True
        failures = 0
        logger.info("ScreenSampler started", backend=self._source.name,
                    size=self._source.size(), sample_hz=settings.screen.sample_hz)

        try:
            while True:
                start_ts = time.monotonic()

                try:
                    frame = await loop.run_in_executor(
                        self._thread_pool, self._capture_and_compute
                    )
                    failures = 0
                except CaptureError as exc:
                    failures += 1
                    logger.warning("Screen capture failed", error=str(exc),
                                   consecutive=failures)
                    if failures >= REOPEN_AFTER_FAILURES:
                        # The handle itself is stale — rebuild it rather than
                        # retrying against a device context that will never
                        # succeed again.
                        logger.info("Rebuilding the screen capture handle")
                        try:
                            await loop.run_in_executor(self._thread_pool, self._close)
                            await loop.run_in_executor(self._thread_pool, self._open)
                            failures = 0
                        except Exception as reopen_exc:
                            logger.error("Could not reopen screen capture",
                                         error=str(reopen_exc))
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue

                try:
                    self.screen_queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Drop oldest if queue is full
                    try:
                        self.screen_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self.screen_queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        pass

                elapsed = time.monotonic() - start_ts
                await asyncio.sleep(max(0.0, interval - elapsed))
        finally:
            # Cancellation lands here. The capture handle is an OS resource and
            # is released on the thread that created it; without this the mss
            # instance is finalised by the GC on an arbitrary thread, which on
            # Windows can raise during interpreter shutdown.
            self.available = False
            try:
                await loop.run_in_executor(self._thread_pool, self._close)
            except Exception:
                pass
            self._thread_pool.shutdown(wait=False)
            logger.info("ScreenSampler stopped")
