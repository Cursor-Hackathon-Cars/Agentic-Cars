import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import threading

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "main" / ".env", override=True)

import httpx
from fastapi import FastAPI

log = logging.getLogger("backend")
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from bleak import BleakScanner, BleakClient

try:
    import cv2  # Optional dependency for local webcam test mode.
except Exception:
    cv2 = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "3000"))
PI_IP = os.environ.get("PI_IP", "172.20.10.2")
GO2RTC_PORT = int(os.environ.get("GO2RTC_PORT", "1984"))
GO2RTC_BASE = f"http://{PI_IP}:{GO2RTC_PORT}"
USE_LAPTOP_CAMERA = os.environ.get("USE_LAPTOP_CAMERA", "0").lower() in (
    "1", "true", "yes", "on"
)

LAPTOP_CAMERA_INDEX = int(os.environ.get("LAPTOP_CAMERA_INDEX", "0"))
LAPTOP_CAMERA_WIDTH = int(os.environ.get("LAPTOP_CAMERA_WIDTH", "1280"))
LAPTOP_CAMERA_HEIGHT = int(os.environ.get("LAPTOP_CAMERA_HEIGHT", "720"))
LAPTOP_CAMERA_FPS = max(1, int(os.environ.get("LAPTOP_CAMERA_FPS", "20")))
LAPTOP_CAMERA_JPEG_QUALITY = max(
    1, min(100, int(os.environ.get("LAPTOP_CAMERA_JPEG_QUALITY", "80")))
)

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


class LaptopCamera:
    """Thread-safe local webcam wrapper for test mode."""

    def __init__(self):
        self._cap = None
        self._lock = threading.Lock()

    def start(self):
        if cv2 is None:
            raise RuntimeError(
                "USE_LAPTOP_CAMERA is enabled but OpenCV is not installed. "
                "Install it with: pip install opencv-python"
            )

        cap = cv2.VideoCapture(LAPTOP_CAMERA_INDEX)
        if not cap or not cap.isOpened():
            raise RuntimeError(
                f"Cannot open laptop camera index {LAPTOP_CAMERA_INDEX}"
            )

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, LAPTOP_CAMERA_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, LAPTOP_CAMERA_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, LAPTOP_CAMERA_FPS)
        self._cap = cap

    def stop(self):
        with self._lock:
            if self._cap is not None:
                self._cap.release()
                self._cap = None

    def is_ready(self) -> bool:
        return self._cap is not None

    def read_jpeg(self) -> bytes | None:
        if self._cap is None:
            return None
        with self._lock:
            ok, frame = self._cap.read()
            if not ok:
                return None
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), LAPTOP_CAMERA_JPEG_QUALITY],
            )
            if not ok:
                return None
            return encoded.tobytes()


laptop_camera = LaptopCamera()


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
            except Exception as exc:
                log.warning("BLE write failed, disconnecting: %s", exc)
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
    if USE_LAPTOP_CAMERA:
        try:
            laptop_camera.start()
            print(
                f"📷 Using laptop camera (index={LAPTOP_CAMERA_INDEX}, "
                f"{LAPTOP_CAMERA_WIDTH}x{LAPTOP_CAMERA_HEIGHT} @ {LAPTOP_CAMERA_FPS}fps)"
            )
        except Exception as e:
            print(f"📷 Laptop camera failed to start: {e}")
    else:
        print(f"📷 Pi camera expected at {GO2RTC_BASE}")
    yield
    task.cancel()
    if car.ble_client and car.connected:
        try:
            await car.ble_client.disconnect()
        except Exception:
            pass
    if USE_LAPTOP_CAMERA:
        laptop_camera.stop()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


_ctrl_log_count = 0

