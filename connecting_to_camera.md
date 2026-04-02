# Connecting to the Pi Camera Stream — Step-by-Step Guide

This document walks through every step needed to get from a cold Pi to a live camera stream in the browser.

---

## Prerequisites

- Raspberry Pi Zero 2 W with Camera Module 3 connected via CSI mini ribbon cable
- Pi OS Lite Bookworm 64-bit flashed and booted
- A way to reach the Pi (USB cable serial, SSH, or local network)

---

## Step 1: Connect to the Pi

### Option A: USB Serial Cable

Plug a USB cable between your computer and the Pi's USB data port (not the power-only port).

```bash
# Find the device
sudo dmesg | tail
# Look for something like: cdc_acm 1-1:1.0: ttyACM7: USB ACM device

# Open a serial console (baud rate 115200)
sudo screen /dev/ttyACM7 115200
```

Login with your Pi credentials. You're now on the Pi's shell.

### Option B: SSH over WiFi

If the Pi is already on your network:

```bash
ssh pi@172.20.10.2
```

(Use whatever IP/hostname your Pi has.)

---

## Step 2: Verify the Camera Works

Before setting up any streaming, confirm the camera hardware is detected:

```bash
rpicam-hello --list-cameras
```

You should see output listing the IMX708 sensor. If not, check:
- The ribbon cable is seated properly (contacts face the board on the Pi Zero)
- The camera connector latch is closed
- `dtoverlay=camera_auto_detect` is in `/boot/firmware/config.txt` (default on Bookworm)

Quick test — capture a 2-second clip:

```bash
rpicam-vid --width 640 --height 480 --framerate 30 --codec h264 -t 2000 -o test.h264
```

If this produces a file without errors, the camera is working.

---

## Step 3: Run the Setup Script

From your development machine, copy the `pi/` folder to the Pi:

```bash
scp -r pi/ pi@172.20.10.2:~/pi/
```

Then on the Pi:

```bash
cd ~/pi
sudo bash setup.sh
```

This script does the following:
1. Updates system packages
2. Verifies `rpicam-vid` is installed
3. Downloads **go2rtc v1.9.8** (arm64 binary) to `/opt/go2rtc/`
4. Copies `go2rtc.yaml` configuration to `/opt/go2rtc/`
5. Creates and enables a systemd service so go2rtc starts on boot

---

## Step 4: Verify go2rtc is Running

```bash
sudo systemctl status go2rtc
```

You should see `active (running)`. If it failed, check logs:

```bash
sudo journalctl -u go2rtc -f
```

Common issues:
- **"exec/pipe: EOF"** — go2rtc can't parse the rpicam-vid output. Make sure the `#video=h264` hint is at the end of the exec command in `go2rtc.yaml`.
- **"no cameras available"** — the camera isn't detected. Go back to Step 2.

### Test via go2rtc Dashboard

From a browser on the same network, open:

```
http://172.20.10.2:1984
```

Click **"stream"** → **"camera"** → **"WebRTC"**. You should see the live camera feed right in the dashboard. If this works, go2rtc is correctly capturing and serving the stream.

---

## Step 5: Connect from the Frontend (Direct Mode)

With go2rtc confirmed working, start the frontend dev server on your machine:

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173` in Chrome/Edge. You'll see the connection screen. Under **"Camera Only"**:

1. Enter `http://172.20.10.2:1984` (the Pi's go2rtc address)
2. Click **Connect**

What happens behind the scenes:
1. The `useWhep` hook creates an `RTCPeerConnection`
2. Opens a WebSocket to `ws://172.20.10.2:1984/api/ws?src=camera`
3. Sends an SDP offer via the WebSocket
4. go2rtc responds with an SDP answer and ICE candidates
5. WebRTC negotiation completes → video frames flow over UDP
6. The `<video>` element displays the live feed (rotated 180° because the camera is mounted upside-down)

The camera URL is saved to `localStorage` so you won't need to re-enter it next time.

---

## Step 6: Set Up the Backend + Pi Agent (Remote Mode)

For internet access (friends driving your car remotely), you need the backend relay and the Pi agent.

### On your server (or local machine for testing):

```bash
cd backend
PI_TOKEN=your-secret-token npm start
```

The backend is now running on port 3000.

### On the Pi:

Install the Python agent dependencies:

```bash
pip install -r ~/pi/requirements.txt
```

Install the agent as a service:

```bash
sudo mkdir -p /opt/pi-agent
sudo cp ~/pi/agent.py /opt/pi-agent/agent.py
```

