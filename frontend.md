# Frontend Application — Complete Reference

## Overview

The frontend is a React single-page application that serves as the driver's cockpit. It renders a full-screen camera feed with a HUD overlay showing car controls, status indicators, and connection management. It supports two operating modes:

1. **Remote mode** — Connects to the backend server via WebSocket. Car control and camera streaming are relayed through the backend to the Pi. This is the primary mode for internet-based driving.
2. **Direct mode** — Connects directly to the Pi's go2rtc for camera, and uses Web Bluetooth for car control. This only works when the browser is on the same local network as the Pi and the car.

```
┌─────────────────────────────────────────────────────┐
│                   Browser (Frontend)                 │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ useBle   │  │ useWhep  │  │   useBackend     │  │
│  │ (direct) │  │ (direct) │  │   (remote)       │  │
│  │ Web BLE  │  │ go2rtc   │  │   backend WS     │  │
│  │ to car   │  │ WebRTC   │  │   → car + camera │  │
│  └──────────┘  └──────────┘  └──────────────────┘  │
│        └──────────┴──────────────────┘               │
│                       ▼                              │
│                   App.jsx                            │
│              (unified interface)                     │
└─────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component     | Version   | Purpose                              |
|---------------|-----------|--------------------------------------|
| React         | 19.2.4    | UI framework                        |
| Vite          | 8.x       | Build tool and dev server           |
| Tailwind CSS  | 4.2.2     | Utility-first CSS (via Vite plugin) |
| Web Bluetooth | —         | Direct BLE car control (Chrome/Edge)|
| WebRTC        | —         | Camera streaming (all browsers)     |
| WebSocket     | —         | Communication with backend          |

---

## File Structure

```
frontend/src/
├── main.jsx          Entry point — renders <App /> into DOM
├── App.jsx           Main application component (HUD, controls, video)
├── App.css           Minimal custom CSS (rotate-180 utility)
├── index.css         Tailwind CSS imports
├── useBle.js         Hook: direct BLE car control
├── useWhep.js        Hook: direct WebRTC camera (connects to go2rtc)
└── useBackend.js     Hook: remote mode (everything via backend WebSocket)
```

---

## Hook: `useBle.js` — Direct BLE Car Control

This hook uses the Web Bluetooth API to connect directly from the browser to a Shell Racing Legends car. It only works when the car is within Bluetooth range of the device running the browser.

### BLE UUIDs

| UUID                                    | Purpose                      |
|-----------------------------------------|------------------------------|
| `0000fff0-0000-1000-8000-00805f9b34fb` | Control Service              |
| `0000fff1-0000-1000-8000-00805f9b34fb` | Control Characteristic       |
| `0000180f-0000-1000-8000-00805f9b34fb` | Battery Service (standard)   |
| `00002a19-0000-1000-8000-00805f9b34fb` | Battery Level Characteristic |

### Car Name Filters

The hook uses `navigator.bluetooth.requestDevice()` with name prefix filters matching all known Shell Racing car models:

```js
const NAME_FILTERS = [
  'SL-12Cilindri', 'SL-296 GT3', 'SL-296 GTB', 'SL-330 P4(1967)',
  'SL-488 Challenge Evo', 'SL-488 GTE', 'SL-499P', 'SL-499P N',
  'SL-Daytona SP3', 'SL-F1-75', 'SL-FXX-K Evo', 'SL-Purosangue',
  'SL-SF1000', 'SL-SF-23', 'SL-SF-24', 'SL-SF90 Spider',
  'SL-SF90 Spider N', 'SL-Shell Car',
]
```

### State

| State        | Type     | Description                                |
|--------------|----------|--------------------------------------------|
| `status`     | string   | `disconnected`, `connecting`, or `connected` |
| `deviceName` | string   | BLE device name (e.g. `"SL-Daytona SP3"`) |
| `battery`    | number   | Battery percentage (0–100) or `null`       |
| `error`      | string   | Error message or empty string              |

### 8-Byte Control Packet

The control state is stored in a ref and built into a `Uint8Array` on each send:

```js
const buildPacket = () => new Uint8Array([
  1,                    // mode (always 1)
  s.w ? 1 : 0,         // forward
  s.s ? 1 : 0,         // reverse
  s.a ? 1 : 0,         // left
  s.d ? 1 : 0,         // right
  s.lights ? 1 : 0,    // lights
  s.turbo ? 1 : 0,     // turbo
  s.donut ? 1 : 0,     // donut
])
```

### 20 Hz Send Loop

Once connected, a continuous async loop writes the packet every 50ms:

```js
while (running) {
  await charRef.current.writeValueWithoutResponse(buildPacket())
  await new Promise((r) => setTimeout(r, 50))
}
```

The loop uses `writeValueWithoutResponse` for speed (no BLE ACK wait). It sleeps **after** each write, ensuring consistent 20 Hz timing regardless of write duration.

### Connection Flow

1. `navigator.bluetooth.requestDevice()` opens the browser's device picker
2. User selects a Shell Racing car
3. `device.gatt.connect()` establishes GATT connection
4. Get Control Service → Get Control Characteristic → Store in ref
5. Optionally read Battery Level
6. Start 20 Hz send loop
7. Listen for `gattserverdisconnected` event for cleanup

### Returned API

```js
{ status, deviceName, battery, error, connect, disconnect, updateState }
```

`updateState(key, value)` updates the internal state ref. Keys: `w`, `a`, `s`, `d`, `lights`, `turbo`, `donut`.

---

## Hook: `useWhep.js` — Direct WebRTC Camera

This hook connects directly to go2rtc running on the Pi via WebSocket signaling to establish a WebRTC video stream.

### How It Works

1. Creates an `RTCPeerConnection` with Google's public STUN server
2. Adds a `recvonly` video transceiver (we only receive, never send)
3. Opens a WebSocket to go2rtc: `ws://<pi-ip>:1984/api/ws?src=camera`
4. Creates an SDP offer and sends it as `webrtc/offer`
5. Receives SDP answer as `webrtc/answer`, sets it as remote description
6. Exchanges ICE candidates in both directions
7. `ontrack` fires with the video `MediaStream` → attached to `<video>` element

