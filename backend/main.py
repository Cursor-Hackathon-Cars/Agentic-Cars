import os
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "3000"))
PI_IP = os.environ.get("PI_IP", "172.20.10.2")
GO2RTC_PORT = int(os.environ.get("GO2RTC_PORT", "1984"))
GO2RTC_BASE = f"http://{PI_IP}:{GO2RTC_PORT}"

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# BLE UUIDs (Shell Racing Legends)
CONTROL_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------------------
# Car state (one car at a time)
# ---------------------------------------------------------------------------
class CarState:
    def __init__(self):
        self.ble_client: BleakClient | None = None
        self.connected: bool = False
        self.name: str = ""
        self.battery: int | None = None
        self.scanning: bool = False
        self.scanned_devices: list[dict] = []  # [{name, address}, ...]
        self.control = bytearray([1, 0, 0, 0, 0, 0, 0, 0])


car = CarState()


# ---------------------------------------------------------------------------
# 20 Hz BLE control loop
# ---------------------------------------------------------------------------
async def control_loop():
    """Continuously writes the current control state to the car at 20 Hz."""
    while True:
        if car.connected and car.ble_client and car.ble_client.is_connected:
            try:
                await car.ble_client.write_gatt_char(
                    CONTROL_CHAR_UUID, bytes(car.control), response=False
                )
            except Exception:
                car.connected = False
                car.ble_client = None
                car.name = ""
                car.battery = None
                car.control = bytearray([1, 0, 0, 0, 0, 0, 0, 0])
        await asyncio.sleep(1.0 / 20)  # 50 ms


# ---------------------------------------------------------------------------
# Lifespan — start/stop the background control loop
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(control_loop())
    print(f"🚗 Backend server listening on port {PORT}")
    print(f"📷 Pi camera expected at {GO2RTC_BASE}")
    yield
    task.cancel()
    if car.ble_client and car.connected:
        try:
            await car.ble_client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True}


# ===========================  CAR ENDPOINTS  ===============================


@app.post("/car/scan")
async def car_scan():
    """Scan BLE for Shell Racing cars. Returns up to 3 found devices."""
    if car.scanning:
        return JSONResponse({"error": "Already scanning"}, status_code=409)

    car.scanning = True
    try:
        devices = await BleakScanner.discover(timeout=10)
        found = []
        for d in devices:
            name = d.name or ""
            if name.startswith("SL-"):
                found.append({"name": name, "address": d.address})
        car.scanned_devices = found[:3]
        return {
            "cars": [
                {"number": i + 1, "name": c["name"], "address": c["address"]}
                for i, c in enumerate(car.scanned_devices)
            ]
        }
    finally:
        car.scanning = False


@app.post("/car/connect/{car_number}")
async def car_connect(car_number: int):
    """Connect to a scanned car by number (1, 2, or 3)."""
    if car.connected:
        return JSONResponse(
            {"error": "Already connected to a car. Disconnect first."},
            status_code=409,
        )
    if car_number < 1 or car_number > len(car.scanned_devices):
        return JSONResponse(
            {"error": f"Invalid car number. Scanned {len(car.scanned_devices)} car(s)."},
            status_code=400,
        )

    device = car.scanned_devices[car_number - 1]
    try:
        client = BleakClient(device["address"])
        await client.connect()

        # Read battery level
        battery = None
        try:
            raw = await client.read_gatt_char(BATTERY_CHAR_UUID)
            battery = raw[0]
        except Exception:
            pass

        car.ble_client = client
        car.connected = True
        car.name = device["name"]
        car.battery = battery
        car.control = bytearray([1, 0, 0, 0, 0, 0, 0, 0])

        return {"connected": True, "name": car.name, "battery": car.battery}
    except Exception as e:
        return JSONResponse({"error": f"Failed to connect: {e}"}, status_code=500)


@app.post("/car/disconnect")
async def car_disconnect():
    """Disconnect from the current car."""
    if not car.connected:
        return JSONResponse({"error": "No car connected"}, status_code=409)
    try:
        if car.ble_client:
            await car.ble_client.disconnect()
    except Exception:
        pass
    car.connected = False
    car.ble_client = None
    car.name = ""
    car.battery = None
    car.control = bytearray([1, 0, 0, 0, 0, 0, 0, 0])
    return {"connected": False}


class ControlInput(BaseModel):
    forward: int = 0
    reverse: int = 0
    left: int = 0
    right: int = 0
    lights: int = 0
    turbo: int = 0
    donut: int = 0


@app.post("/car/control")
async def car_control(inp: ControlInput):
    """
    Update the control state. The server continuously sends this state
    to the car at 20 Hz — no need to poll or repeat from the client.
    """
    if not car.connected:
        return JSONResponse({"error": "No car connected"}, status_code=409)

    car.control[1] = 1 if inp.forward else 0
    car.control[2] = 1 if inp.reverse else 0
    car.control[3] = 1 if inp.left else 0
    car.control[4] = 1 if inp.right else 0
    car.control[5] = 1 if inp.lights else 0
    car.control[6] = 1 if inp.turbo else 0
    car.control[7] = 1 if inp.donut else 0

    return {
        "ok": True,
        "control": {
            "forward": car.control[1],
            "reverse": car.control[2],
            "left": car.control[3],
            "right": car.control[4],
            "lights": car.control[5],
            "turbo": car.control[6],
            "donut": car.control[7],
        },
    }


@app.get("/car/status")
async def car_status():
    """Current car connection state."""
    return {
        "connected": car.connected,
        "name": car.name,
        "battery": car.battery,
        "scanning": car.scanning,
    }


# ===========================  CAMERA ENDPOINTS  ===========================


@app.post("/camera/connect")
async def camera_connect():
    """Check if the Raspberry Pi's go2rtc is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{GO2RTC_BASE}/api")
            resp.raise_for_status()
        return {"connected": True, "go2rtc_url": GO2RTC_BASE}
    except Exception:
        return JSONResponse(
            {"error": f"Pi not reachable at {PI_IP}:{GO2RTC_PORT}"},
            status_code=503,
        )


@app.get("/camera/stream")
async def camera_stream():
    """Proxy the MJPEG stream from go2rtc on the Pi."""
    url = f"{GO2RTC_BASE}/api/stream.mjpeg?src=camera"

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=None, write=None, pool=None)
    )
    try:
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get(
            "content-type", "multipart/x-mixed-replace; boundary=frame"
        )
    except Exception:
        await client.aclose()
        return JSONResponse(
            {"error": f"Cannot connect to camera stream at {url}"},
            status_code=503,
        )

    async def generate():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=4096):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(generate(), media_type=content_type)


@app.get("/camera/snapshot")
async def camera_snapshot():
    """Get a single JPEG frame from go2rtc."""
    url = f"{GO2RTC_BASE}/api/frame.jpeg?src=camera"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return Response(content=resp.content, media_type="image/jpeg")
    except Exception:
        return JSONResponse(
            {"error": f"Cannot get snapshot from {url}"},
            status_code=503,
        )


# ---------------------------------------------------------------------------
# Serve frontend static files (catch-all)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
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
