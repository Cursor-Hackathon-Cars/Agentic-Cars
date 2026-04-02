# Raspberry Pi — Complete Reference

## Overview

The Raspberry Pi Zero 2 W is the physical bridge between the internet and the Shell Racing Legends car. It runs two persistent services:

1. **go2rtc** — A camera streaming server that captures video from the Camera Module 3 and serves it via WebRTC.
2. **Pi Agent** (`agent.py`) — A Python process that connects outbound to the backend server and bridges car control (BLE) and camera signaling (go2rtc).

```
┌───────────────────────────────────────────────────────────────┐
│                     Raspberry Pi Zero 2 W                     │
│                                                               │
│  ┌────────────┐       ┌─────────────────────────────────┐    │
│  │  go2rtc    │◄─WS──►│        Pi Agent (agent.py)      │    │
│  │  :1984     │       │                                  │    │
│  │  rpicam-vid│       │     ┌──── BLE (bleak) ────┐     │    │
│  └────────────┘       │     │  Shell Racing Car   │     │    │
│                       │     └─────────────────────┘     │    │
│                       └──────────┬──────────────────────┘    │
│                                  │ outbound WebSocket         │
└──────────────────────────────────┼───────────────────────────┘
                                   ▼
                          Backend Server (:3000)
```

The Pi makes an **outbound** WebSocket connection to the backend. No inbound ports need to be opened on the Pi's network — this is critical for traversing NATs and firewalls.

---

## Hardware

| Component              | Detail                                                    |
|------------------------|-----------------------------------------------------------|
| Board                  | Raspberry Pi Zero 2 W (quad-core ARM Cortex-A53, 512MB)  |
| OS                     | Raspberry Pi OS Lite Bookworm 64-bit                      |
| Camera                 | Camera Module 3 (IMX708 sensor, 12MP)                    |
| Camera connector       | CSI mini ribbon cable (Pi Zero form factor)               |
| WiFi                   | On-board 2.4GHz 802.11 b/g/n                             |
| Bluetooth              | On-board BLE 4.2                                          |
| Network manager        | NetworkManager (`nmcli`)                                   |
| Static IP (optional)   | Configured via `nmcli connection modify`                   |

---

## Service 1: go2rtc (Camera Streaming)

### What is go2rtc?

go2rtc is a lightweight streaming server that takes a video source and makes it available over WebRTC, RTSP, MSE, and other protocols. In this project it runs as a single static binary.

### Installation

The `setup.sh` script automates this:

```bash
sudo bash pi/setup.sh
```

What it does:
1. Updates system packages (`apt update && apt upgrade`)
2. Verifies `rpicam-vid` is available (part of `rpicam-apps-lite`)
3. Downloads go2rtc v1.9.8 for arm64 from GitHub releases to `/opt/go2rtc/go2rtc`
4. Copies `go2rtc.yaml` to `/opt/go2rtc/go2rtc.yaml`
5. Creates and enables a systemd service

### Configuration File: `go2rtc.yaml`

```yaml
streams:
  camera:
    - "exec:rpicam-vid --width 640 --height 480 --framerate 30 --codec h264 --profile baseline --inline --intra 10 --bitrate 1200000 --nopreview -t 0 -o -#video=h264"

webrtc:
  ice_servers:
    - urls: [stun:stun.l.google.com:19302]

api:
  listen: ":1984"
  origin: "*"
```

#### Stream source breakdown

The stream named `camera` uses the `exec:` source type, which means go2rtc launches a child process and reads its stdout as an H.264 elementary stream.

