import os
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketState

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "3000"))
PI_TOKEN = os.environ.get("PI_TOKEN", "changeme")

PI_MESSAGE_TYPES = {"car/status", "camera/answer", "camera/candidate"}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
pi_socket: WebSocket | None = None
clients: dict[str, WebSocket] = {}  # client_id -> WebSocket

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def broadcast(msg: dict) -> None:
    """Send a JSON message to every connected browser client."""
    data = json.dumps(msg)
    stale: list[str] = []
    for cid, ws in clients.items():
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_text(data)
        except Exception:
            stale.append(cid)
    for cid in stale:
        clients.pop(cid, None)


async def send_to_pi(msg: dict) -> bool:
    """Send a JSON message to the Pi. Returns False if Pi is offline."""
    global pi_socket
    if pi_socket is None:
        return False
    try:
        await pi_socket.send_text(json.dumps(msg))
        return True
    except Exception:
        pi_socket = None
        return False


async def send_to_client(client_id: str, msg: dict) -> None:
    """Send a JSON message to a specific browser client."""
    ws = clients.get(client_id)
    if ws is None:
        return
    try:
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.send_text(json.dumps(msg))
    except Exception:
        clients.pop(client_id, None)


def sanitise_control(msg: dict) -> dict:
    """Coerce all control fields to 0 or 1."""
    return {
        "type": "control",
        "forward": 1 if msg.get("forward") else 0,
        "reverse": 1 if msg.get("reverse") else 0,
        "left": 1 if msg.get("left") else 0,
        "right": 1 if msg.get("right") else 0,
        "lights": 1 if msg.get("lights") else 0,
        "turbo": 1 if msg.get("turbo") else 0,
        "donut": 1 if msg.get("donut") else 0,
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if PI_TOKEN == "changeme":
        print("⚠️  Using default PI_TOKEN — set PI_TOKEN env var for production")
    print(f"🚗 Backend server listening on port {PORT}")
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# WebSocket: Pi agent  (/ws/pi?token=...)
# ---------------------------------------------------------------------------
@app.websocket("/ws/pi")
async def ws_pi(websocket: WebSocket, token: str = Query("")):
    global pi_socket

    # Authenticate
    if token != PI_TOKEN:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    # Replace existing Pi connection
    if pi_socket is not None:
        try:
            await pi_socket.close(code=4000, reason="Replaced by new connection")
        except Exception:
            pass

    pi_socket = websocket
    print("✅ Pi agent connected")
    await broadcast({"type": "pi/status", "connected": True})

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type not in PI_MESSAGE_TYPES:
                continue

            if msg_type == "car/status":
                await broadcast(msg)
            elif msg_type in ("camera/answer", "camera/candidate"):
                client_id = msg.pop("clientId", None)
                if client_id:
                    await send_to_client(client_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if pi_socket is websocket:
            pi_socket = None
        print("❌ Pi agent disconnected")
        await broadcast({"type": "pi/status", "connected": False})
        await broadcast({
            "type": "car/status",
            "connected": False,
            "name": "",
            "battery": None,
        })


# ---------------------------------------------------------------------------
# WebSocket: Browser clients  (/ws)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_client(websocket: WebSocket):
    await websocket.accept()

    client_id = str(uuid.uuid4())
    clients[client_id] = websocket

    # Send current Pi status
    await send_to_client(client_id, {
        "type": "pi/status",
        "connected": pi_socket is not None,
    })

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            # --- Pi offline error replies ---
            if pi_socket is None:
                error_map = {
                    "car/connect": "Car not connected — Pi is offline",
                    "car/disconnect": "Car not connected — Pi is offline",
                    "control": "Car not connected — Pi is offline",
                    "camera/offer": "Camera not connected — Pi is offline",
                    "camera/candidate": "Camera not connected — Pi is offline",
                }
                err = error_map.get(msg_type)
                if err:
                    await send_to_client(client_id, {
                        "type": "error",
                        "message": err,
                    })
                continue

            # --- Forward to Pi ---
            if msg_type == "control":
                await send_to_pi(sanitise_control(msg))

            elif msg_type in ("car/connect", "car/disconnect"):
                await send_to_pi({"type": msg_type})

            elif msg_type == "camera/offer":
                sdp = msg.get("sdp")
                if isinstance(sdp, str):
                    await send_to_pi({
                        "type": "camera/offer",
                        "sdp": sdp,
                        "clientId": client_id,
                    })

            elif msg_type == "camera/candidate":
                candidate = msg.get("candidate")
                if isinstance(candidate, str):
                    await send_to_pi({
                        "type": "camera/candidate",
                        "candidate": candidate,
                        "clientId": client_id,
                    })

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients.pop(client_id, None)
        # Tell Pi to tear down camera session for this client
        if pi_socket is not None:
            await send_to_pi({
                "type": "camera/stop",
                "clientId": client_id,
            })


# ---------------------------------------------------------------------------
# Serve frontend static files (catch-all)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    """Serve index.html for any path (SPA catch-all)."""
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return JSONResponse({"error": "Frontend not built"}, status_code=404)


# ---------------------------------------------------------------------------
# Run with: uvicorn main:app --host 0.0.0.0 --port 3000
# Or:       python main.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
