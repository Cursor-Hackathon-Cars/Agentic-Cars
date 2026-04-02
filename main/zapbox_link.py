"""
Zapbox Link Interface — reads controller input from a Zapbox headset
and translates it into car control signals.

The Zapbox Link exposes a WebSocket endpoint that streams 6DOF
controller state at high frequency.  This module connects to that
stream, maps tilt / rotation to driving controls, and tracks whether
the user is actively holding the controller.

When the controller goes idle (no significant movement for
INACTIVITY_SEC seconds) the Copilot switches to autonomous mode and
hands the car to the AI DrivingAgent.

Environment variables:
    ZAPBOX_LINK_URL     ws://host:port     (Zapbox Link endpoint)
    INACTIVITY_SEC      3.0                seconds before agent takes over
    TILT_THRESHOLD      15.0               degrees of tilt for input

Expected Zapbox Link message format (JSON over WebSocket):
    {
        "type": "controller_state",
        "rotation": { "pitch": float, "yaw": float, "roll": float },
        "position": { "x": float, "y": float, "z": float },
        "buttons":  { "trigger": bool, "grip": bool, "a": bool, "b": bool },
        "timestamp": int
    }

Driving mapping:
    pitch  > +threshold   →  forward
    pitch  < −threshold   →  reverse
    roll   > +threshold   →  right
    roll   < −threshold   →  left
    button A (toggle)     →  lights
    button B (toggle)     →  turbo
    trigger (held)        →  donut
"""

import asyncio
import json
import logging
import os
import time

import websockets

log = logging.getLogger("zapbox")

ZAPBOX_LINK_URL = os.getenv("ZAPBOX_LINK_URL", "")
INACTIVITY_SEC = float(os.getenv("INACTIVITY_SEC", "3.0"))
TILT_THRESHOLD = float(os.getenv("TILT_THRESHOLD", "15.0"))
RECONNECT_DELAY = 3

ZERO = dict(
    forward=0, reverse=0, left=0, right=0,
    lights=0, turbo=0, donut=0,
)


class ZapboxLink:
    """
    Maintains a persistent WebSocket connection to the Zapbox Link
    endpoint and converts 6DOF controller state into the car's
    control format (forward/reverse/left/right/lights/turbo/donut).
    """

    def __init__(self):
        self.connected = False
        self.running = True
        self._control: dict = dict(ZERO)
        self._last_movement: float = 0.0
        self._prev_pitch: float | None = None
        self._prev_roll: float | None = None
        self._lights_on = False
        self._turbo_on = False
        self._btn_a_prev = False
        self._btn_b_prev = False

    def is_active(self) -> bool:
        """True when the Zapbox is connected AND the user moved recently."""
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

        # Movement detection for inactivity tracking
        if self._prev_pitch is not None and self._prev_roll is not None:
            delta_pitch = abs(pitch - self._prev_pitch)
            delta_roll = abs(roll - self._prev_roll)
            if delta_pitch > 1.0 or delta_roll > 1.0:
                self._last_movement = time.time()
        else:
            self._last_movement = time.time()

        self._prev_pitch = pitch
        self._prev_roll = roll

    # ---- websocket loop ----

    async def run(self):
        if not ZAPBOX_LINK_URL:
            log.info("ZAPBOX_LINK_URL not set — Zapbox disabled, agent-only mode")
            while self.running:
                await asyncio.sleep(1)
            return

        log.info("Zapbox Link connecting to %s", ZAPBOX_LINK_URL)

        while self.running:
            try:
                async with websockets.connect(ZAPBOX_LINK_URL) as ws:
                    self.connected = True
                    self._last_movement = time.time()
                    log.info("Zapbox Link connected")
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "controller_state":
                            self._process(msg)
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("Zapbox connection lost: %s", e)
            finally:
                self.connected = False
                self._control = dict(ZERO)

            if self.running:
                await asyncio.sleep(RECONNECT_DELAY)
