"""Durable UI/engine state: an atomic store, and a debounced writer task.

Two separate concerns, deliberately kept apart:

  StateStore      knows how to get a dict onto disk without ever leaving a
                  truncated file behind, and how to read one back defensively.
  StatePersister  knows WHEN to call it -- which is the whole problem.

The "when" is the interesting half. Every command the engine applies dirties
the state, and the UI's controls are sliders and a colour wheel: dragging one
fires an event per frame, so a naive save-on-change is a few hundred writes per
second of the same small file. That is disk thrashing on an SSD and a genuine
stutter risk on the tick loop.

The debounce is therefore two-sided:

  quiet period (trailing, 1 s)  wait until the edits stop, then write once.
                                A whole slider drag collapses into one write.
  max delay (leading, 5 s)      but never wait longer than this since the FIRST
                                dirty mark. A user who keeps fiddling for a
                                minute would otherwise have nothing on disk the
                                entire time, and a crash or a power cut would
                                lose all of it.

Trailing alone loses everything under continuous input; leading alone writes on
a fixed cadence whether or not anything changed. Together they give at most one
write per five seconds of sustained activity, and exactly one write per burst.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

from utils.paths import state_file

logger = structlog.get_logger()

# The debounce constants. Named rather than inlined because they are a policy
# decision, not an implementation detail -- see the module docstring.
QUIET_PERIOD_S = 1.0    # trailing: how long the edits must stop before a write
MAX_DELAY_S = 5.0       # leading: the longest a dirty state is held unwritten

SCHEMA_VERSION = 1


class StateStore:
    """Load and save one JSON document, atomically.

    Atomic means the reader never sees a half-written file: the payload goes to
    a temporary file in the SAME directory (so the rename cannot cross a
    filesystem boundary), is flushed and fsync'd, and only then replaced over
    the real path. `os.replace` is atomic on POSIX and on Windows/NTFS, so a
    power cut leaves either the complete old file or the complete new one.

    Without this, the crash window is not hypothetical: the file is rewritten
    on every settling burst, so "the process died mid-write" is precisely the
    case where a user most wants their settings back.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else state_file()
        # Set on the first failure, so an unwritable data directory logs a
        # single line rather than one per debounce window forever.
        self._write_failed = False

    def load(self) -> Dict[str, Any]:
        """Read the stored state, or {} if there is not a usable one.

        Every failure mode -- missing file, truncated JSON, a file written by a
        newer version, a directory where the file should be -- returns {} and
        lets the engine boot on its config defaults. Refusing to start because
        a cache file is corrupt would be the wrong trade for a lighting app.
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Stored state unreadable, starting from defaults",
                           path=str(self.path), error=str(exc))
            return {}

        if not isinstance(data, dict):
            logger.warning("Stored state is not an object, ignoring", path=str(self.path))
            return {}

        version = data.get("version")
        if version is not None and version != SCHEMA_VERSION:
            # Forward compatibility is not worth guessing at. A newer file is
            # ignored rather than half-applied; a future migration hook goes
            # here. apply_dict is field-by-field tolerant either way.
            logger.warning("Stored state has an unknown schema version, ignoring",
                           found=version, expected=SCHEMA_VERSION)
            return {}

        state = data.get("state")
        return state if isinstance(state, dict) else {}

    def save(self, state: Dict[str, Any]) -> bool:
        """Write the state atomically. Returns True on success.

        Never raises: persistence is a convenience, and a failed write must not
        take down a running engine or abort a shutdown sequence.
        """
        payload = {
            "version": SCHEMA_VERSION,
            "saved_at": time.time(),
            "state": state,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # mkstemp + an explicit replace: NamedTemporaryFile's own cleanup
            # would race the rename on Windows.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                    fh.flush()
                    # The rename is only atomic with respect to what is on the
                    # PLATTER. Without the fsync the metadata operation can be
                    # ordered ahead of the data, which is how you end up with a
                    # correctly-named file full of zero bytes after a power cut.
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            if not self._write_failed:
                self._write_failed = True
                logger.error("Could not persist state; continuing without it",
                             path=str(self.path), error=str(exc))
            return False

        if self._write_failed:
            self._write_failed = False
            logger.info("State persistence recovered", path=str(self.path))
        return True


class StatePersister:
    """The debounced writer. One asyncio task; owns no application state.

    It is handed callables rather than the engine itself: `is_dirty` to ask
    whether anything changed and `snapshot` to serialise it. That keeps the
    engine free of any persistence knowledge beyond setting one boolean, and
    makes this testable without a BLE stack.
    """

    def __init__(
        self,
        store: StateStore,
        is_dirty: Callable[[], bool],
        clear_dirty: Callable[[], None],
        snapshot: Callable[[], Dict[str, Any]],
        quiet_period_s: float = QUIET_PERIOD_S,
        max_delay_s: float = MAX_DELAY_S,
        poll_interval_s: float = 0.2,
    ):
        self.store = store
        self._is_dirty = is_dirty
        self._clear_dirty = clear_dirty
        self._snapshot = snapshot
        self.quiet_period_s = quiet_period_s
        self.max_delay_s = max_delay_s
        # How often the dirty flag is examined. A poll rather than an event
        # because the producer side must stay as cheap as a single attribute
        # assignment inside the 30 Hz tick -- see EffectEngine.run. Five
        # wakeups a second that usually do nothing is not worth optimising.
        self.poll_interval_s = poll_interval_s

        self._first_dirty: Optional[float] = None   # start of the current burst
        self._last_dirty: Optional[float] = None    # most recent change in it

    async def run(self):
        """Poll the dirty flag and write when either deadline is reached."""
        logger.info("State persister started", path=str(self.store.path),
                    quiet_s=self.quiet_period_s, max_delay_s=self.max_delay_s)
        try:
            while True:
                await asyncio.sleep(self.poll_interval_s)

                now = time.monotonic()
                if self._is_dirty():
                    # Consume the flag immediately. Anything that dirties it
                    # again during the window simply extends the quiet period,
                    # and -- crucially -- a change arriving between the
                    # snapshot and the next poll re-arms rather than being
                    # swallowed.
                    self._clear_dirty()
                    self._last_dirty = now
                    if self._first_dirty is None:
                        self._first_dirty = now

                if self._first_dirty is None:
                    continue   # nothing pending

                settled = (now - self._last_dirty) >= self.quiet_period_s
                overdue = (now - self._first_dirty) >= self.max_delay_s
                if settled or overdue:
                    self._write(reason="settled" if settled else "max_delay")
        except asyncio.CancelledError:
            # Cancellation is the normal shutdown path. Anything still pending
            # is written on the way out -- a user who changed the mode half a
            # second before quitting expects it back, and this is the only
            # place that can honour that.
            if self._first_dirty is not None or self._is_dirty():
                self._clear_dirty()
                self._write(reason="shutdown")
            raise

    def flush(self, force: bool = False) -> bool:
        """Write now, outside the debounce. Used by the shutdown path.

        With `force`, writes whether or not anything is pending -- which is
        what makes a clean shutdown produce a file even on a run where nothing
        was ever touched, so the next boot restores rather than falling back to
        config defaults.
        """
        if not force and self._first_dirty is None and not self._is_dirty():
            return False
        self._clear_dirty()
        return self._write(reason="flush")

    def _write(self, reason: str) -> bool:
        self._first_dirty = None
        self._last_dirty = None
        try:
            state = self._snapshot()
        except Exception as exc:
            # A broken snapshot must not kill the task; the next dirty mark
            # will try again.
            logger.error("Could not snapshot state for persistence", error=str(exc))
            return False
        ok = self.store.save(state)
        if ok:
            logger.debug("State persisted", reason=reason)
        return ok