Edit the service file with your backend URL and token:

```bash
nano ~/pi/agent.service
```

Change:
```ini
Environment=BACKEND_URL=ws://YOUR_BACKEND:3000
Environment=PI_TOKEN=your-secret-token
```

Install and start:

```bash
sudo cp ~/pi/agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-agent
```

Verify it connected:

```bash
sudo journalctl -u pi-agent -f
```

You should see:
```
Pi agent starting…
  Backend : ws://your-server:3000
  go2rtc  : ws://127.0.0.1:1984
Connected to backend: ws://your-server:3000
```

### On the backend terminal, you'll see:

```
Pi agent connected
```

---

## Step 7: Connect from the Frontend (Remote Mode)

Open `http://your-server:3000` in any browser (anywhere on the internet).

Under **"Remote Control"**:
1. Enter `http://your-server:3000` (or just leave it as-is since the frontend is already served from there)
2. Click **Connect**

What happens:
1. The `useBackend` hook opens a WebSocket to `ws://your-server:3000/ws`
2. Backend responds with `pi/status: connected` (assuming the Pi agent is online)
3. The frontend automatically starts the camera (no manual step needed)
4. An SDP offer is sent through: Browser → Backend → Pi Agent → go2rtc
5. The SDP answer comes back: go2rtc → Pi Agent → Backend → Browser
6. ICE candidates are exchanged the same way
7. Once WebRTC negotiation completes, video flows **directly** over UDP: go2rtc → Browser
8. The live feed appears (rotated 180°)

You can now also click **"Connect Car"** — the Pi agent will scan for BLE Shell Racing cars and connect to the first one it finds.

---

## Troubleshooting

### Camera feed not appearing

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| go2rtc dashboard works but frontend doesn't | WebSocket blocked by CORS or firewall | Ensure `origin: "*"` is in `go2rtc.yaml`. Check browser console for WS errors. |
| ICE state stuck at "checking" | NAT/firewall blocking UDP | Add a TURN server to the ICE configuration |
| Video element is black but "connected" | Race condition (stream attached before element mounted) | This is handled by the callback ref pattern in the code. Try refreshing. |
| Feed shows but is upside-down | Camera mounted upside-down | Already handled — the `rotate-180` CSS class flips it. |
| Feed is choppy/freezing | WiFi bandwidth insufficient | Lower resolution/framerate in `go2rtc.yaml`, or reduce `--bitrate` |

### Pi agent not connecting to backend

| Symptom | Fix |
|---------|-----|
| `Backend lost (ConnectionRefused)` | Check the backend is running and the URL is correct |
| `Backend lost (401)` | `PI_TOKEN` doesn't match between agent and backend |
| Agent connects but car scan fails | Ensure the car is powered on and within BLE range of the Pi |

### Checking service status

```bash
# go2rtc (camera)
sudo systemctl status go2rtc
sudo journalctl -u go2rtc -f

# Pi agent
sudo systemctl status pi-agent
sudo journalctl -u pi-agent -f
```

### Restarting after config changes

```bash
# After editing go2rtc.yaml:
sudo cp ~/pi/go2rtc.yaml /opt/go2rtc/go2rtc.yaml
sudo systemctl restart go2rtc

# After editing agent.py or agent.service:
sudo cp ~/pi/agent.py /opt/pi-agent/agent.py
sudo systemctl daemon-reload
sudo systemctl restart pi-agent
```

---

## Network Diagram

```
  Your Phone / Laptop                Your Server               Raspberry Pi
  ──────────────────                ─────────────              ──────────────
  Browser (frontend)                 Backend :3000              go2rtc :1984
       │                                │                          │
       │◄──── serves HTML/JS ──────────│                          │
       │                                │                          │
       │── WS /ws ─────────────────────►│                          │
       │                                │◄── WS /ws/pi?token= ────│ (outbound)
       │                                │                          │
       │── camera/offer ──────────────►│── camera/offer+id ──────►│
       │                                │                          │── webrtc/offer ──►go2rtc
       │                                │                          │◄─ webrtc/answer ──go2rtc
       │◄─ camera/answer ─────────────│◄─ camera/answer+id ──────│
       │                                │                          │
       │◄══════════ WebRTC video (direct UDP) ════════════════════►│
       │                                │                          │
       │── control {forward:1} ────────►│── control ──────────────►│
       │                                │                          │── BLE write ──► 🏎️ Car
```

Only signaling goes through the backend. The actual video frames travel directly over UDP between the Pi and the browser via WebRTC.
