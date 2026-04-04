#!/usr/bin/env python3
"""
Agentic Copilot — main orchestrator.

Connects to the backend's REST API, integrates the Zapbox Link for
manual driving, and delegates to an AI DrivingAgent when the controller
goes idle.

    ┌──────────────┐         ┌──────────────┐
    │  Zapbox Link │────┐    │   Backend    │
    │  (manual)    │    │    │   :3000      │
    └──────────────┘    ▼    └──────┬───────┘
                    ┌────────┐      │
                    │Copilot │◄HTTP─┘
                    └────────┘
                        ▲
    ┌──────────────┐    │
    │  AI Agent    │────┘
    │  (autonomous)│
    └──────────────┘

Backend REST endpoints used:
    POST /car/scan              → scan BLE for Shell Racing cars
    POST /car/connect/{number}  → connect to a scanned car
    POST /car/disconnect        → disconnect
    POST /car/control           → update driving state (called at 20 Hz)
    GET  /car/status            → poll connection / battery info
    GET  /camera/snapshot       → JPEG frame (proxied from go2rtc)

Usage:
    BACKEND_URL=http://host:3000 \\
    OPENAI_API_KEY=sk-... \\
    python copilot.py
"""

import asyncio
import logging
import os
import signal
import socket

from dotenv import load_dotenv

load_dotenv()

import aiohttp

from agent import DrivingAgent
from zapbox_link import ZapboxLink

log = logging.getLogger("copilot")

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:3000").rstrip("/")


class Copilot:
    def __init__(self):
        self.http: aiohttp.ClientSession | None = None
        self.zapbox = ZapboxLink()
        self.agent = DrivingAgent()
        self.mode = "idle"          # idle | manual | autonomous
        self.car_connected = False
        self.car_name = ""
        self.car_battery: int | None = None
        self.running = True

    # ---- HTTP helpers ----

    async def _post(self, path: str, body: dict | None = None) -> dict | None:
        if self.http is None:
            return None
        try:
            async with self.http.post(
                f"{BACKEND_URL}{path}", json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return await resp.json()
        except Exception as e:
            log.warning("POST %s failed: %s", path, e)
            return None

    async def _get(self, path: str) -> dict | None:
        if self.http is None:
            return None
        try:
            async with self.http.get(
                f"{BACKEND_URL}{path}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return await resp.json()
        except Exception as e:
            log.warning("GET %s failed: %s", path, e)
            return None

    # ---- car connection ----

    async def _scan_and_connect(self):
        """Scan for Shell Racing cars and connect to the first one found."""
        log.info("Scanning for cars ...")
        result = await self._post("/car/scan")
        if not result or not result.get("cars"):
            log.warning("No cars found")
            return

        cars = result["cars"]
        log.info(
            "Found %d car(s): %s",
            len(cars), ", ".join(c["name"] for c in cars),
        )

        resp = await self._post(f"/car/connect/{cars[0]['number']}")
        if resp and resp.get("connected"):
            self.car_connected = True
            self.car_name = resp.get("name", "")
            self.car_battery = resp.get("battery")
            log.info(
                "Car connected: %s  battery=%s%%",
                self.car_name, self.car_battery,
            )
        else:
            err = (resp or {}).get("error", "Unknown error")
            log.warning("Connect failed: %s", err)

    # ---- loops ----

    async def _run_status_poller(self):
        """Poll /car/status every 2 s to stay in sync with the backend."""
        while self.running:
            status = await self._get("/car/status")
            if status:
                prev = self.car_connected
                self.car_connected = status.get("connected", False)
                self.car_name = status.get("name", "")
                self.car_battery = status.get("battery")
                if self.car_connected and not prev:
                    log.info(
                        "Car connected: %s  battery=%s%%",
                        self.car_name, self.car_battery,
                    )
                elif not self.car_connected and prev:
                    log.info("Car disconnected")
            await asyncio.sleep(2)

    async def _run_auto_connect(self):
        """Keep trying to connect a car until one sticks."""
        await asyncio.sleep(2)
        while self.running:
            if not self.car_connected:
                await self._scan_and_connect()
            await asyncio.sleep(10)

    async def _run_mode_switcher(self):
        """Watch Zapbox activity and toggle between manual / autonomous / idle."""
        while self.running:
            if not self.car_connected:
                if self.mode != "idle":
                    self.mode = "idle"
                    self.agent.active = False
                await asyncio.sleep(0.5)
                continue

            if self.zapbox.is_active():
                if self.mode != "manual":
                    self.mode = "manual"
                    self.agent.active = False
                    log.info("-> MANUAL  (Zapbox active)")
            else:
                if self.agent.enabled:
                    if self.mode != "autonomous":
                        self.mode = "autonomous"
                        self.agent.active = True
                        log.info("-> AUTONOMOUS  (Agent taking over)")
                else:
                    if self.mode != "idle":
                        self.mode = "idle"
                        self.agent.active = False
                        log.info("-> IDLE  (Agentic mode disabled)")

            await asyncio.sleep(0.1)

    async def _run_control_loop(self):
        """POST /car/control at 20 Hz with whichever source is active."""
        ZERO = dict(
            forward=0, reverse=0, left=0, right=0,
            lights=0, turbo=0, donut=0,
        )
        ctrl_log_count = 0
        while self.running:
            if not self.car_connected or self.http is None:
                await asyncio.sleep(0.1)
                continue

            if self.mode == "manual":
                ctrl = self.zapbox.get_control()
            elif self.mode == "autonomous":
                ctrl = self.agent.get_control()
            else:
                ctrl = ZERO

            ctrl_log_count += 1
            has_input = any(v != 0 for v in ctrl.values())
            if ctrl_log_count % 40 == 1 or has_input:
                log.info(
                    "POST /car/control  mode=%s  zapbox.connected=%s  "
                    "zapbox.active=%s  ctrl=%s",
                    self.mode, self.zapbox.connected,
                    self.zapbox.is_active(), ctrl,
                )

            await self._post("/car/control", ctrl)
            await asyncio.sleep(1.0 / 20)

    # ---- entry ----

    @staticmethod
    def _local_ip() -> str:
        """Best-effort detection of the machine's LAN IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def run(self):
        local_ip = self._local_ip()
        log.info("Agentic Copilot starting")
        log.info("  BACKEND_URL = %s", BACKEND_URL)
        log.info(
            "  Controller → open controller.html on your phone and enter: %s:%d",
            local_ip, self.zapbox.port,
        )
        log.info("  AGENTIC_MODE = %s", "on" if self.agent.enabled else "OFF")

        if not self.agent.enabled:
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("openai").setLevel(logging.WARNING)

        self.http = aiohttp.ClientSession()
        tasks = [
            self._run_status_poller(),
            self._run_auto_connect(),
            self._run_mode_switcher(),
            self._run_control_loop(),
            self.zapbox.run(),
        ]
        if self.agent.enabled:
            tasks.append(self.agent.run())

        try:
            await asyncio.gather(*tasks)
        finally:
            await self.http.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-10s  %(message)s",
        datefmt="%H:%M:%S",
    )

    copilot = Copilot()

    def _shutdown(*_):
        log.info("Shutting down ...")
        copilot.running = False
        copilot.zapbox.running = False
        copilot.agent.running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    asyncio.run(copilot.run())


if __name__ == "__main__":
    main()
