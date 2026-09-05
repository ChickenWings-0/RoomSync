# RoomSync: Dev Script → Daily Driver

## Context

RoomSync (`C:\ROOMLIGHTS-APP\RoomSync`) is a working lighting engine: a 30 Hz asyncio render loop
(`effects/engine.py`) driving three MELK-OA10 BLE strips and an OpenRGB PC target, fronted by a
FastAPI app (`web/app.py`) with a vanilla-JS dashboard. The effects work. Nothing else about it is
an application yet.

Three concrete gaps stand between it and a daily driver:

1. **Nothing is remembered.** The only file the app writes is `os.makedirs(static_dir)`. Every
   restart drops back to `config.toml`'s seed — `STATIC`, brightness 0.85, warm white
   `(255,147,41)`, speed 1.0, palette `rainbow`. A daily driver that forgets your room every reboot
   is a demo.
2. **Nothing starts it.** It is launched by hand from a terminal, with `reload=True`, from a CWD it
   silently depends on.
3. **There is no app.** The UI is a browser tab, and there is no way to stop the backend short of
   Ctrl-C in the terminal you launched it from.

There is a fourth gap the migration exposes: **RoomSync cannot currently boot on Fedora at all.**
`samplers/audio.py` imports `pyaudiowpatch` (WASAPI, Windows-exclusive, no Linux build) at module
scope, and `samplers/screen.py` uses `mss`, which cannot capture under Wayland. That is an import
crash, not a degraded mode, so it is Section 0 — persistence and autostart are both moot if the
process dies on `import`.

**Intended outcome:** log into either OS, and the room comes up in the state you left it, with no
window, no terminal, and no ceremony. A desktop icon opens a chromeless dashboard. A tray icon owns
the process.

**Decisions taken:** cross-platform sampler work is in scope (Section 0); the backend runs
perpetually as a service with a **tray icon** as the control surface (no shutdown button in the web
UI); autostart brings up **OpenRGB + engine only**, never the dashboard window.

---

## Architectural facts this plan is built on

Established by reading the code — these are what make the design cheap:

- **`AppState` (`effects/engine.py:90`) is a flat dataclass of 12 scalars.** Modes are `str`-Enums,
  colours are 3-tuples. The entire persistable surface is trivially JSON-serialisable. There is no
  nested state, no object graph.
- **Every mutation funnels through one function.** `EffectEngine._apply_command`
  (`effects/engine.py:529`) is the sole writer of `AppState`; REST (`web/routes.py`) and WS
  (`web/ws.py`) both do nothing but `await engine.command_bus.put(Command(...))`. There is exactly
  one place to hook.
- **The hardware layers already tolerate arbitrary startup order.** `OpenRGBBridge.run()` retries
  forever (`RECONNECT_DELAYS = (1,2,5)` → 10 s cap), and each `BLEStripWorker._reconnect_loop`
  retries forever. **This means startup ordering in Section 2 is log hygiene, not correctness.**
  Nothing breaks if the engine starts before OpenRGB or before the Bluetooth radio is up.
- **`OpenRGBBridge.set_unified_color()` (`peripherals/openrgb_bridge.py:207`) already exists,
  documented for "shutdown blackout", with zero callers.** The blackout path is half-written.
- **`pystray` and `Pillow` are already in `requirements.txt` and nothing imports them.** The tray
  was anticipated.
- **The frontend already builds its mode banks at runtime from `GET /api/modes`**
  (`web/static/app.js`), filtering on `room_ok`/`pc_ok`. Capability gating needs one new field, not
  new plumbing.
- **The frontend sends one WS frame per `input` event with no throttle.** A slider drag is ~60
  messages/second. Debouncing must be server-side.

---

## Section 0 — Cross-platform foundation

### 0.1 Path resolution — new `utils/paths.py`

`config.py:324` loads `TomlConfigSettingsSource(settings_cls, "config.toml")` by **relative** path,
so the app only works when CWD is `RoomSync/`. A scheduled task or systemd unit has an arbitrary
CWD. This must be fixed before anything else in the plan can be trusted.

