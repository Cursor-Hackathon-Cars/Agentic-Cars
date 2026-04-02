# Backend Server — Complete Reference

## Overview

The backend server is the **single centralised relay** between web browser clients (anywhere on the internet) and the Raspberry Pi that physically controls the car and streams the camera. It is a Node.js application built on Express and the `ws` WebSocket library.

No client ever talks directly to the Pi. Every control command, camera signaling message, and status update flows through this server.

```
┌─────────────┐         ┌──────────────┐         ┌──────────────────┐
│  Browser(s)  │◄──WS──►│   Backend    │◄──WS──►│  Pi Agent        │
│  /ws         │         │  :3000       │         │  /ws/pi?token=   │
└─────────────┘         └──────────────┘         └──────────────────┘
```

---

## Technology Stack

| Component         | Detail                                  |
|-------------------|-----------------------------------------|
| Runtime           | Node.js (ESM modules, `"type": "module"`) |
| HTTP framework    | Express 4.x                             |
| WebSocket library | `ws` 8.x                                |
| Port (default)    | 3000 (configurable via `PORT` env var)  |
| Authentication    | Shared secret `PI_TOKEN` env var        |

---

## File: `backend/server.js`

### Imports and Setup

```js
import express from 'express'
import { createServer } from 'http'
import { WebSocketServer, WebSocket } from 'ws'
import { randomUUID } from 'crypto'
```

A single HTTP server is created from Express and shared with the WebSocket server. The `ws` library is initialised in `noServer` mode — meaning the server itself handles the HTTP `Upgrade` request and decides which handler to invoke based on the URL path.

### Environment Variables

| Variable   | Default      | Purpose                                     |
|------------|--------------|---------------------------------------------|
| `PORT`     | `3000`       | TCP port the server listens on              |
| `PI_TOKEN` | `changeme`   | Shared secret the Pi agent must present     |

If `PI_TOKEN` is left at the default, the server prints a warning at startup:
```
⚠️  Using default PI_TOKEN — set PI_TOKEN env var for production
```

---

## HTTP Endpoints

### `GET /health`

Returns `{ "ok": true }`. Useful for load balancers, uptime monitors, or deployment health checks.

### `GET *` (catch-all)

Serves the frontend's production build from `../frontend/dist/`. This means once the frontend is built (`npm run build` in the frontend directory), the backend serves the entire single-page application. Any route that doesn't match a static file falls back to `index.html`, enabling client-side routing.

---

## WebSocket Endpoints

The server intercepts the HTTP `Upgrade` event (the mechanism browsers use to establish a WebSocket) and routes based on the URL path:

### `/ws/pi` — Pi Agent Connection

**Who connects:** The Python agent running on the Raspberry Pi.

**Authentication:** The Pi must include its token as a query parameter:
```
ws://backend-host:3000/ws/pi?token=<PI_TOKEN>
```

If the token is missing or incorrect, the server responds with `HTTP 401 Unauthorized` and destroys the socket immediately. This prevents unauthorised devices from impersonating the Pi.

**Single connection model:** Only one Pi can be connected at a time. If a new Pi connects while an old one is still active, the old connection is closed with WebSocket close code `4000` and message `"Replaced by new connection"`.

**On connection:**
1. The server stores the WebSocket reference in the global `piSocket` variable.
2. All connected browser clients are immediately notified via broadcast:
   ```json
   { "type": "pi/status", "connected": true }
   ```

**Message handling from Pi:**

The server only accepts these three message types from the Pi (defined in the `PI_MESSAGE_TYPES` set):

| Message Type         | What happens                                                                                     |
|----------------------|--------------------------------------------------------------------------------------------------|
| `car/status`         | Broadcast to all connected browser clients. Contains `connected`, `name`, `battery`, `error`, `scanning` fields. |
| `camera/answer`      | Routed to a **specific** client identified by `clientId`. The `clientId` is stripped before forwarding. Contains the SDP answer from go2rtc. |
| `camera/candidate`   | Routed to a **specific** client identified by `clientId`. Contains an ICE candidate from go2rtc.  |

Any message with an unrecognised type is silently dropped.

**On disconnect:**
1. `piSocket` is set to `null`.
2. All clients are notified:
   ```json
   { "type": "pi/status", "connected": false }
   ```
   ```json
   { "type": "car/status", "connected": false, "name": "", "battery": null }
   ```

### `/ws` — Browser Client Connection

**Who connects:** Any web browser running the frontend application.

