"""
AI Driving Agent — analyses camera frames and produces control decisions.

Periodically fetches a JPEG snapshot from the backend's /camera/snapshot
proxy (or a direct go2rtc URL if overridden), sends it to OpenAI's
vision model, and translates the response into car control signals that
the Copilot reads at 20 Hz.

Environment variables:
    BACKEND_URL           http://host:3000  (used to build default snapshot URL)
    CAMERA_SNAPSHOT_URL   Override: full URL for a JPEG frame, e.g.
                          http://192.168.1.50:1984/api/frame.jpeg?src=camera
                          Defaults to {BACKEND_URL}/camera/snapshot
    OPENAI_API_KEY        OpenAI API key (required for vision analysis)
    AGENT_INTERVAL        Seconds between vision calls (default 1.0)
"""

import asyncio
import base64
import json
import logging
import os

import aiohttp
from openai import AsyncOpenAI

log = logging.getLogger("agent")

_BACKEND = os.getenv("BACKEND_URL", "http://localhost:3000").rstrip("/")
CAMERA_SNAPSHOT_URL = os.getenv("CAMERA_SNAPSHOT_URL", f"{_BACKEND}/camera/snapshot")
AGENT_INTERVAL = float(os.getenv("AGENT_INTERVAL", "1.0"))

SYSTEM_PROMPT = """\
You are an autonomous AI copilot for a small remote-controlled car.
You receive a single camera frame from the car's forward-facing camera.
The camera is mounted on top of the car (it may be rotated 180°).

Decide the NEXT driving action.  Reply ONLY with a JSON object:
{
  "forward":   0 or 1,
  "reverse":   0 or 1,
  "left":      0 or 1,
  "right":     0 or 1,
  "lights":    0 or 1,
  "reasoning": "<one sentence>"
}

Rules
  • Drive forward by default unless an obstacle is close (~30 cm).
  • Steer left or right to avoid obstacles.
  • If the path ahead is fully blocked, reverse briefly.
  • Turn on lights when the environment looks dark.
  • NEVER set forward AND reverse at the same time.
  • NEVER set left AND right at the same time.
  • Prefer gentle exploration — alternate turns to cover the area.
"""

ZERO = dict(
    forward=0, reverse=0, left=0, right=0,
    lights=0, turbo=0, donut=0,
)


class DrivingAgent:
    """
    Captures camera snapshots, sends them to OpenAI Vision, and maintains
    an internal control state that the Copilot samples at 20 Hz.
    """

    def __init__(self):
        self.active = False
        self.running = True
        self._control: dict = dict(ZERO)
        self._http: aiohttp.ClientSession | None = None
        self._openai: AsyncOpenAI | None = None

        if os.getenv("OPENAI_API_KEY"):
            self._openai = AsyncOpenAI()
            log.info("OpenAI client initialised")
        else:
            log.warning("OPENAI_API_KEY not set — agent will drive blind")

    def get_control(self) -> dict:
        return dict(self._control)

    # ---- camera snapshot ----

    async def _capture_frame(self) -> bytes | None:
        if not CAMERA_SNAPSHOT_URL:
            return None
        try:
            if self._http is None:
                self._http = aiohttp.ClientSession()
            async with self._http.get(
                CAMERA_SNAPSHOT_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.warning("Snapshot HTTP %d", resp.status)
        except Exception as e:
            log.warning("Snapshot error: %s", e)
        return None

    # ---- vision analysis ----

    async def _analyse(self, frame: bytes) -> dict:
        if self._openai is None:
            return dict(ZERO)

        b64 = base64.b64encode(frame).decode()
        try:
            resp = await self._openai.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Analyse this frame and decide the next driving action.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64}",
                                },
                            },
                        ],
                    },
                ],
                max_tokens=200,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            decision = json.loads(raw)
            log.info("Agent: %s", decision.get("reasoning", ""))
            return {
                "forward": 1 if decision.get("forward") else 0,
                "reverse": 1 if decision.get("reverse") else 0,
                "left":    1 if decision.get("left") else 0,
                "right":   1 if decision.get("right") else 0,
                "lights":  1 if decision.get("lights") else 0,
                "turbo":   0,
                "donut":   0,
            }
        except Exception as e:
            log.error("Vision analysis failed: %s", e)
            return dict(ZERO)

    # ---- fallback ----

    @staticmethod
    def _blind_drive() -> dict:
        """No camera / no API key — crawl forward with lights on."""
        return dict(forward=1, reverse=0, left=0, right=0,
                    lights=1, turbo=0, donut=0)

    # ---- main loop ----

    async def run(self):
        log.info(
            "Agent loop started  snapshot=%s  interval=%.1fs",
            CAMERA_SNAPSHOT_URL or "(none)",
            AGENT_INTERVAL,
        )

        while self.running:
            if not self.active:
                self._control = dict(ZERO)
                await asyncio.sleep(0.2)
                continue

            frame = await self._capture_frame()
            if frame and self._openai:
                self._control = await self._analyse(frame)
            elif not self._openai:
                self._control = self._blind_drive()

            await asyncio.sleep(AGENT_INTERVAL)

        if self._http:
            await self._http.close()