```python
APP_DIR    = Path(__file__).resolve().parent.parent      # the repo root
config_path()  ->  $ROOMSYNC_CONFIG  or  APP_DIR / "config.toml"
state_dir()    ->  Windows: %APPDATA%\RoomSync
                   Linux:   $XDG_STATE_HOME/roomsync  or  ~/.local/state/roomsync
log_dir()      ->  Windows: %LOCALAPPDATA%\RoomSync\logs
                   Linux:   $XDG_STATE_HOME/roomsync/logs
```

~25 lines, no new dependency, only two platforms to serve. (`platformdirs` is the off-the-shelf
alternative; not worth a dep here.) `ROOMSYNC_STATE_DIR` overrides for tests. Then change
`config.py:324` to `str(config_path())` and `WorkingDirectory` stops being load-bearing.

### 0.2 Sampler backends

Extract capture from DSP. Both samplers keep their current shape — the async `run()` loop in
`samplers/screen.py`, the `threading.Thread` in `samplers/audio.py` — and gain a swappable source.

**`samplers/backends/audio_*.py`** implementing:

```python
class LoopbackSource(Protocol):
    @staticmethod
    def available() -> bool: ...
    def open(self) -> None: ...
    def read(self) -> np.ndarray: ...   # mono float32, settings.audio.chunk_size
    def close(self) -> None: ...
```

- `WasapiLoopback` — the existing `pyaudiowpatch` code, moved verbatim. Windows.
- `PipewireLoopback` — Linux. Use **`sounddevice`** (PortAudio) against the default sink's
  `.monitor` source. PipeWire's PulseAudio compatibility layer exposes `alsa_output.*.monitor`, so
  PortAudio's Pulse host API sees it with no PipeWire-specific code, and the returned chunk shape is
  identical to WASAPI's. Lowest-friction path on Fedora by a wide margin.

**`samplers/backends/screen_*.py`** implementing an equivalent `ScreenSource`.

- `MssCapture` — existing `mss` code. Windows, and Linux-under-X11.
- `PortalCapture` — Linux/Wayland, via `org.freedesktop.portal.ScreenCast` + a PipeWire stream.
  This is the one genuinely hard piece of the milestone. Two mitigations:
  - Persist the portal's **`restore_token` into `state_dir()`** so the permission dialog appears
    once, not every session (GNOME honours this from portal v4+).
  - The sampler only needs ~20 Hz of a heavily subsampled edge crop
    (`settings.screen.subsample_skip = 10`), so a low-resolution stream is cheap.

  Land it **second**: ship the abstraction + gating first so the app boots on Fedora with
  SCREEN_SYNC greyed out, then add the portal backend. Do not block the migration on it.

Selection by `platform.system()` / `XDG_SESSION_TYPE`, with `available()` probed at startup.

### 0.3 Capability gating — modes go grey, not boom

- `effects/registry.py::catalog()` gains `available: bool` + `unavailable_reason: str` per mode,
  derived from which backends actually opened.
- `EffectEngine._apply_command` refuses an unavailable mode **using the exact pattern already there
  for `allows(mode, target)`** (`engine.py:555`) — the target keeps the mode it had rather than
  going dark. Same for the 400 in `routes.py::set_mode`.
- `app.js` renders unavailable modes `disabled` with the reason as a tooltip. It already iterates
  the catalog; this is one attribute.
- `AudioAnalyser.start()` becomes a no-op and `ScreenSampler.run()` returns immediately when no
  backend is available. **Boot must never crash on a missing sampler.**

### 0.4 Other portability items

- `utils/helpers.enable_high_resolution_timer()` (`timeBeginPeriod`) must be a verified no-op on
  Linux. Linux timer granularity is fine at 30 Hz.
- BLE is the easy half: `bleak` on Linux uses BlueZ over D-Bus, MAC addresses work as-is (unlike
  macOS), and `bleak-retry-connector` was written for BlueZ. Expect it to behave *better*.
- `CONNECT_STAGGER_S` is a module constant in `ble/manager.py`, tuned for the Windows BLE stack's
  intolerance of parallel handshakes. BlueZ does not need 1.8 s of stagger. Move it into
  `[ble]` in `config.toml` next to the existing `stagger_delay_ms` so Fedora can shorten it.

---

## Section 1 — State persistence

### 1.1 Where