### Callback Ref Pattern

A critical implementation detail: the `ontrack` event can fire **before** the React `<video>` element has mounted into the DOM. To solve this race condition, the hook uses a callback ref pattern:

```js
const videoRef = useCallback((el) => {
  videoElRef.current = el
  if (el && streamRef.current) {
    el.srcObject = streamRef.current  // Attach pending stream
  }
}, [])
```

The `ontrack` handler stores the stream in `streamRef`. When the video element mounts (or if it's already mounted), the stream is attached. This ensures the video always plays regardless of timing.

### Auto-Reconnect

The hook automatically recovers from connection failures:

| ICE State        | Action                                                        |
|------------------|---------------------------------------------------------------|
| `connected`      | Set status to `connected`, clear `connectingRef`              |
| `completed`      | Same as connected                                             |
| `disconnected`   | Wait 2 seconds. If still disconnected, retry connection.      |
| `failed`/`closed`| Cleanup immediately, retry after 1 second.                    |

A `connectingRef` boolean prevents overlapping reconnection attempts.

### Latency Measurement

Every 2 seconds, the hook polls `RTCPeerConnection.getStats()` looking for a `candidate-pair` report with `state: 'succeeded'`. It reads `currentRoundTripTime`, halves it (RTT → one-way latency), and converts to milliseconds.

### Returned API

```js
{ videoRef, status, latency, connect, disconnect }
```

---

## Hook: `useBackend.js` — Remote Mode (Everything via Backend)

This is the unified hook for internet-based control. A single WebSocket to the backend handles car control, car status, and camera signaling.

### State

| State          | Type    | Description                                          |
|----------------|---------|------------------------------------------------------|
| `status`       | string  | `disconnected`, `connecting`, `connected` (backend WS) |
| `piOnline`     | boolean | Whether the Pi agent is connected to the backend     |
| `car`          | object  | `{ connected, name, battery, error, scanning }`      |
| `cameraStatus` | string  | `disconnected`, `connecting`, `connected`             |
| `latency`      | number  | Camera latency in ms (or null)                       |

### Connection Flow

```js
const connect = (url) => {
  // 1. Convert HTTP URL to WS: "http://host:3000" → "ws://host:3000/ws"
  // 2. Open WebSocket
  // 3. On open → setStatus('connected')
  // 4. Listen for messages from backend
}
```

### Message Handling

| Incoming message     | Frontend action                                              |
|----------------------|--------------------------------------------------------------|
| `pi/status`          | Update `piOnline`. If Pi went offline, reset car state and stop camera. |
| `car/status`         | Update `car` state object (connected, name, battery, error, scanning).   |
| `camera/answer`      | Set as WebRTC remote description on the `RTCPeerConnection`. |
| `camera/candidate`   | Add ICE candidate to the `RTCPeerConnection`.                |
| `error`              | Display error message in the car error banner.               |

### Camera: WebRTC via Backend Relay

The camera works exactly like `useWhep.js` but the signaling goes through the backend WebSocket instead of directly to go2rtc:

```
Browser                 Backend               Pi Agent            go2rtc
  │                       │                      │                  │
  │─camera/offer─────────►│──camera/offer+id────►│──webrtc/offer──►│
  │                       │                      │                  │
  │◄─camera/answer────────│◄─camera/answer+id───│◄─webrtc/answer──│
  │                       │                      │                  │
  │◄──camera/candidate───│◄─camera/candidate+id─│◄─webrtc/candidate│
  │──camera/candidate────►│──camera/candidate+id─►│──webrtc/candidate│►
  │                       │                      │                  │
  │◄═══════════════WebRTC media (direct UDP)══════════════════════►│
```

The signaling path is longer but the **actual video frames** still flow directly over UDP once the WebRTC connection is established.

### Camera Auto-Reconnect

Same strategy as `useWhep.js`:
- ICE `disconnected` → wait 2 seconds, then retry
- ICE `failed` → cleanup and retry after 1.5 seconds

### Car Control

```js
const updateState = (key, value) => {
  const map = { w: 'forward', s: 'reverse', a: 'left', d: 'right' }
  const field = map[key] || key
  controlRef.current[field] = value ? 1 : 0
  send({ type: 'control', ...controlRef.current })
}
```

Control commands are sent immediately on each state change (not on a timer). The ref `controlRef` always holds the complete current state, and every `send` includes all fields. This ensures the backend always has the full picture even if messages are lost.

### Backend Auto-Reconnect

When the WebSocket closes unexpectedly:
```js
ws.onclose = () => {
  // Reset all state
  // If urlRef still has a URL (not manually disconnected):
  reconnectRef.current = setTimeout(() => connect(urlRef.current), 3000)
}
```

Manual `disconnect()` clears `urlRef`, preventing auto-reconnect.

### Auto-Start Camera

When in remote mode, if the Pi comes online, the camera starts automatically:
```js
useEffect(() => {
  if (isRemote && backend.piOnline && backend.cameraStatus === 'disconnected') {
    backend.startCamera()
  }
}, [isRemote, backend.piOnline, backend.cameraStatus, backend.startCamera])
```

### Returned API

```js
{
  // Backend connection
  status, piOnline, connect, disconnect,
  // Car (mirrors useBle interface)
  car, connectCar, disconnectCar, updateState,
  // Camera (mirrors useWhep interface)
  videoRef, cameraStatus, latency, startCamera, stopCamera,
}
```

---

## Component: `App.jsx`

### Dual-Mode Architecture

The App component instantiates all three hooks simultaneously:

```jsx
const ble = useBle()          // Direct BLE (always available)
const cam = useWhep(cameraUrl) // Direct camera (always available)
const backend = useBackend()   // Remote mode (active when connected)
```

The flag `isRemote = backend.status === 'connected'` determines which source is used for every piece of state:

```jsx
const carConnected = isRemote ? backend.car.connected : ble.status === 'connected'
const carConnecting = isRemote ? backend.car.scanning : ble.status === 'connecting'
const carName = isRemote ? backend.car.name : ble.deviceName
const carBattery = isRemote ? backend.car.battery : ble.battery
const carError = isRemote ? backend.car.error : ble.error
const camConnected = isRemote ? backend.cameraStatus === 'connected' : cam.status === 'connected'
const camLatency = isRemote ? backend.latency : cam.latency
const camVideoRef = isRemote ? backend.videoRef : cam.videoRef
const camDisconnect = isRemote ? backend.stopCamera : cam.disconnect
```

This unified interface means the entire HUD works identically in both modes. The user doesn't need to know whether they're in remote or direct mode — the controls behave the same.

### Keyboard Controls

Global `keydown`/`keyup` event listeners handle driving and toggle controls:

| Key | Action                                          |
|-----|-------------------------------------------------|
| W   | Forward (press=on, release=off)                 |
| A   | Left (press=on, release=off)                    |
| S   | Reverse (press=on, release=off)                 |
| D   | Right (press=on, release=off)                   |
| L   | Toggle lights                                   |
| T   | Toggle turbo                                    |
| O   | Toggle donut                                    |

Keyboard handlers route to the correct `updateState` function based on `isRemote`:

```jsx
const update = isRemote ? backend.updateState : ble.updateState
```

### Local Storage Persistence

Both URLs are saved to and restored from `localStorage`:

| Key          | Purpose                                   |
|--------------|-------------------------------------------|
| `backendUrl` | Remembered backend server URL             |
| `cameraUrl`  | Remembered direct camera URL              |

On mount, if a `backendUrl` exists in storage, the app automatically connects to it:

```jsx
useEffect(() => {
  const saved = localStorage.getItem('backendUrl')
  if (saved) connectRef.current(saved)
}, [])
```

### UI Layout

The app is a full-screen black canvas with layered elements:

```
┌─────────────────────────────────────────────────────┐
│  Layer 1: Full-screen <video> (or connection form)  │
│                                                      │
│  Layer 2: HUD overlay (pointer-events: none)        │
│  ┌─── Top Bar ────────────────────────────────────┐ │
│  │ Sol Machines  │  latency │ cam │ backend │ car  │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  ┌─── Error Banner ──────────────────────────────┐  │
│  │ "Car not connected — Pi is offline"           │  │
│  └───────────────────────────────────────────────┘  │
│                                                      │
│  ┌─── Bottom HUD ────────────────────────────────┐  │
│  │  [W]          WASD drive · L lights ·  Lights │  │
│  │ [A][S][D]     T turbo · O donut       Turbo   │  │
│  │                                       Donut   │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### Connection Screen (Not Remote, No Camera)

When neither backend nor direct camera is connected, the center shows two options:

1. **Remote Control** — Backend URL input + red Connect button
2. **Camera Only** — Direct go2rtc URL input + blue Connect button

Separated by an "or direct" divider.

### Camera Waiting Screen (Remote, Pi Offline)

When connected to backend but Pi hasn't connected yet:
```
"Waiting for Pi to come online…"
```

### Video Rendering

The `<video>` element uses:
- `autoPlay` — starts playback as soon as media arrives
- `playsInline` — prevents fullscreen on mobile
- `muted` — required for autoplay (browser policy)
- `className="rotate-180"` — rotates 180° because the camera is mounted upside-down

### HUD Components

#### Top Bar
- **Sol Machines** logo (left)
- **Latency** display (`Xms`, only when camera connected)
- **Cam** button with green pulse dot (disconnect camera)
- **Backend** button with green/amber pulse dot (disconnect backend; only in remote mode)
- **Battery** percentage with icon (only when car connected)
- **Car name** text (only when car connected, hidden on mobile)
- **Connect Car** / **Disconnect** / **Scanning…** button

#### Error Banner
Red translucent banner below the top bar. Shows `carError` (from backend `error` messages or BLE failures).

#### Bottom HUD
- **Left:** WASD key indicators. Glow red and scale down when pressed.
- **Center:** Keybind hints (hidden on mobile)
- **Right:** Toggle buttons for Lights (amber), Turbo (red), Donut (purple). Each shows a label, icon, hotkey badge, and active indicator dot.

### Sub-Components

#### `Key({ label, sub, active })`

A 56×56px rounded square that displays a key letter and sublabel. When `active`, it turns red with a glow effect and scales to 95%.

#### `ToggleButton({ label, hotkey, active, onClick, activeColor, icon })`

A 176px-wide button with icon, label, hotkey badge, and status dot. When active, it takes the `activeColor` class (e.g. `bg-amber-500`) and shows a white filled dot.

---

## Vite Configuration (`vite.config.js`)

```js
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://172.20.10.2:1984',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
```

The proxy is configured for development only — it forwards `/api` requests to the Pi's go2rtc when using `useWhep.js` in direct mode via `npm run dev`. In production, the frontend is built as static files and served by the backend.

---

## Build & Deployment

### Development

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173` with hot module replacement.

### Production Build

```bash
cd frontend
npm run build
```

Outputs to `frontend/dist/`. The backend's Express server serves these files.

### Full Stack (production)

```bash
# 1. Build frontend
cd frontend && npm run build && cd ..

# 2. Start backend (serves frontend + WebSocket relay)
cd backend
PORT=3000 PI_TOKEN=mysecret npm start
```

Open `http://your-server:3000` — the backend serves the React app and handles all WebSocket connections.

---

## Complete Control Flow Diagram

### Remote mode: User presses W → Car drives forward

```
1. Browser keydown event ('w')
2. App.jsx handleKeyDown → backend.updateState('w', true)
3. useBackend: controlRef.current.forward = 1
4. useBackend: send({ type: 'control', forward: 1, reverse: 0, ... })
5. WebSocket message → Backend server
6. Backend: sanitise values to 0/1
7. Backend: piSocket.send({ type: 'control', forward: 1, ... })
8. WebSocket message → Pi Agent
9. Pi Agent: self.control_state[1] = 1
10. Pi Agent: run_control loop tick (≤50ms)
11. Pi Agent: BLE write [1, 1, 0, 0, 0, 0, 0, 0] to car
12. Car motor engages
```

### Remote mode: Camera stream connects

```
1. Pi comes online → backend: { type: "pi/status", connected: true }
2. useBackend: setPiOnline(true)
3. App.jsx useEffect detects piOnline → backend.startCamera()
4. useBackend: creates RTCPeerConnection, generates offer
5. useBackend: send({ type: "camera/offer", sdp: "..." })
6. Backend: tags with clientId, forwards to Pi
7. Pi Agent: opens WS to go2rtc, forwards offer
8. go2rtc: streams rpicam-vid, generates answer
9. Pi Agent: forwards answer with clientId
10. Backend: routes to correct client (strips clientId)
11. useBackend: pc.setRemoteDescription(answer)
12. ICE candidates exchanged (both directions, same path)
13. WebRTC peer connection established
14. Video frames: rpicam-vid → go2rtc → WebRTC (UDP) → browser
15. ontrack fires → streamRef set → videoRef callback attaches to <video>
16. User sees live camera feed (rotated 180°)
```

### Direct mode: Camera stream connects

```
1. User enters Pi IP, clicks Connect
2. useWhep: creates RTCPeerConnection
3. useWhep: opens WS to ws://<pi-ip>:1984/api/ws?src=camera
4. useWhep: creates offer, sends as webrtc/offer
5. go2rtc: responds with webrtc/answer + candidates
6. useWhep: sets remote description, adds candidates
7. WebRTC peer connection established → video plays
```

### Direct mode: BLE car control

```
1. User clicks "Connect Car" button
2. useBle: navigator.bluetooth.requestDevice() → browser picker
3. User selects car → GATT connect → get control characteristic
4. useBle: starts 20Hz send loop
5. User presses keys → updateState updates stateRef
6. Next loop tick: buildPacket() reads stateRef, writes to BLE
```