| rpicam-vid flag        | Meaning                                                      |
|------------------------|--------------------------------------------------------------|
| `--width 640`          | Output width in pixels                                       |
| `--height 480`         | Output height in pixels                                      |
| `--framerate 30`       | 30 frames per second                                         |
| `--codec h264`         | Hardware H.264 encoding (uses the Pi's GPU)                  |
| `--profile baseline`   | H.264 Baseline profile (simplest, most compatible)           |
| `--inline`             | Embed SPS/PPS in the stream (required for go2rtc to parse)   |
| `--intra 10`           | Keyframe every 10 frames (~333ms at 30fps). Faster recovery from packet loss. |
| `--bitrate 1200000`    | Cap at 1.2 Mbps. Prevents WiFi congestion spikes.           |
| `--nopreview`          | Don't open a preview window (headless Pi)                    |
| `-t 0`                 | Run indefinitely (no timeout)                                |
| `-o -`                 | Output to stdout                                             |

The `#video=h264` suffix after `-o -` is a go2rtc format hint. It tells go2rtc that the pipe contains raw H.264 data. The `#` must be inside double quotes in YAML since `#` normally starts a comment.

#### WebRTC section

```yaml
webrtc:
  ice_servers:
    - urls: [stun:stun.l.google.com:19302]
```

Configures the STUN server for WebRTC ICE candidate discovery. This helps peers discover each other's public IPs.

#### API section

```yaml
api:
  listen: ":1984"
  origin: "*"
```

- **listen:** go2rtc's HTTP/WS API runs on port 1984 on all interfaces.
- **origin: "\*":** Allows WebSocket connections from any origin. Required because the Pi agent connects from `localhost` and go2rtc would otherwise reject the connection if the Origin header doesn't match.

### Systemd Service: `go2rtc.service`

```ini
[Unit]
Description=go2rtc camera streamer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/go2rtc
ExecStart=/opt/go2rtc/go2rtc -config /opt/go2rtc/go2rtc.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- Starts after network is available
- Auto-restarts on crash (5 second delay)
- Runs as a simple foreground process

### go2rtc WebSocket API

The Pi agent communicates with go2rtc over its WebSocket signaling API:

```
ws://127.0.0.1:1984/api/ws?src=camera
```

The `?src=camera` parameter tells go2rtc which stream to bind this WebSocket session to.

**Protocol (go2rtc ↔ Pi agent):**

| Direction       | Message                                                | Purpose                      |
|-----------------|--------------------------------------------------------|------------------------------|
| Agent → go2rtc  | `{ "type": "webrtc/offer", "value": "<SDP>" }`       | Forward client's SDP offer   |
| go2rtc → Agent  | `{ "type": "webrtc/answer", "value": "<SDP>" }`      | SDP answer for the client    |
| go2rtc → Agent  | `{ "type": "webrtc/candidate", "value": "<candidate>" }` | ICE candidate               |
| Agent → go2rtc  | `{ "type": "webrtc/candidate", "value": "<candidate>" }` | Forward client's ICE candidate |

---

## Service 2: Pi Agent (`agent.py`)

### What it does

The Pi agent is a Python asyncio application that:
1. Connects outbound to the backend server via WebSocket
2. Receives car control commands and writes them to the Shell Racing car over BLE
3. Relays WebRTC camera signaling between clients (via backend) and the local go2rtc instance

### Dependencies

```
bleak        — BLE communication library (scans for and connects to the car)
websockets   — WebSocket client library (connects to backend and go2rtc)
```

Install with:
```bash
pip install -r pi/requirements.txt
```

### Environment Variables

| Variable      | Default                    | Purpose                                      |
|---------------|----------------------------|----------------------------------------------|
| `BACKEND_URL` | `ws://localhost:3000`     | Backend server URL                           |
| `PI_TOKEN`    | `changeme`                 | Shared secret matching the backend's token   |
| `GO2RTC_URL`  | `ws://127.0.0.1:1984`    | Local go2rtc WebSocket API                   |

### Class: `PiAgent`

The entire agent is a single class with the following state:

```python
self.backend_ws = None              # Current WebSocket to the backend
self.ble_client = None              # Current BleakClient to the car
self.car_connected = False          # Is the car currently connected
self.car_name = ''                  # Device name of the connected car
self.control_state = bytearray([1, 0, 0, 0, 0, 0, 0, 0])  # Current packet
self.camera_sessions = {}           # clientId → (go2rtc_ws, relay_task)
self.running = True                 # Shutdown flag
```

### Backend Connection (`run_backend`)

```python
async def run_backend(self):
    ws_url = f'{BACKEND_URL}/ws/pi?token={PI_TOKEN}'
    while self.running:
        try:
            async with websockets.connect(ws_url) as ws:
                self.backend_ws = ws
                async for raw in ws:
                    await self.handle_message(json.loads(raw))
        except (websockets.ConnectionClosed, OSError):
            ...
        finally:
            self.backend_ws = None
        await asyncio.sleep(RECONNECT_DELAY)  # 3 seconds
```

This is an infinite reconnect loop:
1. Connect to `ws://<backend>/ws/pi?token=<token>`
2. Read messages until the connection drops
3. Wait 3 seconds, try again

The agent never gives up. If the backend is down, it just keeps retrying.

### Message Dispatch (`handle_message`)

When a message arrives from the backend:

| `type`              | Action                                                       |
|---------------------|--------------------------------------------------------------|
| `control`           | Update `self.control_state` bytearray from message fields    |
| `car/connect`       | Spawn `connect_car()` as async task                          |
| `car/disconnect`    | Call `disconnect_car()`                                      |
| `camera/offer`      | Spawn `start_camera_session()` as async task                 |
| `camera/candidate`  | Forward ICE candidate to the correct go2rtc session          |
| `camera/stop`       | Tear down the camera session for the given clientId          |

### Car BLE Connection

#### The BLE Protocol

Shell Racing Legends cars expose a GATT service with UUID `0000fff0` and a writable characteristic `0000fff1`. The car expects an 8-byte packet at roughly 20Hz:

| Byte | Field   | Values       |
|------|---------|--------------|
| 0    | Mode    | Always `1`   |
| 1    | Forward | `0` or `1`   |
| 2    | Reverse | `0` or `1`   |
| 3    | Left    | `0` or `1`   |
| 4    | Right   | `0` or `1`   |
| 5    | Lights  | `0` or `1`   |
| 6    | Turbo   | `0` or `1`   |
| 7    | Donut   | `0` or `1`   |

There is also a standard Battery Service (`0x180F`) with Battery Level Characteristic (`0x2A19`) that returns a single byte percentage.

#### Scanning (`connect_car`)

1. Agent sends `car/status { scanning: true }` to backend (which broadcasts to all clients)
2. Runs `BleakScanner.discover(timeout=10)` — scans for BLE devices for 10 seconds
3. Looks for any device whose name starts with `SL-` (all Shell Racing cars use this prefix)
4. If found, connects with `BleakClient`, reads battery level, and reports success
5. If not found, reports error `"No car found"`

#### Control Loop (`run_control`)

```python
async def run_control(self):
    while self.running:
        if self.car_connected and self.ble_client:
            try:
                await self.ble_client.write_gatt_char(
                    CHAR_UUID, bytes(self.control_state), response=False
                )
            except Exception:
                # Mark disconnected
                ...
        await asyncio.sleep(1.0 / 20)  # 50ms = 20Hz
```

This loop runs **continuously** at 20 Hz regardless of whether control commands are arriving. If the car is connected, it writes whatever `self.control_state` currently contains. When a `control` message arrives from the backend, it simply updates the bytearray — the next tick of the loop sends the new state.

`response=False` means "write without response" (BLE write command vs write request). This is faster and reduces latency since we don't wait for an ACK.

If a write fails (car walked out of range, battery died, etc.), the agent marks the car as disconnected and notifies the backend.

#### Disconnection (`disconnect_car`)

1. Calls `self.ble_client.disconnect()`
2. Resets `control_state` to all zeros (stops the car)
3. Reports `car/status { connected: false }` to backend

### Camera Session Management

Each browser client gets its own independent camera session. This allows multiple people to watch the camera simultaneously.

#### Starting a Session (`start_camera_session`)

When a `camera/offer` arrives (containing a client's SDP offer and their UUID):

1. If an existing session for this `clientId` exists, tear it down first
2. Open a new WebSocket to go2rtc: `ws://127.0.0.1:1984/api/ws?src=camera`
3. Forward the SDP offer to go2rtc: `{ "type": "webrtc/offer", "value": "<sdp>" }`
4. Spawn an async relay task that reads go2rtc responses and forwards them back through the backend

#### Relay Task (`_relay_go2rtc`)

```python
async def _relay_go2rtc(self, client_id, go2rtc_ws):
    async for raw in go2rtc_ws:
        msg = json.loads(raw)
        if msg['type'] == 'webrtc/answer':
            await self.send({
                'type': 'camera/answer',
                'clientId': client_id,
                'sdp': msg['value'],
            })
        elif msg['type'] == 'webrtc/candidate':
            await self.send({
                'type': 'camera/candidate',
                'clientId': client_id,
                'candidate': msg['value'],
            })
```

Each go2rtc WebSocket is bound to a specific client. The relay translates between go2rtc's message format (`webrtc/answer`, `webrtc/candidate` with `value`) and the backend's format (`camera/answer`, `camera/candidate` with `sdp`/`candidate` + `clientId`).

#### ICE Candidate Forwarding (`forward_ice_candidate`)

When a client sends an ICE candidate via the backend, the agent looks up the go2rtc WebSocket for that `clientId` and forwards:
```python
await go2rtc_ws.send(json.dumps({
    'type': 'webrtc/candidate',
    'value': msg['candidate'],
}))
```

#### Stopping a Session (`stop_camera_session`)

Called when a client disconnects from the backend (the backend sends `camera/stop`):
1. Remove the session from `camera_sessions`
2. Cancel the relay task
3. Close the go2rtc WebSocket

### Concurrency Model

The agent uses Python's `asyncio` with two main coroutines running in parallel via `asyncio.gather`:

```python
await asyncio.gather(
    self.run_backend(),    # Backend WS connection + message handling
    self.run_control(),    # BLE write loop at 20Hz
)
```

Additional tasks are spawned dynamically:
- `connect_car()` — spawned per `car/connect` request
- `start_camera_session()` — spawned per `camera/offer`
- `_relay_go2rtc()` — spawned per camera session

### Shutdown

The agent listens for `SIGINT` (Ctrl+C) and `SIGTERM` (systemd stop):
```python
signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)
```

Setting `agent.running = False` causes all loops to exit gracefully.

### Systemd Service: `agent.service`

```ini
[Unit]
Description=Shell Racing Pi Agent
After=network-online.target go2rtc.service
Wants=network-online.target

[Service]
Type=simple
Environment=BACKEND_URL=ws://YOUR_BACKEND:3000
Environment=PI_TOKEN=changeme
Environment=GO2RTC_URL=ws://127.0.0.1:1984
ExecStart=/usr/bin/python3 /opt/pi-agent/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

- Starts **after** go2rtc (so the camera API is available)
- Auto-restarts on crash
- Environment variables are configured directly in the unit file

### Deployment on the Pi

```bash
# Install Python dependencies
pip install -r pi/requirements.txt

# Copy agent to Pi
sudo mkdir -p /opt/pi-agent
sudo cp pi/agent.py /opt/pi-agent/agent.py

# Edit the service file with your backend URL and token
nano pi/agent.service

# Install and start the service
sudo cp pi/agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-agent
```

---

## WiFi Configuration

The Pi uses NetworkManager (default on Bookworm). WiFi is configured with `nmcli`:

```bash
# Connect to a network
sudo nmcli device wifi connect "NetworkName" password "password"

# Set static IP (optional)
sudo nmcli connection modify "NetworkName" \
  ipv4.method manual \
  ipv4.addresses 172.20.10.2/24 \
  ipv4.gateway 172.20.10.1

# Verify
nmcli connection show "NetworkName"
```

NetworkManager automatically reconnects on boot — no additional services needed.

---

## Complete Data Flow

### Car control (keypress to wheel spin)

```
Browser: W key down
  → useBackend.updateState('w', true)
  → WebSocket: { type: "control", forward: 1, ... }
  → Backend server (sanitises values)
  → WebSocket: { type: "control", forward: 1, ... }
  → Pi Agent: self.control_state[1] = 1
  → run_control tick (within 50ms)
  → BLE write: [1, 1, 0, 0, 0, 0, 0, 0]
  → Car motor activates
```

### Camera (offer to video frame)

```
Browser: startCamera()
  → RTCPeerConnection.createOffer()
  → WebSocket: { type: "camera/offer", sdp: "..." }
  → Backend (tags clientId)
  → WebSocket: { type: "camera/offer", sdp: "...", clientId: "abc" }
  → Pi Agent: start_camera_session()
  → websockets.connect("ws://127.0.0.1:1984/api/ws?src=camera")
  → go2rtc: { type: "webrtc/offer", value: "..." }
  → go2rtc: { type: "webrtc/answer", value: "..." }
  → Pi Agent: { type: "camera/answer", clientId: "abc", sdp: "..." }
  → Backend (routes to client "abc")
  → Browser: pc.setRemoteDescription(answer)
  → ICE candidates exchanged (both directions, same path)
  → WebRTC direct media path established:
      rpicam-vid → go2rtc → WebRTC → Browser <video>
```

Note: The actual video frames travel **directly** via WebRTC (UDP) from the Pi to the browser. Only the signaling (SDP offers/answers and ICE candidates) goes through the backend. This keeps latency minimal.