| | Path |
|---|---|
| Windows | `%APPDATA%\RoomSync\state.json` → `C:\Users\Admin\AppData\Roaming\RoomSync\state.json` |
| Linux | `$XDG_STATE_HOME/roomsync/state.json` → `~/.local/state/roomsync/state.json` |

**Not** beside `config.toml`. `config.toml` is a hand-commented, human-owned, read-only file that
pydantic-settings parses; writing machine state back into it would fight the settings source and
destroy ~200 lines of tuning commentary. It is also inside a git checkout — machine state does not
belong there and must survive a `git clean`.

**Precedence:** `AppState` field defaults (seeded from `config.toml`) → `state.json` overlaid on
top. `config.toml` therefore stays the factory-reset seed, and deleting `state.json` restores it.

### 1.2 Serialisation — new methods on `AppState`

- **`to_dict()`** — becomes the single source of truth for the state projection. Then refactor
  `web/routes.py::get_state` (currently 30 lines of hand-written duplication, `routes.py:73-101`)
  to build its 22-key response *on top of* `to_dict()`, adding only the legacy room-valued aliases
  (`mode`, `brightness`, `speed`, `palette`) and list-ifying the colour tuples. Removes a
  drift-prone duplicate.
- **`apply_dict(data)`** — field-by-field, **never `**data`**:
  - Unknown keys ignored (forward compat with a newer state.json).
  - Missing keys keep the config-seeded default, so adding a field later doesn't invalidate an old
    file.
  - **Every value re-validated through the same rules as `_apply_command`**: `Mode(...)` in
    try/except *plus* `allows(mode, target)`, brightness clamped `[0,1]`, speed `max(0.1, ...)`,
    palette checked against the registry, colour through the `_parse_color` clamp. A hand-edited or
    corrupt file must not boot the engine into unreachable state — precisely the failure
    `_seed_mode` (`engine.py:74`) was written to prevent.
  - Any load failure: log a warning, rename to `state.json.bad`, boot from config defaults. **Never
    crash on boot.**

### 1.3 The hook — two lines, not on the render path

`_apply_command` stays untouched. The hook goes in `run()` (`engine.py:260`), where commands are
already drained:

```python
cmds = drain_all(self.command_bus)
for cmd in cmds:
    self._apply_command(cmd)
if cmds:                                     # <-- the entire hook
    now_m = time.monotonic()
    self._state_dirty_at = now_m
    self._state_dirty_since = self._state_dirty_since or now_m
```

Two float assignments, only on ticks that carried a command. No I/O, no allocation, no JSON, no
lock, nothing that can block. At 30 Hz this is unmeasurable. It deliberately over-triggers (a
no-op command marks dirty) because the debouncer and the hash check downstream absorb that for free.

### 1.4 Debouncing — the slider-drag problem

A brightness drag emits ~60 WS frames/second and each one lands as a command. Writing per command
is 60 fsyncs/second. The policy, implemented in a **separate `asyncio.Task`** (`StatePersister.run()`),
polling at 4 Hz:

| Timer | Value | Purpose |
|---|---|---|
| **Quiet period** (trailing) | 1.0 s | Save once no change has arrived for 1 s. A whole slider drag collapses into **one** write on release. |
| **Max delay** (leading cap) | 5.0 s | If changes keep arriving continuously — a colour wheel dragged for 30 s — force a save every 5 s, so a crash mid-drag loses at most 5 s. |

Save when `now - _state_dirty_at >= 1.0` **or** `now - _state_dirty_since >= 5.0`; then clear both.

Three further guards:

1. **Content-hash short-circuit.** Compare `json.dumps(to_dict(), sort_keys=True)` against the last
   written string; skip identical writes. Kills the common case of the UI re-sending a value that
   didn't change — `app.js` fires on every `input` event, including re-clicking the already-active
   mode button.
2. **Off-thread I/O.** `fsync` on a busy disk blocks for tens of ms, and the persister shares the
   event loop with the tick. Write via `await loop.run_in_executor(None, self._write_blocking, payload)`
   — the same idiom `OpenRGBBridge` already uses for its socket writes and `ScreenSampler` for
   `mss`.
