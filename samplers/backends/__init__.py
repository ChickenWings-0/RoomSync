"""Backend selection: which capture implementation runs on THIS machine.

One table per capability, ordered by preference, probed once at startup. The
result is a `Capability` record per capability — available or not, with a
reason — and that record is what the mode registry gates on and what the UI
turns into a greyed-out button with a tooltip.

Probing is explicit and eager (`probe_all()` during app startup) rather than
lazy at first use, for one reason: the answer has to be known before the first
`GET /api/modes`, or the browser builds its mode banks against capabilities
nobody has checked yet and the greying-out arrives a second late.

Adding a platform is adding a row. A PipeWire loopback source goes in the audio
table above the Windows one with its own `probe()`; nothing else changes, and
until someone writes it the Linux answer is a clear sentence rather than an
ImportError.
"""

import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import structlog

from .base import (CAP_AUDIO, CAP_SCREEN, BackendUnavailable, CaptureError,
                   LoopbackSource, ScreenSource)

logger = structlog.get_logger()

__all__ = [
    "CAP_AUDIO", "CAP_SCREEN", "BackendUnavailable", "CaptureError",
    "LoopbackSource", "ScreenSource", "Capability",
    "probe_all", "capabilities", "capability", "open_loopback", "open_screen",
]


@dataclass(frozen=True)
class Capability:
    """The answer to "can this machine do X?", with a reason when it cannot."""

    name: str
    available: bool
    backend: Optional[str] = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "capability": self.name,
            "available": self.available,
            "backend": self.backend,
            "reason": self.reason,
        }


def _windows_loopback():
    from .windows import WasapiLoopback
    return WasapiLoopback


def _mss_screen():
    from .windows import MssCapture
    return MssCapture


# ── The tables ───────────────────────────────────────────────────────
# (loader, platform predicate). The loader is deferred so that importing this
# module never imports a capture library — which is what makes probing on a
# machine missing one of them a reported capability rather than an ImportError
# at app startup.
_TRUE: Callable[[], bool] = lambda: True
_WINDOWS: Callable[[], bool] = lambda: sys.platform.startswith("win")

_AUDIO_BACKENDS: List[Tuple[Callable, Callable[[], bool]]] = [
    (_windows_loopback, _WINDOWS),
    # PipeWire / PulseAudio monitor source goes here, above or below the
    # Windows row as appropriate — the predicate keeps them from being tried on
    # the wrong platform, so ordering between platforms never matters.
]

_SCREEN_BACKENDS: List[Tuple[Callable, Callable[[], bool]]] = [
    # mss handles Windows (BitBlt) and X11 (SHM) from the one implementation,
    # so there is a single row here and its probe failure message covers the
    # Wayland case explicitly.
    (_mss_screen, _TRUE),
]

_CAPABILITIES: dict = {}


def _select(cap_name: str, table) -> Tuple[Optional[type], Capability]:
    """Try each eligible backend in order; return the first that probes clean."""
    reasons: List[str] = []
    tried_any = False

    for loader, eligible in table:
        if not eligible():
            continue
        tried_any = True
        try:
            cls = loader()
        except BackendUnavailable as exc:
            reasons.append(exc.reason)
            continue
        except Exception as exc:
            # A backend module that fails to import for any other reason is a
            # bug in that backend, not a reason to take the app down with it.
            reasons.append("%s failed to load: %s" % (getattr(loader, "__name__", "?"), exc))
            continue

        try:
            cls.probe()
        except BackendUnavailable as exc:
            reasons.append("%s: %s" % (cls.name, exc.reason))
            continue
        except Exception as exc:
            reasons.append("%s failed to probe: %s" % (cls.name, exc))
            continue

        return cls, Capability(cap_name, True, backend=cls.name)

    if not tried_any:
        reason = "No %s backend exists for this platform (%s) yet." % (cap_name, sys.platform)
    else:
        reason = " ".join(reasons) or "No %s backend could be started." % cap_name
    return None, Capability(cap_name, False, reason=reason)


_SELECTED: dict = {}


def probe_all() -> dict:
    """Probe every capability once and cache the result. Returns {name: Capability}.

    Safe to call more than once; the probes only run on the first call, because
    they open real devices and doing that repeatedly on a hot path would be a
    good way to fight with whatever else wants the audio endpoint.
    """
    if _CAPABILITIES:
        return _CAPABILITIES

    for cap_name, table in ((CAP_AUDIO, _AUDIO_BACKENDS), (CAP_SCREEN, _SCREEN_BACKENDS)):
        cls, cap = _select(cap_name, table)
        _SELECTED[cap_name] = cls
        _CAPABILITIES[cap_name] = cap
        if cap.available:
            logger.info("Capture backend selected", capability=cap_name, backend=cap.backend)
        else:
            # Deliberately a warning, not an error: a machine without a
            # loopback device is a supported configuration that simply has
            # fewer modes, and this line is how the user finds out why.
            logger.warning("Capability unavailable — its modes will be disabled",
                           capability=cap_name, reason=cap.reason)

    return _CAPABILITIES


def capabilities() -> dict:
    """{name: Capability}, probing first if that has not happened yet."""
    return probe_all()


def capability(name: str) -> Capability:
    return capabilities().get(name, Capability(name, False, reason="Unknown capability."))


def open_loopback() -> LoopbackSource:
    """The selected loopback source, opened. Raises BackendUnavailable."""
    probe_all()
    cls = _SELECTED.get(CAP_AUDIO)
    if cls is None:
        raise BackendUnavailable(capability(CAP_AUDIO).reason)
    source = cls()
    source.open()
    return source


def open_screen() -> ScreenSource:
    """The selected screen source, opened. Raises BackendUnavailable."""
    probe_all()
    cls = _SELECTED.get(CAP_SCREEN)
    if cls is None:
        raise BackendUnavailable(capability(CAP_SCREEN).reason)
    source = cls()
    source.open()
    return source
