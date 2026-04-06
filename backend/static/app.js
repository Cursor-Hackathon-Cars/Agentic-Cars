// ==========================================================================
// Agentic Cars — Frontend (REST API client)
// Controls the car and streams camera via the backend's REST endpoints.
// ==========================================================================

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ---- DOM refs ----
  const cameraFeed = $("camera-feed");
  const cameraFeedImg = $("camera-feed-img");
  const btnScan = $("btn-scan");
  const scanResults = $("scan-results");
  const carConnectedInfo = $("car-connected-info");
  const connectedCarName = $("connected-car-name");
  const btnDisconnectCar = $("btn-disconnect-car");
  const btnCameraConnect = $("btn-camera-connect");
  const btnCameraDisconnect = $("btn-camera-disconnect");
  const camStatusDot = $("cam-status");
  const carStatusDot = $("car-status-dot");
  const batteryDisplay = $("battery-display");
  const carNameEl = $("car-name");
  const errorBanner = $("error-banner");
  const driveHud = $("drive-hud");
  const btnLights = $("btn-lights");
  const btnTurbo = $("btn-turbo");
  const btnDonut = $("btn-donut");

  // ---- State ----
  let carConnected = false;
  let cameraConnected = false;
  let statusPollTimer = null;
  let snapshotTimer = null;

  const control = {
    forward: 0, reverse: 0, left: 0, right: 0,
    lights: 0, turbo: 0, donut: 0,
  };

  // ---- Helpers ----
  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  function stopCameraFeed() {
    if (snapshotTimer) {
      if (snapshotTimer.stop) snapshotTimer.stop();
      else clearInterval(snapshotTimer);
      snapshotTimer = null;
    }
    cameraFeed.pause();
    cameraFeed.removeAttribute("src");
    cameraFeed.load();
    cameraFeedImg.removeAttribute("src");
    hide(cameraFeed);
    hide(cameraFeedImg);
  }

  function showError(msg) {
    errorBanner.textContent = msg;
    show(errorBanner);
    setTimeout(() => hide(errorBanner), 5000);
  }

  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || data.detail || `HTTP ${resp.status}`);
    }
    return data;
  }

  // ---- Status polling ----
  function startStatusPolling() {
    stopStatusPolling();
    statusPollTimer = setInterval(pollStatus, 2000);
    pollStatus();
  }

  function stopStatusPolling() {
    if (statusPollTimer) { clearInterval(statusPollTimer); statusPollTimer = null; }
  }

  async function pollStatus() {
    try {
      const s = await api("GET", "/car/status");
      carConnected = s.connected;
      updateCarUI(s);
    } catch {
      // Server might be down
    }
  }

  function updateCarUI(status) {
    // Car status dot
    carStatusDot.className = "status-dot " + (status.connected ? "dot-on" : "dot-off");

    if (status.connected) {
      hide(btnScan);
      hide(scanResults);
      show(carConnectedInfo);
      connectedCarName.textContent = status.name || "Car";
      show(driveHud);

      if (status.battery != null) {
        batteryDisplay.textContent = "🔋 " + status.battery + "%";
        show(batteryDisplay);
      } else {
        hide(batteryDisplay);
      }

      if (status.name) {
        carNameEl.textContent = status.name;
        show(carNameEl);
      }
    } else {
      show(btnScan);
      hide(carConnectedInfo);
      hide(driveHud);
      hide(batteryDisplay);
      hide(carNameEl);
    }
  }

  // ---- Car: Scan ----
  btnScan.addEventListener("click", async () => {
    btnScan.textContent = "Scanning…";
    btnScan.disabled = true;
    hide(scanResults);
    try {
      const data = await api("POST", "/car/scan");
      if (data.cars.length === 0) {
        scanResults.innerHTML = '<p class="no-cars">No cars found. Make sure a car is turned on.</p>';
      } else {
        scanResults.innerHTML = data.cars.map((c) =>
          `<button class="btn btn-sm btn-green scan-car-btn" data-num="${c.number}">
            ${c.number}. ${c.name}
          </button>`
        ).join("");
        // Bind connect buttons
        scanResults.querySelectorAll(".scan-car-btn").forEach((btn) => {
          btn.addEventListener("click", () => connectCar(parseInt(btn.dataset.num)));
        });
      }
      show(scanResults);
    } catch (e) {
      showError(e.message);
    } finally {
      btnScan.textContent = "Scan for Cars";
      btnScan.disabled = false;
    }
  });

  // ---- Car: Connect ----
  async function connectCar(num) {
    try {
      const data = await api("POST", `/car/connect/${num}`);
      carConnected = true;
      hide(scanResults);
      pollStatus();
    } catch (e) {
      showError(e.message);
    }
  }

  // ---- Car: Disconnect ----
  btnDisconnectCar.addEventListener("click", async () => {
    try {
      await api("POST", "/car/disconnect");
      carConnected = false;
      control.forward = 0; control.reverse = 0;
      control.left = 0; control.right = 0;
      control.lights = 0; control.turbo = 0; control.donut = 0;
      pollStatus();
      renderToggles();
    } catch (e) {
      showError(e.message);
    }
  });

  // ---- Car: Control ----
  async function sendControl() {
    if (!carConnected) return;
    try {
      await api("POST", "/car/control", control);
    } catch {
      // Silently ignore — status poll will catch disconnect
    }
  }

  function updateControl(key, value) {
    const map = { w: "forward", s: "reverse", a: "left", d: "right" };
    const field = map[key] || key;
    control[field] = value ? 1 : 0;
    sendControl();
  }

  function toggleControl(field) {
    control[field] = control[field] ? 0 : 1;
    sendControl();
    renderToggles();
  }

  function renderToggles() {
    btnLights.dataset.active = control.lights ? "true" : "false";
    btnTurbo.dataset.active = control.turbo ? "true" : "false";
    btnDonut.dataset.active = control.donut ? "true" : "false";
  }

  // ---- Camera: Connect ----
  btnCameraConnect.addEventListener("click", async () => {
    btnCameraConnect.textContent = "Connecting…";
    btnCameraConnect.disabled = true;
    try {
      const camInfo = await api("POST", "/camera/connect");
      stopCameraFeed();

      const tryVideoStream = async () => {
        return await new Promise((resolve) => {
          let done = false;
          const finish = (ok) => {
            if (done) return;
            done = true;
            cameraFeed.onplaying = null;
            cameraFeed.onerror = null;
            resolve(ok);
          };

          cameraFeed.src = "/camera/stream?" + Date.now();
          cameraFeed.play().catch(() => {});

          cameraFeed.onplaying = () => finish(true);
          cameraFeed.onerror = () => finish(false);

          setTimeout(() => finish(cameraFeed.readyState >= 2), 2500);
        });
      };

      const startSnapshotFallback = () => {
        show(cameraFeedImg);
        hide(cameraFeed);

        let running = true;
        let failures = 0;
        const next = () => {
          if (!running) return;
          const img = new Image();
          img.onload = () => {
            if (!running) return;
            failures = 0;
            cameraFeedImg.src = img.src;
            camStatusDot.className = "status-dot dot-on";
            hide(errorBanner);
            requestAnimationFrame(next);
          };
          img.onerror = () => {
            if (!running) return;
            failures++;
            if (failures >= 3) {
              camStatusDot.className = "status-dot dot-warn";
              errorBanner.textContent =
                "Camera stream lost — retrying… (" + failures + " failures)";
              show(errorBanner);
            }
            setTimeout(next, 1000);
          };
          img.src = "/camera/snapshot?t=" + Date.now();
        };
        next();

        snapshotTimer = { stop() { running = false; hide(errorBanner); } };
      };

      const isZapbox = /Zapbox/i.test(navigator.userAgent);
      const isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
      const forceSnapshot = camInfo.source === "laptop_camera" || isZapbox || isMobile;

      if (forceSnapshot) {
        startSnapshotFallback();
      } else {
        show(cameraFeed);
        hide(cameraFeedImg);
        const ok = await tryVideoStream();
        if (!ok) {
          startSnapshotFallback();
        }
      }

      cameraConnected = true;
      camStatusDot.className = "status-dot dot-on";
      hide(btnCameraConnect);
      show(btnCameraDisconnect);
    } catch (e) {
      showError(e.message);
      btnCameraConnect.textContent = "Connect Camera";
      btnCameraConnect.disabled = false;
    }
  });

  // ---- Camera: Disconnect ----
  btnCameraDisconnect.addEventListener("click", () => {
    stopCameraFeed();
    cameraConnected = false;
    camStatusDot.className = "status-dot dot-off";
    show(btnCameraConnect);
    btnCameraConnect.textContent = "Connect Camera";
    btnCameraConnect.disabled = false;
    hide(btnCameraDisconnect);
  });

  // ---- Keyboard handling ----
  document.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    const k = e.key.toLowerCase();
    if ("wasd".includes(k) && k.length === 1) {
      updateControl(k, true);
      const el = $("key-" + k);
      if (el) el.classList.add("active");
    }
    if (k === "l") toggleControl("lights");
    if (k === "t") toggleControl("turbo");
    if (k === "o") toggleControl("donut");
  });

  document.addEventListener("keyup", (e) => {
    const k = e.key.toLowerCase();
    if ("wasd".includes(k) && k.length === 1) {
      updateControl(k, false);
      const el = $("key-" + k);
      if (el) el.classList.remove("active");
    }
  });

  // ---- Toggle button clicks ----
  btnLights.addEventListener("click", () => toggleControl("lights"));
  btnTurbo.addEventListener("click", () => toggleControl("turbo"));
  btnDonut.addEventListener("click", () => toggleControl("donut"));

  // ---- Touch/mouse controls for WASD ----
  document.querySelectorAll(".key").forEach((el) => {
    const key = el.dataset.key;
    el.addEventListener("touchstart", (e) => { e.preventDefault(); updateControl(key, true); el.classList.add("active"); }, { passive: false });
    el.addEventListener("touchend", (e) => { e.preventDefault(); updateControl(key, false); el.classList.remove("active"); }, { passive: false });
    el.addEventListener("mousedown", (e) => { e.preventDefault(); updateControl(key, true); el.classList.add("active"); });
    el.addEventListener("mouseup", (e) => { e.preventDefault(); updateControl(key, false); el.classList.remove("active"); });
    el.addEventListener("mouseleave", () => { if (el.classList.contains("active")) { updateControl(key, false); el.classList.remove("active"); } });
  });

  // ---- Init ----
  startStatusPolling();
})();