3. **Atomic write, cross-platform.**
   ```
   tmp = dir / f"state.json.{os.getpid()}.tmp"
   write → flush → os.fsync(fh.fileno()) → os.replace(tmp, dest)
   ```
   `os.replace` is atomic on POSIX *and* on NTFS. The Windows-specific trap is that it raises
   `PermissionError` if the destination is momentarily open by an AV scanner or indexer — so retry
   once after 50 ms, then log and give up. **A failed save must never propagate.**

### 1.5 Wiring

New module **`persistence.py`** at the repo root, sibling to `config.py` (same tier of concern):
`StateStore` (load / atomic save) + `StatePersister` (the debounce task).

In `web/app.py::lifespan`, immediately after `EffectEngine(...)` is constructed and **before** the
tick task starts — so the very first frame is already the restored state, with no flash of default
warm-white:

```python
store = StateStore(state_path())
fx_engine.state.apply_dict(store.load())
```

`StatePersister.run()` joins the existing `task_set`. Teardown calls `await persister.flush()`
**before** cancelling tasks, so a change made during the final burst isn't lost.

Restoring `sync_room`/`sync_pc = False` means the lights come up black — correct: you turned them
off, they stay off.

**Not persisted:** `_room_hsv`, `_dsp`, `_pattern`, `_screen_current_colors`, `ws_clients` — all
derived runtime state.

### 1.6 Fold power into state (small, high-value)

`POST /api/power` (`routes.py:159`) bypasses the engine entirely and reaches into the private
`ble_mgr._workers`, and the button's on/off is a JS-local `powerState` boolean that is never read
back. So it is lost on reload and can silently disagree with the strips.

Add `power_on: bool = True` to `AppState`, route the endpoint through a new `SET_POWER` command like
every other control, and it persists and survives a reload for free.

---

## Section 2 — Process management & autostart

### 2.0 Prerequisites (both OSes) — do these first

None of the launchers are trustworthy without these:

- **Create a venv.** There is none today; `python` on this box resolves to the
  `WindowsApps\python.exe` Store alias, which misbehaves in non-interactive contexts. Autostart must
  invoke the venv interpreter **by absolute path**.
- **Pin the dependencies.** `requirements.txt` is entirely unpinned. Unpinned deps + a service that
  starts at boot is a silent breakage waiting to happen. `pip freeze > requirements.lock.txt`.
- **Add a `.gitignore`** — the repo has none. `.venv/`, `__pycache__/`, `*.pyc`, `*.log`.
- **Kill `reload=True`.** `main.py:20` runs uvicorn's reloader, which forks a supervisor + child
  (two PIDs for a service manager to get confused by) and watches the filesystem forever for no
  reason. Change to `uvicorn.run(app, ..., reload=False, log_config=None)` and add an argparse
  `--dev` flag that restores it. `log_config=None` so structlog owns output instead of uvicorn's
  dictConfig.
- **Add file logging.** A silent background service with no console needs somewhere to fail.
  structlog → `RotatingFileHandler(log_dir()/"roomsync.log", maxBytes=2MB, backupCount=3)`.
  Non-negotiable for headless debugging.
- **Single-instance guard.** Two engines fighting over three BLE strips and one OpenRGB socket is a
  genuinely baffling failure mode. Probe `socket.bind((host, port))` in `main.py` before
  `uvicorn.run`; on `EADDRINUSE`, log "RoomSync already running" and `sys.exit(0)`. Portable, and no
  stale-lockfile problem.

### 2.1 Windows — Task Scheduler (recommended)

| | `shell:startup` + `.bat`/`.vbs` | **Task Scheduler** |
|---|---|---|
| Silent | `.bat` flashes a console permanently; needs a `.vbs` `WScript.Shell.Run(cmd, 0, False)` wrapper | Native hidden execution, no console |
| **Elevation** | **Cannot elevate.** OpenRGB needs admin to enumerate SMBus/motherboard/RAM controllers → either a UAC prompt every login or a degraded device list | **"Run with highest privileges" — silent, no prompt** |
| Ordering | None; both race | `At log on` + configurable delay, tasks orderable |
| Restart on crash | None | "Restart every 1 minute, up to 3 times" |
| Diagnostics | A file in a folder | `taskschd.msc`, last-run result, event log |

