"""
Zapbox Controller Server — serves the controller UI and accepts
WebSocket connections from the phone browser (Zapbox browser, Safari,
Chrome, etc.).

Runs a small aiohttp web server on ZAPBOX_PORT that:
    GET  /    → serves controller.html (the phone opens this URL)
    WS   /ws  → receives controller_state JSON at ~20 Hz

When running in the Zapbox browser the page uses WebXR to read the
6DOF controllers.  In any other browser it falls back to the
DeviceOrientation gyroscope API.

WebXR mode sends joystick-based input:
    joystick.forward  — left controller stick Y (−1…1)
    joystick.steer    — right controller stick X (−1…1)
    toggles.turbo / toggles.donut / toggles.lights — booleans

Gyro mode falls back to the legacy rotation-based protocol.

Environment variables:
    ZAPBOX_PORT           8080     port for HTTP + WebSocket
    INACTIVITY_SEC        3.0     seconds before agent takes over
    TILT_THRESHOLD        15.0    degrees of tilt for gyro input
    JOYSTICK_THRESHOLD    0.30    deadzone for joystick axes (0–1)
"""

import asyncio
import json
import logging
import os
import time

from aiohttp import web

log = logging.getLogger("zapbox")

ZAPBOX_PORT = int(os.getenv("ZAPBOX_PORT", "8080"))
INACTIVITY_SEC = float(os.getenv("INACTIVITY_SEC", "3.0"))
TILT_THRESHOLD = float(os.getenv("TILT_THRESHOLD", "15.0"))
JOYSTICK_THRESHOLD = float(os.getenv("JOYSTICK_THRESHOLD", "0.30"))

CONTROLLER_HTML = os.path.join(os.path.dirname(__file__), "controller.html")

ZERO = dict(
    forward=0, reverse=0, left=0, right=0,
    lights=0, turbo=0, donut=0,
)


class ZapboxLink:
    """
    Serves the controller page over HTTP and accepts a single WebSocket
    connection that streams gyroscope / XR controller data.
    """

    def __init__(self):
        self.connected = False
        self.running = True
        self.port = ZAPBOX_PORT
        self._control: dict = dict(ZERO)
        self._last_movement: float = 0.0
        self._msg_count: int = 0
        self._prev_fwd: float = 0.0
        self._prev_steer: float = 0.0
        self._prev_pitch: float | None = None
        self._prev_roll: float | None = None

    def is_active(self) -> bool:
        if not self.connected:
            return False
        return (time.time() - self._last_movement) < INACTIVITY_SEC

    def get_control(self) -> dict:
        return dict(self._control)

    # ---- input processing ----

    def _process(self, msg: dict):
        if "joystick" in msg:
            self._process_joystick(msg)
        else:
            self._process_gyro(msg)

    def _process_joystick(self, msg: dict):
        """New protocol: joystick axes + pre-toggled booleans from the client."""
        joy = msg.get("joystick", {})
        toggles = msg.get("toggles", {})

        fwd_val = float(joy.get("forward", 0.0))
        steer_val = float(joy.get("steer", 0.0))

        forward = 1 if fwd_val > JOYSTICK_THRESHOLD else 0
        reverse = 1 if fwd_val < -JOYSTICK_THRESHOLD else 0
        right = 1 if steer_val > JOYSTICK_THRESHOLD else 0
        left = 1 if steer_val < -JOYSTICK_THRESHOLD else 0

        self._control = dict(
            forward=forward,
            reverse=reverse,
            left=left,
            right=right,
            lights=1 if toggles.get("lights", False) else 0,
            turbo=1 if toggles.get("turbo", False) else 0,
            donut=1 if toggles.get("donut", False) else 0,
        )

        self._msg_count += 1
        if self._msg_count % 40 == 1:
            log.info(
                "WS recv #%d  fwd_axis=%.2f steer_axis=%.2f → "
                "fwd=%d rev=%d L=%d R=%d  "
                "turbo=%s donut=%s lights=%s",
                self._msg_count, fwd_val, steer_val,
                forward, reverse, left, right,
                toggles.get("turbo"), toggles.get("donut"),
                toggles.get("lights"),
            )

        delta_fwd = abs(fwd_val - self._prev_fwd)
        delta_steer = abs(steer_val - self._prev_steer)
        if delta_fwd > 0.05 or delta_steer > 0.05:
            self._last_movement = time.time()

        self._prev_fwd = fwd_val
        self._prev_steer = steer_val

    def _process_gyro(self, msg: dict):
        """Legacy protocol: pitch/roll rotation + button edge toggles."""
        rot = msg.get("rotation", {})
        buttons = msg.get("buttons", {})

        pitch = rot.get("pitch", 0.0)
        roll = rot.get("roll", 0.0)

        forward = 1 if pitch > TILT_THRESHOLD else 0
        reverse = 1 if pitch < -TILT_THRESHOLD else 0
        right = 1 if roll > TILT_THRESHOLD else 0
        left = 1 if roll < -TILT_THRESHOLD else 0

        self._control = dict(
            forward=forward,
            reverse=reverse,
            left=left,
            right=right,
            lights=1 if buttons.get("lights", False) else 0,
            turbo=1 if buttons.get("turbo", False) else 0,
            donut=1 if buttons.get("donut", False) else 0,
        )

        self._msg_count += 1
        if self._msg_count % 40 == 1:
            log.info(
                "WS recv #%d [gyro]  pitch=%.1f roll=%.1f → "
                "fwd=%d rev=%d L=%d R=%d",
                self._msg_count, pitch, roll,
                forward, reverse, left, right,
            )

        if self._prev_pitch is not None and self._prev_roll is not None:
            delta_pitch = abs(pitch - self._prev_pitch)
            delta_roll = abs(roll - self._prev_roll)
            if delta_pitch > 1.0 or delta_roll > 1.0:
                self._last_movement = time.time()
        else:
            self._last_movement = time.time()

        self._prev_pitch = pitch
        self._prev_roll = roll

    def _reset(self):
        self._control = dict(ZERO)
        self._prev_fwd = 0.0
        self._prev_steer = 0.0
        self._prev_pitch = None
        self._prev_roll = None

    # ---- HTTP handler ----

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return web.FileResponse(CONTROLLER_HTML)

    # ---- WebSocket handler ----

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        remote = request.remote or "unknown"
        log.info("Controller connected from %s", remote)
        self.connected = True
        self._reset()
        self._last_movement = time.time()

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("Malformed JSON from %s: %s", remote, msg.data[:80])
                        continue
                    msg_type = data.get("type")
                    if msg_type == "controller_state":
                        self._process(data)
                    else:
                        log.debug("Unknown WS message type: %s", msg_type)
                elif msg.type == web.WSMsgType.ERROR:
                    log.warning("WS error from %s: %s", remote, ws.exception())
                    break
        finally:
            self.connected = False
            self._reset()
            log.info("Controller disconnected from %s", remote)

        return ws

    # ---- main ----

    async def run(self):
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/ws", self._handle_ws)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Zapbox server listening on 0.0.0.0:%d", self.port)

        try:
            while self.running:
                await asyncio.sleep(0.5)
        finally:
            await runner.cleanup()
