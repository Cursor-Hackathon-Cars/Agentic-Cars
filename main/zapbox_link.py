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

Environment variables:
    ZAPBOX_PORT         8080               port for HTTP + WebSocket
    INACTIVITY_SEC      3.0                seconds before agent takes over
    TILT_THRESHOLD      15.0               degrees of tilt for input
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
        self._prev_pitch: float | None = None
        self._prev_roll: float | None = None
        self._lights_on = False
        self._turbo_on = False
        self._btn_a_prev = False
        self._btn_b_prev = False

    def is_active(self) -> bool:
        if not self.connected:
            return False
        return (time.time() - self._last_movement) < INACTIVITY_SEC

    def get_control(self) -> dict:
        return dict(self._control)

    # ---- input processing ----

    def _process(self, msg: dict):
        rot = msg.get("rotation", {})
        buttons = msg.get("buttons", {})

        pitch = rot.get("pitch", 0.0)
        roll = rot.get("roll", 0.0)

        forward = 1 if pitch > TILT_THRESHOLD else 0
        reverse = 1 if pitch < -TILT_THRESHOLD else 0
        right = 1 if roll > TILT_THRESHOLD else 0
        left = 1 if roll < -TILT_THRESHOLD else 0

        btn_a = bool(buttons.get("a", False))
        btn_b = bool(buttons.get("b", False))
        trigger = bool(buttons.get("trigger", False))

        if btn_a and not self._btn_a_prev:
            self._lights_on = not self._lights_on
        if btn_b and not self._btn_b_prev:
            self._turbo_on = not self._turbo_on
        self._btn_a_prev = btn_a
        self._btn_b_prev = btn_b

        self._control = dict(
            forward=forward,
            reverse=reverse,
            left=left,
            right=right,
            lights=1 if self._lights_on else 0,
            turbo=1 if self._turbo_on else 0,
            donut=1 if trigger else 0,
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
        self._prev_pitch = None
        self._prev_roll = None
        self._lights_on = False
        self._turbo_on = False
        self._btn_a_prev = False
        self._btn_b_prev = False

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
                        continue
                    if data.get("type") == "controller_state":
                        self._process(data)
                elif msg.type == web.WSMsgType.ERROR:
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