**The elevation row is decisive** — that alone rules out the startup folder. Two tasks:

**Task A — `RoomSync OpenRGB Server`**
- Trigger: `At log on` (this user)
- Action: `"C:\Program Files\OpenRGB\OpenRGB.exe" --server --startminimized --noautoconnect`
- **Run with highest privileges**, hidden
- *Verify the exact flags against `OpenRGB.exe --help` during implementation — do not assume.*

**Task B — `RoomSync Engine`**
- Trigger: `At log on`, **delay 20 s**
- Action: `C:\ROOMLIGHTS-APP\RoomSync\.venv\Scripts\pythonw.exe`, argument `main.py`,
  **Start in:** `C:\ROOMLIGHTS-APP\RoomSync`
- **`pythonw.exe`, not `python.exe`** — the GUI-subsystem interpreter never allocates a console.
  This is the whole answer to "no terminal window" and makes the `.vbs` wrapper unnecessary.
- **Not** elevated. BLE and a loopback HTTP server don't need it, and an elevated process's tray
  icon can be uninteractable from a non-elevated shell.
- **Uncheck "Stop the task if it runs longer than 3 days"** — this is on by default and would
  silently kill your daily driver mid-week. Uncheck "Start only if on AC power" on a laptop.
- Restart on failure: every 1 min × 3.

The 20 s delay is cosmetic — `OpenRGBBridge` reconnects forever anyway. It just keeps the log clean.

**Why not a real Windows Service (NSSM / pywin32)?** A service runs in session 0: no tray icon, and
no access to the user session's WASAPI audio endpoint. The audio sampler makes a true service
impossible. "At log on" is the correct tier.

Ship as `scripts/windows/install-autostart.ps1` / `uninstall-autostart.ps1` using
`Register-ScheduledTask` + `ScheduledTaskPrincipal`, checked in so it is reproducible and mirrors
the Linux pair.

### 2.2 Linux (Fedora) — systemd user units

Two units in `~/.config/systemd/user/`:

**`roomsync-openrgb.service`**
```ini
[Unit]
Description=OpenRGB SDK server
PartOf=graphical-session.target
[Service]
Type=simple
ExecStart=/usr/bin/openrgb --server --noautoconnect
Restart=on-failure
RestartSec=5
[Install]
WantedBy=graphical-session.target
```

**`roomsync.service`**
```ini
[Unit]
Description=RoomSync lighting engine
After=graphical-session.target roomsync-openrgb.service bluetooth.target
Wants=roomsync-openrgb.service
PartOf=graphical-session.target
[Service]
Type=simple
WorkingDirectory=%h/ROOMLIGHTS-APP/RoomSync
ExecStart=%h/ROOMLIGHTS-APP/RoomSync/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=graphical-session.target
```

**On the dependency questions specifically:**

- **`graphical-session.target`, not `default.target`.** The audio sampler needs the user's PipeWire
  session, the screen sampler needs the Wayland/portal session, and the tray needs a running
  desktop. `default.target` fires on *any* user-manager start — including a bare SSH login — where
  none of those exist. `PartOf=` also gives clean teardown on logout.
- **Do not run `loginctl enable-linger`.** Lingering starts the user manager at boot *without* a
  graphical session — exactly the wrong thing here.
- **Do not wait for the network.** The web server binds `127.0.0.1` and OpenRGB is on loopback.
  `network-online.target` would add boot latency and buy nothing.
- **Bluetooth**: `bluetooth.target` is a *system* unit, so a user unit can `After=` it but cannot
  meaningfully `Wants=` it. In practice BlueZ is up long before the graphical session, and
  `_reconnect_loop` retries forever regardless. Belt-and-braces, not load-bearing.
- **Silence is free.** systemd services have no TTY by definition; stdout/stderr default to the
  journal. `journalctl --user -u roomsync -f` plus the rotating file log from §2.0 gives two views.
- **Environment caveat:** a user unit does *not* inherit your shell profile. The PipeWire backend and
  the portal need `XDG_RUNTIME_DIR` / `WAYLAND_DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`, which come
  from the user manager's environment — GNOME on Fedora populates these via
  `dbus-update-activation-environment`. Verify explicitly; this is a common silent failure.
