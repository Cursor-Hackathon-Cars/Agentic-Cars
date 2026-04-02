// ==========================================================================
// Agentic Cars — Frontend Application
// Connects to backend via WebSocket, streams camera via WebRTC relay,
// provides keyboard + click controls for driving the car.
// ==========================================================================

(function () {
  "use strict";

  // ---- DOM refs ----
  const $ = (id) => document.getElementById(id);
  const videoEl = $("camera-video");
  const connectScreen = $("connect-screen");
  const waitingScreen = $("waiting-screen");
  const hud = $("hud");
  const urlInput = $("backend-url");
  const btnConnect = $("btn-connect");
  const btnDisconnectBackend = $("btn-disconnect-backend");
  const btnConnectCar = $("btn-connect-car");
  const btnDisconnectCar = $("btn-disconnect-car");
  const scanningLabel = $("scanning-label");
  const piStatusDot = $("pi-status");
  const camStatusDot = $("cam-status");
  const latencyBadge = $("latency");
  const batteryDisplay = $("battery-display");
  const carNameEl = $("car-name");
  const errorBanner = $("error-banner");
  const btnLights = $("btn-lights");
  const btnTurbo = $("btn-turbo");
  const btnDonut = $("btn-donut");

  // ---- State ----
  let ws = null;
  let pc = null; // RTCPeerConnection
  let reconnectTimer = null;
  let latencyInterval = null;
  let backendUrl = null; // current URL (null = not connected)

  const state = {
    piOnline: false,
    car: { connected: false, name: "", battery: null, error: "", scanning: false },
    cameraStatus: "disconnected", // disconnected | connecting | connected
    latency: null,
  };

  const control = {
    forward: 0, reverse: 0, left: 0, right: 0,
    lights: 0, turbo: 0, donut: 0,
  };

  // ---- LocalStorage ----
  const STORAGE_KEY = "backendUrl";

  function saveUrl(url) {
    try { localStorage.setItem(STORAGE_KEY, url); } catch {}
  }
  function loadUrl() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; } catch { return ""; }
  }
  function clearUrl() {
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
  }

  // ---- UI updates ----
  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function renderUI() {
    const connected = ws && ws.readyState === WebSocket.OPEN;

    // Screens
    if (!connected) {
      show(connectScreen);
      hide(waitingScreen);
      hide(hud);
      return;
    }

    hide(connectScreen);

    if (!state.piOnline) {
      show(waitingScreen);
      hide(hud);
      return;
    }

    hide(waitingScreen);
    show(hud);

    // Pi status dot
    piStatusDot.className = "status-dot " + (state.piOnline ? "dot-on" : "dot-off");

    // Camera status dot
    camStatusDot.className = "status-dot " + (
      state.cameraStatus === "connected" ? "dot-on" :
      state.cameraStatus === "connecting" ? "dot-warn" : "dot-off"
    );

    // Latency
    if (state.cameraStatus === "connected" && state.latency != null) {
      latencyBadge.textContent = state.latency + "ms";
      show(latencyBadge);
    } else {
      hide(latencyBadge);
    }

    // Car info
    if (state.car.connected) {
      hide(btnConnectCar);
      hide(scanningLabel);
      show(btnDisconnectCar);
      if (state.car.name) { carNameEl.textContent = state.car.name; show(carNameEl); }
      else { hide(carNameEl); }
      if (state.car.battery != null) { batteryDisplay.textContent = "🔋 " + state.car.battery + "%"; show(batteryDisplay); }
      else { hide(batteryDisplay); }
    } else if (state.car.scanning) {
      hide(btnConnectCar);
      show(scanningLabel);
      hide(btnDisconnectCar);
      hide(carNameEl);
      hide(batteryDisplay);
    } else {
      show(btnConnectCar);
      hide(scanningLabel);
      hide(btnDisconnectCar);
      hide(carNameEl);
      hide(batteryDisplay);
    }

    // Error
    if (state.car.error) {
      errorBanner.textContent = state.car.error;
      show(errorBanner);
    } else {
      hide(errorBanner);
    }

    // Toggle buttons
    btnLights.dataset.active = control.lights ? "true" : "false";
    btnTurbo.dataset.active = control.turbo ? "true" : "false";
    btnDonut.dataset.active = control.donut ? "true" : "false";
  }

  // ---- WebSocket ----
  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  function connectBackend(url) {
    if (ws) disconnectBackend();

    // Normalise URL: http(s) → ws(s), append /ws path
    let wsUrl = url.trim().replace(/\/+$/, "");
    if (wsUrl.startsWith("http://")) wsUrl = "ws://" + wsUrl.slice(7);
    else if (wsUrl.startsWith("https://")) wsUrl = "wss://" + wsUrl.slice(8);
    else if (!wsUrl.startsWith("ws://") && !wsUrl.startsWith("wss://")) wsUrl = "ws://" + wsUrl;
    if (!wsUrl.endsWith("/ws")) wsUrl += "/ws";

    backendUrl = wsUrl;
    saveUrl(url.trim());

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log("[WS] Connected to backend");
      renderUI();
    };

    ws.onmessage = (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch { return; }
      handleMessage(msg);
    };

    ws.onclose = () => {
      console.log("[WS] Disconnected");
      resetState();
      stopCamera();
      renderUI();
      // Auto-reconnect if we still want to be connected
      if (backendUrl) {
        reconnectTimer = setTimeout(() => connectBackend(backendUrl), 3000);
      }
    };

    ws.onerror = () => {}; // onclose will fire
  }

  function disconnectBackend() {
    backendUrl = null;
    clearUrl();
    clearTimeout(reconnectTimer);
    if (ws) { ws.close(); ws = null; }
    stopCamera();
    resetState();
    renderUI();
  }

  function resetState() {
    state.piOnline = false;
    state.car = { connected: false, name: "", battery: null, error: "", scanning: false };
    state.cameraStatus = "disconnected";
    state.latency = null;
    control.forward = 0; control.reverse = 0;
    control.left = 0; control.right = 0;
    control.lights = 0; control.turbo = 0; control.donut = 0;
  }

  // ---- Message handling ----
  function handleMessage(msg) {
    switch (msg.type) {
      case "pi/status":
        state.piOnline = !!msg.connected;
        if (!state.piOnline) {
          resetCarState();
          stopCamera();
        } else {
          // Auto-start camera when Pi comes online
          if (state.cameraStatus === "disconnected") {
            startCamera();
          }
        }
        break;

      case "car/status":
        state.car.connected = !!msg.connected;
        state.car.name = msg.name || "";
        state.car.battery = msg.battery ?? null;
        state.car.error = msg.error || "";
        state.car.scanning = !!msg.scanning;
        break;

      case "camera/answer":
        if (pc && msg.sdp) {
          pc.setRemoteDescription({ type: "answer", sdp: msg.sdp })
            .catch((e) => console.error("[WebRTC] setRemoteDescription error:", e));
        }
        break;

      case "camera/candidate":
        if (pc && msg.candidate) {
          pc.addIceCandidate({ candidate: msg.candidate, sdpMid: msg.sdpMid || "0", sdpMLineIndex: msg.sdpMLineIndex || 0 })
            .catch((e) => console.error("[WebRTC] addIceCandidate error:", e));
        }
        break;

      case "error":
        state.car.error = msg.message || "Unknown error";
        break;
    }
    renderUI();
  }

  function resetCarState() {
    state.car = { connected: false, name: "", battery: null, error: "", scanning: false };
  }

  // ---- Camera (WebRTC via backend relay) ----
  function startCamera() {
    if (state.cameraStatus !== "disconnected") return;
    state.cameraStatus = "connecting";
    renderUI();

    pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    // Receive-only video
    pc.addTransceiver("video", { direction: "recvonly" });

    pc.ontrack = (evt) => {
      videoEl.srcObject = evt.streams[0] || new MediaStream([evt.track]);
    };

    pc.onicecandidate = (evt) => {
      if (evt.candidate) {
        send({
          type: "camera/candidate",
          candidate: evt.candidate.candidate,
          sdpMid: evt.candidate.sdpMid,
          sdpMLineIndex: evt.candidate.sdpMLineIndex,
        });
      }
    };

    pc.oniceconnectionstatechange = () => {
      const s = pc.iceConnectionState;
      console.log("[WebRTC] ICE state:", s);
      if (s === "connected" || s === "completed") {
        state.cameraStatus = "connected";
        startLatencyPolling();
        renderUI();
      } else if (s === "disconnected") {
        // Wait 2s then retry
        setTimeout(() => {
          if (pc && pc.iceConnectionState === "disconnected") {
            stopCamera();
            startCamera();
          }
        }, 2000);
      } else if (s === "failed" || s === "closed") {
        stopCamera();
        // Retry after delay
        setTimeout(() => {
          if (state.piOnline && state.cameraStatus === "disconnected") startCamera();
        }, 1500);
      }
    };

    pc.createOffer()
      .then((offer) => pc.setLocalDescription(offer))
      .then(() => {
        send({ type: "camera/offer", sdp: pc.localDescription.sdp });
      })
      .catch((e) => {
        console.error("[WebRTC] createOffer error:", e);
        stopCamera();
      });
  }

  function stopCamera() {
    stopLatencyPolling();
    if (pc) {
      pc.ontrack = null;
      pc.onicecandidate = null;
      pc.oniceconnectionstatechange = null;
      pc.close();
      pc = null;
    }
    videoEl.srcObject = null;
    state.cameraStatus = "disconnected";
    state.latency = null;
  }

  // ---- Latency polling ----
  function startLatencyPolling() {
    stopLatencyPolling();
    latencyInterval = setInterval(async () => {
      if (!pc) return;
      try {
        const stats = await pc.getStats();
        stats.forEach((report) => {
          if (report.type === "candidate-pair" && report.state === "succeeded" && report.currentRoundTripTime != null) {
            state.latency = Math.round((report.currentRoundTripTime / 2) * 1000);
            renderUI();
          }
        });
      } catch {}
    }, 2000);
  }

  function stopLatencyPolling() {
    if (latencyInterval) { clearInterval(latencyInterval); latencyInterval = null; }
  }

  // ---- Car control ----
  function updateControl(key, value) {
    const map = { w: "forward", s: "reverse", a: "left", d: "right" };
    const field = map[key] || key;
    control[field] = value ? 1 : 0;
    send({ type: "control", ...control });
  }

  function toggleControl(field) {
    control[field] = control[field] ? 0 : 1;
    send({ type: "control", ...control });
    renderUI();
  }

  // ---- Keyboard handling ----
  const pressedKeys = new Set();

  function onKeyDown(e) {
    if (e.repeat) return;
    const k = e.key.toLowerCase();
    pressedKeys.add(k);

    // Drive keys
    if ("wasd".includes(k) && k.length === 1) {
      updateControl(k, true);
      const el = $("key-" + k);
      if (el) el.classList.add("active");
    }

    // Toggle keys
    if (k === "l") toggleControl("lights");
    if (k === "t") toggleControl("turbo");
    if (k === "o") toggleControl("donut");
  }

  function onKeyUp(e) {
    const k = e.key.toLowerCase();
    pressedKeys.delete(k);

    if ("wasd".includes(k) && k.length === 1) {
      updateControl(k, false);
      const el = $("key-" + k);
      if (el) el.classList.remove("active");
    }
  }

  document.addEventListener("keydown", onKeyDown);
  document.addEventListener("keyup", onKeyUp);

  // ---- Button handlers ----
  btnConnect.addEventListener("click", () => {
    const url = urlInput.value.trim();
    if (!url) return;
    connectBackend(url);
  });

  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") btnConnect.click();
  });

  btnDisconnectBackend.addEventListener("click", () => {
    disconnectBackend();
  });

  btnConnectCar.addEventListener("click", () => {
    send({ type: "car/connect" });
  });

  btnDisconnectCar.addEventListener("click", () => {
    send({ type: "car/disconnect" });
  });

  btnLights.addEventListener("click", () => toggleControl("lights"));
  btnTurbo.addEventListener("click", () => toggleControl("turbo"));
  btnDonut.addEventListener("click", () => toggleControl("donut"));

  // ---- Touch controls for WASD on mobile ----
  document.querySelectorAll(".key").forEach((el) => {
    const key = el.dataset.key;
    el.addEventListener("touchstart", (e) => {
      e.preventDefault();
      updateControl(key, true);
      el.classList.add("active");
    }, { passive: false });
    el.addEventListener("touchend", (e) => {
      e.preventDefault();
      updateControl(key, false);
      el.classList.remove("active");
    }, { passive: false });
    // Also mouse for desktop clicking
    el.addEventListener("mousedown", (e) => {
      e.preventDefault();
      updateControl(key, true);
      el.classList.add("active");
    });
    el.addEventListener("mouseup", (e) => {
      e.preventDefault();
      updateControl(key, false);
      el.classList.remove("active");
    });
    el.addEventListener("mouseleave", () => {
      if (el.classList.contains("active")) {
        updateControl(key, false);
        el.classList.remove("active");
      }
    });
  });

  // ---- Auto-connect on load ----
  const savedUrl = loadUrl();
  if (savedUrl) {
    urlInput.value = savedUrl;
    connectBackend(savedUrl);
  }

  // Initial render
  renderUI();
})();
