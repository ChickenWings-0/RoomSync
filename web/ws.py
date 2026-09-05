import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import structlog

from effects.engine import Command

logger = structlog.get_logger()
ws_router = APIRouter()

# Commands the preview socket is allowed to inject into the engine. The socket
# is the control path for anything a user *drags* — a colour wheel or a slider
# fires continuously, and one HTTP round trip per pixel is not a control
# surface, it is a denial of service against your own engine.
#
# SET_MODE, SET_STATIC_COLOR, SET_SPEED and SET_PALETTE all take the same
# optional "target": "room" | "pc" | "both", absent meaning both. SET_ROOM_MODE
# and SET_PC_MODE are the explicit spellings of the first one.
#
# Nothing here validates whether a mode is legal for a target: the socket is
# fire-and-forget with no response channel, so there is nowhere to report a
# rejection to. `EffectEngine._apply_command` is the authority and refuses a
# PC-only pattern on the room by leaving that target exactly as it was. The
# REST route returns a 400 for the same case, because there it can.
ALLOWED_COMMANDS = {
    "SET_MODE",
    "SET_ROOM_MODE",
    "SET_PC_MODE",
    "SET_BRIGHTNESS",
    "SET_PALETTE",
    "SET_SPEED",
    "SET_STATIC_COLOR",
    "SET_TARGETS",
    # The master switch. The socket path keeps the ENGINE in step;
    # the strips themselves are still driven by the /api/power route,
    # which can talk to the BLE workers directly and immediately.
    "SET_POWER",
}


@ws_router.websocket("/ws/preview")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Access the engine from the app state via the websocket object
    engine = websocket.app.state.engine

    if not engine:
        await websocket.close()
        return

    # Register client for 30Hz broadcast from the engine
    engine.ws_clients.add(websocket)
    try:
        # Bidirectional: the engine pushes preview frames down in its tick
        # loop, the client pushes commands up. Anything unparseable is dropped
        # rather than closing the socket — a malformed frame must not cost the
        # user their live preview.
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not isinstance(msg, dict):
                continue

            cmd_type = msg.get("type")
            payload = msg.get("payload", {})
            if cmd_type not in ALLOWED_COMMANDS or not isinstance(payload, dict):
                continue

            # Same bus the REST routes use, so both paths are applied at the
            # top of a tick and can never interleave mid-frame.
            await engine.command_bus.put(Command(type=cmd_type, payload=payload))
    except WebSocketDisconnect:
        pass
    finally:
        engine.ws_clients.discard(websocket)