@app.post("/car/control")
async def car_control(inp: ControlInput):
    """
    Update the control state. The server continuously sends this state
    to the car at 20 Hz — no need to poll or repeat from the client.
    """
    global _ctrl_log_count

    if not car.connected:
        return JSONResponse({"error": "No car connected"}, status_code=409)

    car.control[1] = 1 if inp.forward else 0
    car.control[2] = 1 if inp.reverse else 0
    car.control[3] = 1 if inp.left else 0
    car.control[4] = 1 if inp.right else 0
    car.control[5] = 1 if inp.lights else 0
    car.control[6] = 1 if inp.turbo else 0
    car.control[7] = 1 if inp.donut else 0

    _ctrl_log_count += 1
    has_input = any([inp.forward, inp.reverse, inp.left, inp.right,
                     inp.lights, inp.turbo, inp.donut])
    if _ctrl_log_count % 40 == 1 or has_input:
        log.info(
            "/car/control #%d  fwd=%d rev=%d L=%d R=%d  "
            "lights=%d turbo=%d donut=%d  ble_bytes=%s",
            _ctrl_log_count,
            inp.forward, inp.reverse, inp.left, inp.right,
            inp.lights, inp.turbo, inp.donut,
            list(car.control),
        )

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


@app.get("/camera/info")
async def camera_info():
    """Return camera source details so clients can connect directly."""
    if USE_LAPTOP_CAMERA:
        return {
            "source": "laptop",
            "stream_url": None,
            "snapshot_url": None,
        }
    return {
        "source": "pi",
        "pi_ip": PI_IP,
        "go2rtc_port": GO2RTC_PORT,
        "stream_url": f"http://{PI_IP}:{GO2RTC_PORT}/api/stream.mp4?src=camera",
        "snapshot_url": f"http://{PI_IP}:{GO2RTC_PORT}/api/frame.jpeg?src=camera",
        "mjpeg_url": f"http://{PI_IP}:{GO2RTC_PORT}/api/stream.mjpeg?src=camera",
    }


@app.post("/camera/connect")
async def camera_connect():
    """Check camera source reachability."""
    if USE_LAPTOP_CAMERA:
        if laptop_camera.is_ready():
            return {
                "connected": True,
                "source": "laptop_camera",
                "camera_index": LAPTOP_CAMERA_INDEX,
            }
        return JSONResponse(
            {"error": "Laptop camera mode enabled, but camera is not ready."},
            status_code=503,
        )

    # Default mode: check whether the Raspberry Pi go2rtc endpoint is reachable.
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
    if USE_LAPTOP_CAMERA:
        # Test mode: serve MJPEG directly from the local webcam.
        if not laptop_camera.is_ready():
            return JSONResponse(
                {"error": "Laptop camera mode enabled, but camera is not ready."},
                status_code=503,
            )

        async def generate_mjpeg():
            while True:
                frame = laptop_camera.read_jpeg()
                if frame:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                await asyncio.sleep(1.0 / LAPTOP_CAMERA_FPS)

        return StreamingResponse(
            generate_mjpeg(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # Default mode: proxy the MP4 stream from go2rtc on the Pi.
    url = f"{GO2RTC_BASE}/api/stream.mp4?src=camera"

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=None, write=None, pool=None)
    )
    try:
        req = client.build_request("GET", url)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get("content-type", "video/mp4")
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


@app.get("/camera/stream.mjpeg")
async def camera_stream_mjpeg():
    """Proxy the MJPEG stream from go2rtc (or serve laptop MJPEG)."""
    if USE_LAPTOP_CAMERA:
        return await camera_stream()

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
            {"error": f"Cannot connect to MJPEG stream at {url}"},
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
    if USE_LAPTOP_CAMERA:
        # Test mode: return one JPEG frame from the local webcam.
        frame = laptop_camera.read_jpeg()
        if frame is None:
            return JSONResponse(
                {"error": "Unable to read frame from laptop camera"},
                status_code=503,
            )
        return Response(content=frame, media_type="image/jpeg")

    # Default mode: return a single JPEG frame from go2rtc.
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-10s  %(message)s",
        datefmt="%H:%M:%S",
    )
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