**No authentication required** — this is the public-facing endpoint. (In production, you'd restrict access via HTTPS, Cloudflare Access, or similar.)

**On connection:**
1. A unique `clientId` is generated using `crypto.randomUUID()`.
2. The client's WebSocket is stored in the `clients` Map: `clientId → WebSocket`.
3. The client is immediately sent the current Pi status:
   ```json
   { "type": "pi/status", "connected": true/false }
   ```

**Message handling from clients:**

If the Pi is offline when a client sends a message, the server responds with an error rather than silently dropping it:

| Client message type  | Error response                                         |
|----------------------|--------------------------------------------------------|
| `car/connect`        | `{ "type": "error", "message": "Car not connected — Pi is offline" }` |
| `car/disconnect`     | `{ "type": "error", "message": "Car not connected — Pi is offline" }` |
| `control`            | `{ "type": "error", "message": "Car not connected — Pi is offline" }` |
| `camera/offer`       | `{ "type": "error", "message": "Camera not connected — Pi is offline" }` |

When the Pi **is** connected, messages are forwarded as follows:

| Client → Backend                    | Backend → Pi                                              |
|-------------------------------------|-----------------------------------------------------------|
| `{ type: "control", forward: 1, … }` | Same but all values sanitised to `0` or `1`              |
| `{ type: "car/connect" }`           | `{ type: "car/connect" }`                                |
| `{ type: "car/disconnect" }`        | `{ type: "car/disconnect" }`                             |
| `{ type: "camera/offer", sdp: "…" }`| `{ type: "camera/offer", sdp: "…", clientId: "uuid" }`  |
| `{ type: "camera/candidate", candidate: "…" }` | `{ type: "camera/candidate", candidate: "…", clientId: "uuid" }` |

Key details:
- **Control sanitisation:** Every control field (`forward`, `reverse`, `left`, `right`, `lights`, `turbo`, `donut`) is coerced to exactly `0` or `1` using ternary operators. This prevents malicious clients from injecting unexpected values.
- **ClientId tagging:** Camera signaling messages are tagged with the sending client's UUID so the Pi agent can manage separate go2rtc sessions per client and route responses back correctly.
- **SDP type validation:** The `sdp` and `candidate` fields are checked to be strings before forwarding. Non-string values cause the message to be silently dropped.

**On disconnect:**
1. The client is removed from the `clients` Map.
2. If the Pi is online, the server tells it to tear down the camera session for this client:
   ```json
   { "type": "camera/stop", "clientId": "uuid" }
   ```

---

## Global State

The server maintains minimal state:

```js
let piSocket = null           // The single Pi agent WebSocket (or null)
const clients = new Map()     // clientId (UUID string) → WebSocket
```

There is no database, no session store, no persistence. If the server restarts, the Pi agent auto-reconnects, and browser clients auto-reconnect (handled by the frontend's `useBackend` hook).

---

## Broadcast Function

```js
function broadcast(msg) {
  const data = JSON.stringify(msg)
  for (const [, ws] of clients) {
    if (ws.readyState === WebSocket.OPEN) ws.send(data)
  }
}
```

Sends a JSON message to **every** connected browser client. Used for:
- `pi/status` notifications
- `car/status` updates

Camera signaling messages are **not** broadcast — they're routed to specific clients.

---

## Security Model

1. **Pi authentication:** The Pi must present the correct `PI_TOKEN` to connect. Without it, the socket is destroyed before the upgrade completes.
2. **Input sanitisation:** All control values from clients are coerced to `0` or `1`. SDP and candidate fields are type-checked.
3. **Single Pi:** Only one Pi connection is active at a time. A new connection replaces the old one.
4. **Message type whitelist:** The server only processes known message types from both Pi and clients. Unrecognised types are dropped.
5. **Client isolation:** Camera sessions are isolated per-client via UUID. One client cannot interfere with another client's camera stream.

---

## Static File Serving

The backend doubles as a web server for the frontend:

```js
app.use(express.static(join(__dirname, '../frontend/dist')))
app.get('*', (_req, res) => {
  res.sendFile(join(__dirname, '../frontend/dist/index.html'))
})
```

After running `npm run build` in the `frontend/` directory, the compiled React app lands in `frontend/dist/`. The backend serves those files and falls back to `index.html` for any unrecognised path (standard SPA behaviour).

---

## Running the Backend

### Development
```bash
cd backend
PI_TOKEN=mysecret npm run dev
```
Uses `node --watch` for auto-restart on file changes.

### Production
```bash
cd backend
PORT=3000 PI_TOKEN=mysecret npm start
```

### Message Flow Example (Car Control)

1. User presses `W` key in browser
2. Frontend sends: `{ "type": "control", "forward": 1, "reverse": 0, "left": 0, "right": 0, "lights": 0, "turbo": 0, "donut": 0 }`
3. Backend sanitises values and forwards to Pi: `{ "type": "control", "forward": 1, "reverse": 0, ... }`
4. Pi agent writes BLE packet `[1, 1, 0, 0, 0, 0, 0, 0]` to the car

### Message Flow Example (Camera Stream)

1. Frontend creates `RTCPeerConnection`, generates SDP offer
2. Frontend sends: `{ "type": "camera/offer", "sdp": "<offer SDP>" }`
3. Backend tags with clientId and forwards to Pi: `{ "type": "camera/offer", "sdp": "...", "clientId": "abc-123" }`
4. Pi agent opens a WebSocket to local go2rtc, forwards the offer as `webrtc/offer`
5. go2rtc responds with `webrtc/answer` and ICE candidates
6. Pi agent sends back: `{ "type": "camera/answer", "clientId": "abc-123", "sdp": "<answer SDP>" }`
7. Backend strips `clientId`, routes to the correct client
8. Frontend sets the remote description → video stream starts