- **polkit:** `bleak` talks to BlueZ over the *system* bus. Default polkit rules allow this for an
  active local session; confirm during migration.

`scripts/linux/install-autostart.sh` templates `$HOME`/repo paths into both units, then
`systemctl --user daemon-reload && systemctl --user enable --now roomsync.service`.

---

## Section 3 — Native desktop experience & lifecycle

### 3.1 Windows shortcut

**Chrome is not installed on this machine.** Only `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`.
Edge is Chromium and supports `--app` identically; the install script probes for Chrome first, then
Edge.

```
"…\msedge.exe" --app=http://127.0.0.1:8420
               --user-data-dir="%LOCALAPPDATA%\RoomSync\browser"
               --window-size=1100,800
               --class=RoomSync
```

**`--user-data-dir` is the flag people miss and it is the important one.** Without it, `--app` reuses
your main browser profile, so if the browser is already running the invocation is handed to the
existing process — the window's lifecycle becomes coupled to your normal browsing, quitting the
browser can take the app window with it, and taskbar grouping is wrong. A dedicated profile
directory makes it a genuinely separate process with its own icon, its own group, and none of your
extensions.

Create with `WScript.Shell.CreateShortcut` in `scripts/windows/install-shortcut.ps1`; place on the
Desktop **and** in `%APPDATA%\Microsoft\Windows\Start Menu\Programs` so it's Start-key searchable.
Set a real `.ico` so it isn't a browser glyph.

**Also add a web app manifest.** `web/static/manifest.webmanifest` (`display: "standalone"`, name,
icons) linked from `index.html` — ~15 lines. It makes the `--app` window behave better, and unlocks
the one-click "Install this site as an app" path in Edge/Chrome on *both* OSes, which produces a
proper Start Menu / dash entry with no flags at all. Cheapest quality win in this section.

Icons for the shortcut, the manifest and the tray all come from one source image via
`scripts/make_icons.py` — **Pillow is already a dependency.**

### 3.2 Linux `.desktop`

`~/.local/share/applications/roomsync.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=RoomSync
Comment=Room lighting dashboard
Exec=/usr/bin/google-chrome-stable --app=http://127.0.0.1:8420 --user-data-dir=%h/.local/share/roomsync/browser --class=RoomSync
Icon=roomsync
Terminal=false
Categories=Utility;
StartupWMClass=RoomSync
```

- **`StartupWMClass` must match `--class`.** Otherwise GNOME shows a second, generic window entry in
  the dash instead of grouping it under the RoomSync launcher. This is the single most-missed detail
  in Linux web-app shortcuts. On Wayland, Chromium derives `app_id` from `--class`.
- Install `roomsync.png` to `~/.local/share/icons/hicolor/256x256/apps/`, then
  `update-desktop-database ~/.local/share/applications` and `gtk-update-icon-cache`.
- **Browser probe order:** `google-chrome-stable` → `chromium` → `microsoft-edge-stable` → flatpak
  Chromium. **Fedora ships Firefox by default, and Firefox has no `--app` equivalent** (SSB support
  was removed). The install script must fail loudly with an install hint rather than silently write
  a broken launcher.

### 3.3 Lifecycle — the answer to your question

**Run perpetually as a service. No shutdown button in the web UI. A tray icon owns process control.**

Four reasons, in order of weight:

1. **The dashboard is a view onto a service that owns hardware.** Closing a view must never tear
   down hardware — you would lose your lights every time you tidied your taskbar. A web-UI shutdown
   button conflates the two, and one stray click kills the room.
2. **Make running forever actually cheap.** The engine at 30 Hz is mostly `asyncio.sleep`; BLE
   writes at ≤10 Hz; OpenRGB writes only on change plus a 2 s keepalive. The *samplers* are the real
   cost — `mss` grabbing at 20 Hz and a WASAPI loopback stream are not free. So **gate them on
   demand**: the engine already knows `state.room_mode` / `state.pc_mode`, so a `needs_screen()` /
   `needs_audio()` check driven from the same place the dirty flag is set lets both samplers idle
   whenever neither target is in a consuming mode. In STATIC — the overwhelmingly common case — the
   perpetual service costs approximately nothing. This is what makes "just leave it running" the
   right answer rather than a resignation.
