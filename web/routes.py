from typing import List, Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from effects.modes import Mode
from effects.engine import Command
from effects.registry import allows, catalog, is_available, unavailable_reason

router = APIRouter()

_TARGETS = ("room", "pc", "both")


def _targets_of(target: str) -> List[str]:
    """Expand the 'target' grammar. Mirrors EffectEngine._targets_in exactly."""
    return ["room", "pc"] if target == "both" else [target]


def _validated_target(target: Optional[str]) -> str:
    t = (target or "both").lower()
    if t not in _TARGETS:
        raise HTTPException(status_code=400, detail="Invalid target")
    return t


class ModeUpdate(BaseModel):
    mode: str
    # Which output this mode applies to. Optional and defaulting to "both", so
    # the old single-mode call shape still means what it always meant.
    target: str = "both"


class BrightnessUpdate(BaseModel):
    value: float
    target: str = "both"


class PaletteUpdate(BaseModel):
    name: str
    target: str = "both"


class SpeedUpdate(BaseModel):
    value: float
    target: str = "both"


class PowerUpdate(BaseModel):
    on: bool


@router.get("/api/modes")
async def get_modes():
    """The mode catalog: what exists, what it needs, and where it may run.

    The browser builds both mode banks from this and filters each by
    room_ok / pc_ok. That is what lets index.html stop hard-coding two
    identical button grids and app.js stop mirroring the server's capability
    sets in constants that could silently drift out of date.

    Static, and needs no engine, so it answers even before boot finishes.
    """
    return {"modes": catalog()}


@router.get("/api/state")
async def get_state(request: Request):
    """The engine's own snapshot, plus the legacy aliases the UI still reads.

    The body used to be a hand-written dict listing every field twice over —
    once here and once in whatever else needed to serialise the state. It is
    now AppState.to_dict(), so a knob added to the engine is reported here
    automatically instead of being forgotten until someone notices the UI
    never restores it.

    The bare "mode"/"brightness"/"speed"/"palette" keys are NOT in to_dict():
    they are room-valued aliases for the pre-split single knobs, they exist
    only so an older cached page renders something sane rather than undefined,
    and there is no reason to write them to disk. They are added here, at the
    boundary that actually has a legacy client to serve.
    """
    engine = request.app.state.engine
    if not engine:
        raise HTTPException(status_code=503, detail="Engine not ready")

    state = engine.state.to_dict()
    state.update({
        "mode": state["room_mode"],
        "brightness": state["room_brightness"],
        "speed": state["room_speed"],
        "palette": state["room_palette"],
    })
    return state


@router.put("/api/mode")
async def set_mode(payload: ModeUpdate, request: Request):
    engine = request.app.state.engine
    try:
        mode = Mode(payload.mode)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid mode")

    target = _validated_target(payload.target)

    # Refused because this MACHINE cannot feed it — no loopback device, no
    # screen capture. Checked before the per-target hardware rule because it is
    # the more fundamental of the two: an unavailable mode is unavailable on
    # both targets, and saying so plainly beats "cannot run on the room".
    if not is_available(mode):
        raise HTTPException(status_code=400, detail=unavailable_reason(mode))

    # A mode can be refused for a target on hardware grounds: the PC-exclusive
    # patterns have features shorter than one BLE transmit interval, so the
    # room cannot represent them at all. Under target "both" the legal half
    # still applies — which is exactly what the engine does — so only a request
    # that would change nothing whatsoever is an error.
    applied = [t for t in _targets_of(target) if allows(mode, t)]
    if not applied:
        raise HTTPException(
            status_code=400,
            detail="%s cannot run on the %s target: its features are shorter "
                   "than one BLE transmit interval." % (mode.value, target),
        )

    cmd = Command(type="SET_MODE", payload={"mode": payload.mode, "target": target})
    await engine.command_bus.put(cmd)
    return {"status": "ok", "applied": applied}


@router.put("/api/brightness")
async def set_brightness(payload: BrightnessUpdate, request: Request):
    engine = request.app.state.engine
    target = _validated_target(payload.target)
    cmd = Command(type="SET_BRIGHTNESS", payload={"value": payload.value, "target": target})
    await engine.command_bus.put(cmd)
    return {"status": "ok", "applied": _targets_of(target)}


@router.put("/api/palette")
async def set_palette(payload: PaletteUpdate, request: Request):
    engine = request.app.state.engine
    target = _validated_target(payload.target)
    cmd = Command(type="SET_PALETTE", payload={"name": payload.name, "target": target})
    await engine.command_bus.put(cmd)
    return {"status": "ok", "applied": _targets_of(target)}


@router.put("/api/speed")
async def set_speed(payload: SpeedUpdate, request: Request):
    engine = request.app.state.engine
    target = _validated_target(payload.target)
    cmd = Command(type="SET_SPEED", payload={"value": payload.value, "target": target})
    await engine.command_bus.put(cmd)
    return {"status": "ok", "applied": _targets_of(target)}


@router.post("/api/power")
async def set_power(payload: PowerUpdate, request: Request):
    """The master switch. Two paths, because it means two different things.

    The BLE call is immediate and direct, exactly as before: it must halt or
    start the strips NOW rather than at the top of the next tick, since that
    is the difference between a responsive button and a laggy one.

    The command onto the bus is the new half. Power is now part of the engine's
    persisted state, so the engine has to know: without it, the strips go dark
    and the tick loop keeps cheerfully rendering frames at them, and nothing
    survives a restart. It goes through the bus rather than being assigned
    directly for the usual reason — every other mutation does, so they are all
    applied at the top of a tick and can never interleave mid-frame.
    """
    ble_mgr = request.app.state.ble_mgr
    engine = request.app.state.engine

    if engine:
        await engine.command_bus.put(Command(type="SET_POWER", payload={"on": payload.on}))

    if ble_mgr:
        for strip in ble_mgr._workers:
            await ble_mgr.set_power(strip, payload.on)

    return {"status": "ok", "power_on": payload.on}


@router.get("/api/capabilities")
async def get_capabilities(request: Request):
    """What this host can capture, and why not when it cannot.

    The mode catalog already carries per-mode `available` flags, which is what
    the UI actually gates on. This route is the diagnostic view of the same
    thing: one place to look when a user asks why Audio Reactive is greyed out,
    without having to read it back out of sixteen mode entries.
    """
    from samplers import backends
    return {"capabilities": [c.as_dict() for c in backends.capabilities().values()]}


@router.get("/api/devices")
async def get_devices(request: Request):
    ble_mgr = request.app.state.ble_mgr
    if not ble_mgr:
        return {"devices": [], "all_connected": False}
    # worker.status() is the authoritative view: it agrees with the watchdog
    # rather than reading the transport directly.
    return {"devices": ble_mgr.status(), "all_connected": ble_mgr.all_connected}