3. **The tray is the escape hatch OS conventions already expect**, and it puts process control where
   people look for it.
4. Stopping it remains scriptable either way: `systemctl --user stop roomsync` / Task Scheduler.

**Tray (`pystray` + `Pillow`, already in requirements):**

- Menu: `Open Dashboard` · `Lights Off` (a `SET_TARGETS {room:false, pc:false}`) · — · `Restart
  Engine` · `Quit`.
- **Threading:** `Icon.run()` blocks and some backends prefer the main thread, but uvicorn wants it
  too. Run pystray on a **daemon thread** with `icon.run()` — fine on Windows (Shell_NotifyIcon) and
  with the AppIndicator backend. Flag the GTK-backend variant as a verification item.
- **Quit path:** flush state → blackout both outputs (`ble_mgr.set_color(…, 0,0,0)` + one transmit
  interval of grace, then `rgb_bridge.set_unified_color((0,0,0))` — **the function that already
  exists for exactly this and has zero callers**) → `loop.call_soon_threadsafe` to set
  `server.should_exit = True`.
- **Fedora caveat:** GNOME removed tray icons. The `gnome-shell-extension-appindicator` package is
  **required** for the icon to appear at all. State this in the install script's output — otherwise
  the tray silently doesn't show and it looks like the app failed to start. If tray init fails, log
  it and continue headless: the service and the desktop shortcut both still work.

### 3.4 Shutdown hardening (required for a daily driver)

Currently in `web/app.py::lifespan` teardown:

1. **`for t in task_set: t.cancel()` (`app.py:65`) iterates a set that
   `add_done_callback(task_set.discard)` mutates.** Snapshot it: `for t in tuple(task_set)`.
2. **The strips are never driven to black** — they hold their last colour after the process exits.
   Add a blackout step before `disconnect_all()`.
3. **`rgb_bridge.stop()` never disconnects the `OpenRGBClient`** and does
   `shutdown(wait=False)`. Add a `client.disconnect()` on the executor and `wait=True` with a
   timeout.
4. **`AudioAnalyser` has no stop event** and is a daemon thread that can never be joined — its
   `finally: p.terminate()` is unreachable in normal operation. It needs a `threading.Event` and a
   `stop()` regardless, for the on-demand gating in §3.3.
5. **Signals:** uvicorn installs SIGINT/SIGTERM handlers and runs lifespan teardown, so
   `systemctl --user stop roomsync` is already graceful. Task Scheduler's "End task" is a **hard
   kill** — which is exactly why the 5 s max-delay cap in §1.4 matters on Windows.

---

## Files

**New**
| Path | Purpose |
|---|---|
| `utils/paths.py` | `APP_DIR`, `config_path()`, `state_dir()`, `log_dir()` |
| `persistence.py` | `StateStore` (load / atomic save), `StatePersister` (debounce task) |
| `samplers/backends/` | `LoopbackSource` / `ScreenSource` protocols + WASAPI, PipeWire, mss, portal impls |
| `tray.py` | pystray icon, menu, quit path |
| `web/static/manifest.webmanifest` | PWA manifest |
| `scripts/windows/*.ps1` | `install-autostart`, `uninstall-autostart`, `install-shortcut` |
| `scripts/linux/*.sh` + `*.service` + `*.desktop` | systemd units, desktop entry, installers |
| `scripts/make_icons.py` | one source image → `.ico` + `.png` set (Pillow) |
| `.gitignore` | absent today |

**Modified**
| Path | Change |
|---|---|
| `config.py:324` | relative `"config.toml"` → `str(config_path())` |
| `effects/engine.py` | `AppState.to_dict()` / `apply_dict()`; `power_on` field; dirty-flag hook in `run()`; unavailable-mode refusal in `_apply_command`; `needs_screen()`/`needs_audio()` |
| `effects/registry.py` | `available` + `unavailable_reason` in `catalog()` |
| `web/app.py` | restore-before-start; persister task; blackout + hardened teardown; tray start |
| `web/routes.py` | `get_state` rebuilt on `to_dict()`; `/api/power` → `SET_POWER` command |
| `web/ws.py` | `SET_POWER` in `ALLOWED_COMMANDS` |
| `web/static/app.js` | disable unavailable modes; read `power_on` from `/api/state` |
| `samplers/audio.py`, `samplers/screen.py` | backend injection; stop event; idle-when-unneeded |
| `ble/manager.py` | `CONNECT_STAGGER_S` → config |
| `main.py` | `reload=False` + `--dev`; file logging; single-instance port probe |
| `requirements.txt` | add `sounddevice`; pin; generate `requirements.lock.txt` |

---

## Sequencing

1. **Foundation** — `utils/paths.py`, absolute config path, venv, pinned reqs, `.gitignore`, file
   logging, `reload=False`, single-instance guard.
2. **Persistence** — `persistence.py`, `to_dict`/`apply_dict`, dirty hook, persister task, `get_state`
   refactor, `power_on`.
3. **Shutdown hardening + on-demand sampler gating** (§3.4, §3.3-2).
4. **Cross-platform samplers** — protocols + capability gating first (unblocks Fedora boot), then
   PipeWire audio, then the portal screen backend.
5. **Tray.**
6. **Windows autostart + shortcut scripts** — verify on this machine now.
7. **Linux systemd + `.desktop`** — verify after the migration.

Steps 1–3 are pure Windows-verifiable wins and carry no migration risk. Step 4 is the only
substantial engineering risk and is explicitly staged so the portal backend can slip without
blocking anything else.

---

## Verification

**Persistence**
- Set a distinct state (room `BREATHE` @ 0.42, PC `PLASMA`, unlinked colours), `taskkill` /
  `systemctl --user restart roomsync`, confirm `GET /api/state` returns it and the room comes back
  correct with **no visible flash of warm white** on the first frame.
- Drag the brightness slider continuously for 20 s while watching the state file's mtime — expect
  ~4 writes (the 5 s cap), then exactly one more 1 s after release. Confirm with
  `Get-Item state.json | Select LastWriteTime` in a loop, or `inotifywait -m` on Linux.
- Instrument the tick loop's `dt` (or add a temporary p99 log line) across a 60 s drag: p99 must not
  move versus a no-drag baseline. This is the load-bearing claim of §1.3.
- Corrupt `state.json` (truncate mid-object, then set `"room_mode": "NOPE"`): the app must boot from
  config defaults both times, rename the file to `.bad`, and log a warning — not crash.
- Delete `state.json` → engine returns to `config.toml` seed.

**Process management**
- Windows: `Register-ScheduledTask`, reboot, confirm **zero windows appear**, `Get-Process pythonw`
  shows one instance, `curl http://127.0.0.1:8420/api/state` answers, and the log file has a clean
  startup. Then run `main.py` a second time by hand and confirm it exits with "already running".
- Kill the engine process and confirm Task Scheduler restarts it within ~60 s.
- Linux: `systemctl --user status roomsync`, `journalctl --user -u roomsync -b`, then log out and
  back in and confirm `PartOf=graphical-session.target` tore it down and brought it back.
- Deliberately start the engine with OpenRGB **stopped**, confirm it runs and the PC target attaches
  on its own once OpenRGB comes up — proving the ordering claim in §"Architectural facts".

**Desktop experience**
- Launch the shortcut: no address bar, no tabs, own taskbar/dash icon (not a browser glyph), and on
  Linux confirm `StartupWMClass` grouping is correct.
- Close the app window → confirm the strips keep running and the tray icon is still live; reopen
  from the tray and confirm the UI restores current state.
- Tray → `Quit`: strips and PC lights go **black** (not frozen on the last colour), OpenRGB client
  disconnects, state file holds the pre-quit state, and no orphan `pythonw`/`python` remains.

**Cross-platform (post-migration)**
- On Fedora before the sampler backends land: app boots, STATIC and all generative patterns work,
  and AUDIO_REACTIVE / MUSIC / SCREEN_SYNC render **greyed out with a reason** — no traceback.
- After: play audio, confirm MUSIC reacts; grant the portal prompt once, restart, confirm
  SCREEN_SYNC resumes **without** re-prompting (the `restore_token`).
- `test_patterns.py` and `test_ble.py` still run (they are standalone scripts, not pytest suites —
  invoke directly).
