/* DocScan web scanner: camera capture -> upload -> OCR pipeline -> results.
 * Plain JS, no build step. Talks only to the JWT-protected REST API under /api/.
 * All server data is rendered with textContent (never innerHTML) to avoid XSS.
 */
(() => {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const el = (tag, props = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let toastTimer;
  function toast(message, isError = false) {
    const t = $("toast");
    t.textContent = message;
    t.classList.toggle("error", isError);
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 3000);
  }

  const ICONS = { "file-text": "📄", "file-invoice": "🧾", receipt: "🧾", "id-card": "🪪", table: "📊", "file-signature": "✍️" };
  const STEP_LABELS = {
    queued: "Waiting in the queue…",
    prepare_pages: "Preparing pages…",
    ocr_pages: "Reading the text…",
    build_pdf: "Building the searchable PDF…",
    extract_fields: "Extracting fields…",
    analyze: "Analysing the text…",
    done: "Done",
  };
  // Width / height of the framing guide for each document type.
  const GUIDE_ASPECT = { id_document: 85.6 / 54, receipt: 0.45 };
  const DEFAULT_ASPECT = 1 / Math.SQRT2; // A4 portrait
  const MAX_ORIGINAL_SIDE = 4000; // kept in the browser so corners can be re-adjusted later
  const LIVE_SIDE = 480; // frame size used for live edge detection
  const AUTO_CAPTURE_MS = 1200; // page must be held still this long for auto capture

  // ------------------------------------------------------------------ state
  const state = {
    access: localStorage.getItem("docscan.access"),
    refresh: localStorage.getItem("docscan.refresh"),
    user: null,
    types: [],
    // current scan session
    type: null,
    pages: [], // {key, blob, url, rotation, heic}
    target: null, // existing document {id, status, page_count} when adding pages
    nextKey: 1,
    // camera
    stream: null,
    facing: "environment",
    // processing
    docId: null,
    phase: null, // "upload" | "process"
    pollTimer: null,
    // history list
    listPage: 1,
    query: "",
    // result
    result: null,
  };

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data;
    }
  }

  function messageFrom(data) {
    if (!data) return "";
    if (typeof data === "string") return data;
    if (data.detail) return typeof data.detail === "string" ? data.detail : messageFrom(data.detail);
    const parts = [];
    for (const [key, value] of Object.entries(data)) {
      const text = Array.isArray(value) ? value.map(messageFrom).join(" ") : messageFrom(value);
      parts.push(key === "non_field_errors" ? text : `${key}: ${text}`);
    }
    return parts.join(" ");
  }

  let refreshing = null;
  async function refreshAccess() {
    if (!state.refresh) return false;
    refreshing = refreshing || fetch("/api/auth/token/refresh/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh: state.refresh }),
    }).then(async (res) => {
      if (!res.ok) return false;
      const data = await res.json();
      setTokens(data.access, data.refresh || state.refresh);
      return true;
    }).catch(() => false).finally(() => { refreshing = null; });
    return refreshing;
  }

  async function api(path, { method = "GET", json, form, raw = false, retried = false } = {}) {
    const headers = {};
    if (state.access) headers.Authorization = `Bearer ${state.access}`;
    let body;
    if (form) body = form;
    else if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    }
    let res;
    try {
      res = await fetch(`/api${path}`, { method, headers, body });
    } catch (e) {
      throw new ApiError("Network error – is the server running?", 0);
    }
    if (res.status === 401 && !retried && !path.startsWith("/auth/token")) {
      if (await refreshAccess()) return api(path, { method, json, form, raw, retried: true });
      logout("Your session expired. Please log in again.");
      throw new ApiError("Session expired", 401);
    }
    if (raw) {
      if (!res.ok) throw new ApiError(messageFrom(await res.json().catch(() => null)) || res.statusText, res.status);
      return res;
    }
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) {
      const fallback = res.status === 429 ? "Too many requests – wait a moment." : `Request failed (${res.status})`;
      throw new ApiError(messageFrom(data) || fallback, res.status, data);
    }
    return data;
  }

  function setTokens(access, refresh) {
    state.access = access;
    state.refresh = refresh;
    localStorage.setItem("docscan.access", access);
    localStorage.setItem("docscan.refresh", refresh);
  }

  // ------------------------------------------------------------------ views
  const VIEWS = ["auth", "home", "scan", "crop", "review", "progress", "result"];
  let currentView = null;

  function show(name) {
    if (currentView === "scan" && name !== "scan") stopCamera();
    if (name !== "progress") stopPolling();
    for (const v of VIEWS) $(`view-${v}`).classList.toggle("hidden", v !== name);
    currentView = name;
    window.scrollTo(0, 0);
  }

  document.addEventListener("click", (e) => {
    const target = e.target.closest("[data-action=home]");
    if (target && state.user) goHome();
  });

  // ------------------------------------------------------------------ auth
  let registerMode = false;

  function setAuthMode(register) {
    registerMode = register;
    $("auth-title").textContent = register ? "Create account" : "Log in";
    $("auth-submit").textContent = register ? "Create account" : "Log in";
    $("auth-switch-text").textContent = register ? "Already registered?" : "No account?";
    $("auth-switch").textContent = register ? "Log in" : "Create one";
    document.querySelectorAll(".register-only").forEach((n) => n.classList.toggle("hidden", !register));
    $("auth-form").email.required = register;
    $("auth-form").password.autocomplete = register ? "new-password" : "current-password";
    $("auth-error").classList.add("hidden");
  }

  $("auth-switch").addEventListener("click", () => setAuthMode(!registerMode));

  $("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const btn = $("auth-submit");
    btn.disabled = true;
    $("auth-error").classList.add("hidden");
    try {
      const creds = { username: form.username.value.trim(), password: form.password.value };
      if (registerMode) {
        const data = await api("/auth/register/", { method: "POST", json: { ...creds, email: form.email.value.trim() } });
        setTokens(data.access, data.refresh);
      } else {
        const data = await api("/auth/token/", { method: "POST", json: creds });
        setTokens(data.access, data.refresh);
      }
      form.password.value = "";
      await afterLogin();
    } catch (err) {
      $("auth-error").textContent = err.status === 401 ? "Wrong username or password." : err.message;
      $("auth-error").classList.remove("hidden");
    } finally {
      btn.disabled = false;
    }
  });

  async function afterLogin() {
    state.user = await api("/auth/me/");
    $("whoami").textContent = state.user.username;
    $("logout").classList.remove("hidden");
    await goHome();
  }

  function logout(message) {
    stopCamera();
    stopPolling();
    state.access = state.refresh = state.user = null;
    localStorage.removeItem("docscan.access");
    localStorage.removeItem("docscan.refresh");
    $("whoami").textContent = "";
    $("logout").classList.add("hidden");
    setAuthMode(false);
    show("auth");
    if (message) toast(message, true);
  }
  $("logout").addEventListener("click", () => logout());

  // ------------------------------------------------------------------ home
  async function goHome() {
    resetSession();
    show("home");
    try {
      if (!state.types.length) state.types = await api("/document-types/");
      renderTypes();
    } catch (err) {
      toast(err.message, true);
    }
    loadDocs(true);
  }

  function renderTypes() {
    const grid = $("type-grid");
    grid.replaceChildren(...state.types.map((t) =>
      el("button", { class: "type-card", type: "button", onclick: () => startScan(t) }, [
        el("span", { class: "icon", text: ICONS[t.icon] || "📄", "aria-hidden": "true" }),
        el("span", { class: "name", text: t.name }),
        el("span", { class: "desc", text: t.description }),
      ])
    ));
  }

  async function loadDocs(reset) {
    if (reset) state.listPage = 1;
    const params = new URLSearchParams({ page: state.listPage });
    if (state.query) params.set("q", state.query);
    try {
      const data = await api(`/documents/?${params}`);
      const list = $("doc-list");
      const items = data.results.map(docItem);
      if (reset) list.replaceChildren(...items);
      else list.append(...items);
      if (reset && !items.length) {
        list.append(el("p", { class: "muted", text: state.query ? "No documents match your search." : "No documents yet – pick a type above to scan one." }));
      }
      $("more-docs").classList.toggle("hidden", !data.next);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function docItem(d) {
    const thumb = d.thumbnail_url
      ? el("img", { src: d.thumbnail_url, alt: "", loading: "lazy", onerror: (e) => e.target.replaceWith(el("div", { class: "thumb-ph" })) })
      : el("div", { class: "thumb-ph" });
    const date = new Date(d.created_at).toLocaleString();
    return el("button", { class: "doc-item", type: "button", onclick: () => openDocument(d.id) }, [
      thumb,
      el("div", { class: "info" }, [
        el("div", { class: "title", text: d.title }),
        el("div", { class: "muted", text: `${d.document_type_name} · ${d.page_count} page${d.page_count === 1 ? "" : "s"} · ${date}` }),
      ]),
      el("span", { class: `badge ${d.status}`, text: d.status }),
    ]);
  }

  $("more-docs").addEventListener("click", () => { state.listPage += 1; loadDocs(false); });
  let searchTimer;
  $("search-input").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = e.target.value.trim(); loadDocs(true); }, 350);
  });
  $("search-form").addEventListener("submit", (e) => e.preventDefault());

  // ------------------------------------------------------------------ scan session
  function resetSession() {
    for (const p of state.pages) URL.revokeObjectURL(p.url);
    state.pages = [];
    state.type = null;
    state.target = null;
    state.docId = null;
    state.phase = null;
    $("doc-title").value = "";
  }

  function maxNewPages() {
    const existing = state.target ? state.target.page_count : 0;
    return Math.max(0, state.type.max_pages - existing);
  }

  function startScan(type, target = null) {
    resetSession();
    state.type = type;
    state.target = target;
    $("scan-type-name").textContent = target ? `Add pages · ${type.name}` : type.name;
    renderTray();
    show("scan");
    startCamera();
  }

  function renderTray() {
    const n = state.pages.length;
    const existing = state.target ? state.target.page_count : 0;
    $("page-counter").textContent = `${n} / ${maxNewPages()} page${maxNewPages() === 1 ? "" : "s"}`;
    const canFinish = n > 0 || (existing > 0 && ["draft", "uploading"].includes(state.target?.status));
    $("done-scanning").disabled = !canFinish;
    $("done-scanning").textContent = n ? `Done (${n})` : "Done";
    $("tray").replaceChildren(...state.pages.map((p, i) =>
      el("div", { class: "thumb", title: p.original ? "Adjust corners" : "", onclick: () => openCrop(p, "scan") }, [
        thumbImage(p),
        el("span", { class: "num", text: String(existing + i + 1) }),
      ])
    ));
    const tray = $("tray");
    tray.scrollLeft = tray.scrollWidth;
  }

  function thumbImage(page) {
    const img = el("img", { src: page.url, alt: "Scanned page" });
    img.style.transform = `rotate(${page.rotation}deg)`;
    img.addEventListener("error", () => {
      // HEIC can't be previewed in most browsers; the server still converts it.
      img.replaceWith(el("div", { class: "thumb-ph", style: "display:flex;align-items:center;justify-content:center;height:96px;width:72px;font-size:.7rem", text: "HEIC" }));
    });
    return img;
  }

  function hasRoom() {
    if (state.pages.length < maxNewPages()) return true;
    toast(`${state.type.name} documents can have at most ${state.type.max_pages} pages.`, true);
    return false;
  }

  /**
   * A page keeps its full-size `original` plus the detected `quad` and `filter`,
   * so the corners can be adjusted later; `blob` is the processed image that gets uploaded.
   */
  function newPage({ original = null, quad = null, filter = "enhanced", blob = null, name }) {
    return { key: state.nextKey++, original, quad, filter, blob, url: blob ? URL.createObjectURL(blob) : null, rotation: 0, name };
  }

  function pushPage(page) {
    state.pages.push(page);
    renderTray();
  }

  // ------------------------------------------------------------------ camera
  const video = $("video");

  function cameraMessage(text, withRetry = false) {
    const box = $("camera-message");
    box.replaceChildren(el("p", { text }));
    if (withRetry) box.append(el("button", { class: "secondary small", type: "button", text: "Try again", onclick: startCamera }));
    box.append(el("p", { class: "muted", text: "You can still add photos with “Gallery” (on a phone it can open the camera app)." }));
    box.classList.remove("hidden");
  }

  async function startCamera() {
    stopCamera();
    $("camera-message").classList.add("hidden");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      cameraMessage(window.isSecureContext
        ? "This browser cannot access the camera."
        : "The live camera needs HTTPS (or http://localhost).");
      return;
    }
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: state.facing }, width: { ideal: 3840 }, height: { ideal: 2160 } },
      });
      if (currentView !== "scan") { stopCamera(); return; }
      video.srcObject = state.stream;
      await video.play().catch(() => {});
      const devices = await navigator.mediaDevices.enumerateDevices();
      $("switch-camera").classList.toggle("hidden", devices.filter((d) => d.kind === "videoinput").length < 2);
      layoutGuide();
      ensureOpenCV().then(() => {
        if (state.stream) startDetectLoop();
      });
    } catch (err) {
      const messages = {
        NotAllowedError: "Camera permission was denied. Allow camera access in the browser's site settings.",
        NotFoundError: "No camera was found on this device.",
        NotReadableError: "The camera is being used by another application.",
        OverconstrainedError: "The camera does not support the requested settings.",
      };
      cameraMessage(messages[err.name] || `Could not start the camera: ${err.message}`, true);
    }
  }

  function stopCamera() {
    stopDetectLoop();
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    state.stream = null;
    video.srcObject = null;
  }

  $("switch-camera").addEventListener("click", () => {
    state.facing = state.facing === "environment" ? "user" : "environment";
    startCamera();
  });

  document.addEventListener("visibilitychange", () => {
    if (currentView !== "scan") return;
    if (document.hidden) stopCamera();
    else startCamera();
  });

  // ------------------------------------------------------------------ edge detection (OpenCV.js)
  let cv = null; // the OpenCV module, once loaded
  let cvFailed = false;

  function ensureOpenCV() {
    if (cv || cvFailed || !window.DocDetect) {
      if (!window.DocDetect) cvFailed = true;
      updateDetectUi();
      return Promise.resolve(cv);
    }
    setDetectStatus("Loading edge detection…");
    return window.DocDetect.load()
      .then(({ cv: mod }) => {
        cv = mod;
        updateDetectUi();
        return cv;
      })
      .catch((err) => {
        console.warn("OpenCV.js unavailable:", err);
        cvFailed = true;
        updateDetectUi();
        toast("Edge detection could not be loaded – the page will be cropped to the frame instead.", true);
        return null;
      });
  }

  const detectEnabled = () => Boolean(cv) && $("opt-detect").checked;
  const currentFilter = () => $("opt-filter").value;

  function setDetectStatus(text, found = false) {
    const status = $("detect-status");
    status.textContent = text;
    status.classList.toggle("found", found);
    status.classList.toggle("hidden-soft", !text);
  }

  function updateDetectUi() {
    const detect = $("opt-detect");
    detect.disabled = cvFailed;
    if (cvFailed) detect.checked = false;
    $("opt-auto").disabled = !detectEnabled();
    if (!detectEnabled()) {
      clearOverlay();
      setDetectStatus(cvFailed ? "Edge detection unavailable – fit the page in the frame" : (cv ? "" : "Loading edge detection…"));
    } else {
      setDetectStatus("Looking for a document…");
    }
    layoutGuide();
  }
  $("opt-detect").addEventListener("change", updateDetectUi);

  /** Rectangle of the framing guide (used when edge detection is off), in camera-box pixels. */
  function guideRect() {
    const box = $("camera-box").getBoundingClientRect();
    const aspect = GUIDE_ASPECT[state.type?.code] || DEFAULT_ASPECT;
    let w = box.width * 0.9;
    let h = w / aspect;
    if (h > box.height * 0.9) {
      h = box.height * 0.9;
      w = h * aspect;
    }
    return { x: (box.width - w) / 2, y: (box.height - h) / 2, w, h, boxW: box.width, boxH: box.height };
  }

  function layoutGuide() {
    const guide = $("guide");
    guide.classList.toggle("off", detectEnabled() || !state.type);
    if (!state.type) return;
    const r = guideRect();
    Object.assign(guide.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
  }
  window.addEventListener("resize", () => {
    layoutGuide();
    if (currentView === "crop") drawCrop();
  });
  video.addEventListener("loadedmetadata", layoutGuide);

  /** How the <video> (object-fit: cover) maps onto the camera box. */
  function videoToBox() {
    const box = $("camera-box").getBoundingClientRect();
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    const scale = Math.max(box.width / vw, box.height / vh);
    return { scale, ox: (box.width - vw * scale) / 2, oy: (box.height - vh * scale) / 2, box };
  }

  /** The framing guide as a quad in video pixels. */
  function guideQuad() {
    const r = guideRect();
    const { scale, ox, oy } = videoToBox();
    const x0 = Math.max(0, (r.x - ox) / scale);
    const y0 = Math.max(0, (r.y - oy) / scale);
    const x1 = Math.min(video.videoWidth - 1, (r.x + r.w - ox) / scale);
    const y1 = Math.min(video.videoHeight - 1, (r.y + r.h - oy) / scale);
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  }

  // ---- live detection loop: outline the page on top of the video
  const live = { timer: null, busy: false, quad: null, prev: null, stableSince: 0, lastCapture: 0 };
  const liveCanvas = document.createElement("canvas");

  function startDetectLoop() {
    stopDetectLoop();
    updateDetectUi();
    live.timer = setInterval(detectFrame, 150);
  }

  function stopDetectLoop() {
    clearInterval(live.timer);
    live.timer = null;
    live.quad = live.prev = null;
    live.stableSince = 0;
    clearOverlay();
  }

  function detectFrame() {
    if (live.busy || currentView !== "scan" || !video.videoWidth || !detectEnabled() || state.capturing) return;
    live.busy = true;
    let src = null;
    try {
      const s = Math.min(1, LIVE_SIDE / Math.max(video.videoWidth, video.videoHeight));
      liveCanvas.width = Math.round(video.videoWidth * s);
      liveCanvas.height = Math.round(video.videoHeight * s);
      liveCanvas.getContext("2d", { willReadFrequently: true }).drawImage(video, 0, 0, liveCanvas.width, liveCanvas.height);
      src = cv.imread(liveCanvas);
      const quad = window.DocDetect.detectQuad(cv, src, { maxSide: LIVE_SIDE });
      live.quad = quad ? quad.map(([x, y]) => [x / s, y / s]) : null; // video pixels
      trackStability();
      drawOverlay(live.quad);
    } catch (err) {
      console.warn("Live detection failed:", err);
    } finally {
      if (src) src.delete();
      live.busy = false;
    }
  }

  function trackStability() {
    const now = performance.now();
    const auto = $("opt-auto").checked;
    if (!live.quad) {
      live.prev = null;
      live.stableSince = 0;
      setDetectStatus("Looking for a document…");
      return;
    }
    const tolerance = Math.max(video.videoWidth, video.videoHeight) * 0.02;
    const still = live.prev && live.quad.every((p, i) => Math.hypot(p[0] - live.prev[i][0], p[1] - live.prev[i][1]) < tolerance);
    live.prev = live.quad;
    if (!still) live.stableSince = now;
    if (!auto) {
      setDetectStatus("Document found – tap the button", true);
      return;
    }
    const held = now - live.stableSince;
    if (held >= AUTO_CAPTURE_MS && now - live.lastCapture > 2500) {
      live.lastCapture = now;
      live.stableSince = now;
      capture();
    } else {
      setDetectStatus(held > 300 ? "Hold still…" : "Document found", true);
    }
  }

  function clearOverlay() {
    const canvas = $("overlay");
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  }

  function drawOverlay(quad) {
    const canvas = $("overlay");
    const { scale, ox, oy, box } = videoToBox();
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(box.width * dpr) || canvas.height !== Math.round(box.height * dpr)) {
      canvas.width = Math.round(box.width * dpr);
      canvas.height = Math.round(box.height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, box.width, box.height);
    if (!quad) return;
    const pts = quad.map(([x, y]) => [ox + x * scale, oy + y * scale]);
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fillStyle = "rgba(37, 99, 235, 0.18)";
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = "#22c55e";
    ctx.stroke();
    ctx.fillStyle = "#22c55e";
    for (const [x, y] of pts) {
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // ---- image helpers
  function canvasToBlob(canvas, quality = 0.92) {
    return new Promise((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not encode the image."))), "image/jpeg", quality));
  }

  /** Decode a Blob (EXIF orientation applied) into a canvas no larger than `maxSide`. */
  async function blobToCanvas(blob, maxSide = MAX_ORIGINAL_SIDE) {
    const bitmap = await createImageBitmap(blob);
    const s = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * s);
    canvas.height = Math.round(bitmap.height * s);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close();
    return canvas;
  }

  const fullQuad = (w, h) => [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]];

  function detectOnCanvas(canvas) {
    if (!cv) return null;
    const src = cv.imread(canvas);
    try {
      return window.DocDetect.detectQuad(cv, src, { maxSide: 900 });
    } finally {
      src.delete();
    }
  }

  /** Document-style enhancement: stretch levels so paper becomes white and ink dark. */
  function enhance(ctx, w, h) {
    const image = ctx.getImageData(0, 0, w, h);
    const d = image.data;
    const hist = new Uint32Array(256);
    let samples = 0;
    for (let i = 0; i < d.length; i += 16) {
      hist[(d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8]++;
      samples++;
    }
    const percentile = (p) => {
      let acc = 0;
      for (let v = 0; v < 256; v++) {
        acc += hist[v];
        if (acc >= samples * p) return v;
      }
      return 255;
    };
    const lo = percentile(0.02);
    const hi = percentile(0.92); // after cropping, most of the image is paper
    if (hi - lo < 40) return; // flat image, nothing sensible to stretch
    const lut = new Uint8ClampedArray(256);
    for (let v = 0; v < 256; v++) {
      const t = Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
      lut[v] = Math.round(255 * Math.pow(t, 1.15)); // slight gamma keeps text dark
    }
    for (let i = 0; i < d.length; i += 4) {
      d[i] = lut[d[i]];
      d[i + 1] = lut[d[i + 1]];
      d[i + 2] = lut[d[i + 2]];
    }
    ctx.putImageData(image, 0, 0);
  }

  function grayscale(ctx, w, h) {
    const image = ctx.getImageData(0, 0, w, h);
    const d = image.data;
    for (let i = 0; i < d.length; i += 4) {
      const y = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
      d[i] = d[i + 1] = d[i + 2] = y;
    }
    ctx.putImageData(image, 0, 0);
  }

  /**
   * Build the uploadable image: flatten the page (perspective correction) and apply the filter.
   * `source` is a canvas holding page.original, when the caller already has one.
   */
  async function renderPage(page, source = null) {
    const src = source || (await blobToCanvas(page.original));
    const out = $("work-canvas");
    const quad = page.quad || fullQuad(src.width, src.height);
    if (cv) {
      const mat = cv.imread(src);
      let flat = null;
      let bw = null;
      try {
        flat = window.DocDetect.warp(cv, mat, quad); // also caps the size at 3000 px
        if (page.filter === "bw") {
          bw = window.DocDetect.blackWhite(cv, flat);
          cv.imshow(out, bw);
        } else {
          cv.imshow(out, flat);
        }
      } finally {
        mat.delete();
        if (flat) flat.delete();
        if (bw) bw.delete();
      }
    } else {
      // No OpenCV: crop to the quad's bounding box (the frame guide is a rectangle anyway).
      const xs = quad.map((p) => p[0]);
      const ys = quad.map((p) => p[1]);
      const x0 = Math.max(0, Math.min(...xs));
      const y0 = Math.max(0, Math.min(...ys));
      const w = Math.min(src.width, Math.max(...xs)) - x0;
      const h = Math.min(src.height, Math.max(...ys)) - y0;
      const s = Math.min(1, 3000 / Math.max(w, h));
      out.width = Math.round(w * s);
      out.height = Math.round(h * s);
      out.getContext("2d").drawImage(src, x0, y0, w, h, 0, 0, out.width, out.height);
    }
    const ctx = out.getContext("2d", { willReadFrequently: true });
    if (page.filter === "enhanced") enhance(ctx, out.width, out.height);
    if (page.filter === "bw" && !cv) {
      grayscale(ctx, out.width, out.height);
      enhance(ctx, out.width, out.height);
    }
    const blob = await canvasToBlob(out, 0.9);
    if (page.url) URL.revokeObjectURL(page.url);
    page.blob = blob;
    page.url = URL.createObjectURL(blob);
  }

  // ---- capture from the camera
  async function capture() {
    if (state.capturing) return;
    if (!state.stream || !video.videoWidth) {
      toast("The camera is not ready yet.", true);
      return;
    }
    if (!hasRoom()) return;
    state.capturing = true;
    try {
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      const s = Math.min(1, MAX_ORIGINAL_SIDE / Math.max(vw, vh));
      const frame = document.createElement("canvas");
      frame.width = Math.round(vw * s);
      frame.height = Math.round(vh * s);
      frame.getContext("2d").drawImage(video, 0, 0, frame.width, frame.height);

      const flash = $("flash");
      flash.classList.add("on");
      requestAnimationFrame(() => requestAnimationFrame(() => flash.classList.remove("on")));
      if (navigator.vibrate) navigator.vibrate(30);

      let quad;
      let note = "";
      if (detectEnabled()) {
        // Detect again on the sharp full-size frame; fall back to the last live outline.
        quad = detectOnCanvas(frame) || (live.quad && live.quad.map(([x, y]) => [x * s, y * s]));
        if (!quad) note = "No page edges found – tap the page below to set the corners.";
      } else {
        quad = guideQuad().map(([x, y]) => [x * s, y * s]);
      }
      const page = newPage({ original: await canvasToBlob(frame, 0.92), quad, filter: currentFilter(), name: `page-${Date.now()}.jpg` });
      await renderPage(page, frame);
      pushPage(page);
      if (note) toast(note);
    } catch (err) {
      toast(`Could not capture the page: ${err.message}`, true);
    } finally {
      state.capturing = false;
    }
  }

  $("shutter").addEventListener("click", capture);
  document.addEventListener("keydown", (e) => {
    if (currentView === "scan" && e.code === "Space" && !["INPUT", "TEXTAREA", "BUTTON", "SELECT"].includes(e.target.tagName)) {
      e.preventDefault();
      capture();
    }
  });

  // ---- photos from the gallery get the same edge detection
  async function importFile(file) {
    if (!hasRoom()) return false;
    let canvas;
    try {
      canvas = await blobToCanvas(file);
    } catch {
      // e.g. HEIC outside Safari: upload unchanged; the server converts it.
      pushPage(newPage({ blob: file, name: file.name }));
      return true;
    }
    const quad = detectEnabled() ? detectOnCanvas(canvas) : null;
    const page = newPage({ original: await canvasToBlob(canvas, 0.92), quad, filter: currentFilter(), name: `${file.name.replace(/\.[^.]+$/, "")}.jpg` });
    await renderPage(page, canvas);
    pushPage(page);
    return true;
  }

  $("file-input").addEventListener("change", async (e) => {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    await ensureOpenCV();
    toast(`Processing ${files.length} photo${files.length > 1 ? "s" : ""}…`);
    for (const file of files) {
      try {
        if (!(await importFile(file))) break;
      } catch (err) {
        toast(`${file.name}: ${err.message}`, true);
      }
    }
  });

  // ------------------------------------------------------------------ crop editor (drag the corners)
  const crop = { page: null, image: null, quad: null, drag: -1, returnTo: "scan", view: null, pointer: null };

  async function openCrop(page, returnTo) {
    if (!page.original) {
      toast("This photo can't be edited in the browser (e.g. HEIC); it is uploaded as it is.");
      return;
    }
    try {
      crop.image = await blobToCanvas(page.original);
    } catch (err) {
      toast(err.message, true);
      return;
    }
    crop.page = page;
    crop.returnTo = returnTo;
    crop.quad = (page.quad || fullQuad(crop.image.width, crop.image.height)).map((p) => [...p]);
    $("crop-filter").value = page.filter;
    $("crop-detect").disabled = !cv;
    show("crop");
    requestAnimationFrame(drawCrop);
  }

  function drawCrop() {
    if (!crop.image) return;
    const canvas = $("crop-canvas");
    const stage = $("crop-stage").getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(stage.width * dpr);
    canvas.height = Math.round(stage.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const pad = 28;
    const img = crop.image;
    const s = Math.min((stage.width - 2 * pad) / img.width, (stage.height - 2 * pad) / img.height);
    const ox = (stage.width - img.width * s) / 2;
    const oy = (stage.height - img.height * s) / 2;
    crop.view = { s, ox, oy };
    ctx.clearRect(0, 0, stage.width, stage.height);
    ctx.drawImage(img, ox, oy, img.width * s, img.height * s);

    const pts = crop.quad.map(([x, y]) => [ox + x * s, oy + y * s]);
    // Darken everything outside the page.
    ctx.beginPath();
    ctx.rect(0, 0, stage.width, stage.height);
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fillStyle = "rgba(0, 0, 0, 0.5)";
    ctx.fill("evenodd");
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#22c55e";
    ctx.stroke();
    pts.forEach(([x, y], i) => {
      ctx.beginPath();
      ctx.arc(x, y, crop.drag === i ? 16 : 13, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = "#2563eb";
      ctx.stroke();
    });

    // Magnifier while dragging, in the corner away from the finger.
    if (crop.drag >= 0) {
      const [hx, hy] = crop.quad[crop.drag];
      const r = 56;
      const zoom = 2.5 * s;
      const cx = pts[crop.drag][0] < stage.width / 2 ? stage.width - r - 10 : r + 10;
      const cy = r + 10;
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.clip();
      ctx.fillStyle = "#000";
      ctx.fillRect(cx - r, cy - r, 2 * r, 2 * r);
      ctx.drawImage(img, cx - hx * zoom, cy - hy * zoom, img.width * zoom, img.height * zoom);
      ctx.strokeStyle = "#22c55e";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(cx - 12, cy); ctx.lineTo(cx + 12, cy);
      ctx.moveTo(cx, cy - 12); ctx.lineTo(cx, cy + 12);
      ctx.stroke();
      ctx.restore();
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, Math.PI * 2);
      ctx.lineWidth = 3;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
    }
  }

  function cropPoint(e) {
    const rect = $("crop-canvas").getBoundingClientRect();
    return [e.clientX - rect.left, e.clientY - rect.top];
  }

  $("crop-canvas").addEventListener("pointerdown", (e) => {
    if (!crop.view) return;
    const [px, py] = cropPoint(e);
    const { s, ox, oy } = crop.view;
    let best = -1;
    let bestDist = 40; // touch-friendly hit radius
    crop.quad.forEach(([x, y], i) => {
      const d = Math.hypot(ox + x * s - px, oy + y * s - py);
      if (d < bestDist) { best = i; bestDist = d; }
    });
    if (best < 0) return;
    crop.drag = best;
    e.target.setPointerCapture(e.pointerId);
    e.preventDefault();
    drawCrop();
  });

  $("crop-canvas").addEventListener("pointermove", (e) => {
    if (crop.drag < 0) return;
    const [px, py] = cropPoint(e);
    const { s, ox, oy } = crop.view;
    crop.quad[crop.drag] = [
      Math.min(crop.image.width - 1, Math.max(0, (px - ox) / s)),
      Math.min(crop.image.height - 1, Math.max(0, (py - oy) / s)),
    ];
    drawCrop();
  });

  const endDrag = () => {
    if (crop.drag < 0) return;
    crop.drag = -1;
    drawCrop();
  };
  $("crop-canvas").addEventListener("pointerup", endDrag);
  $("crop-canvas").addEventListener("pointercancel", endDrag);

  $("crop-detect").addEventListener("click", () => {
    const quad = detectOnCanvas(crop.image);
    if (quad) {
      crop.quad = quad;
      drawCrop();
    } else {
      toast("No page edges found – drag the corners by hand.", true);
    }
  });

  $("crop-full").addEventListener("click", () => {
    crop.quad = fullQuad(crop.image.width, crop.image.height);
    drawCrop();
  });

  function closeCrop() {
    crop.image = null;
    crop.page = null;
    if (crop.returnTo === "review") {
      renderReview();
      show("review");
    } else {
      renderTray();
      show("scan");
      startCamera();
    }
  }

  $("crop-cancel").addEventListener("click", closeCrop);

  $("crop-apply").addEventListener("click", async () => {
    const page = crop.page;
    const btn = $("crop-apply");
    btn.disabled = true;
    try {
      page.quad = window.DocDetect ? window.DocDetect.orderQuad(crop.quad) : crop.quad;
      page.filter = $("crop-filter").value;
      await renderPage(page, crop.image);
      closeCrop();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("done-scanning").addEventListener("click", () => {
    stopCamera();
    renderReview();
    show("review");
  });

  // ------------------------------------------------------------------ review
  function renderReview() {
    const existing = state.target ? state.target.page_count : 0;
    $("doc-title").parentElement.classList.toggle("hidden", Boolean(state.target));
    $("submit-doc").textContent = state.target ? "Upload & process" : "Upload & extract text";
    $("submit-doc").disabled = !state.pages.length && !(state.target && existing);
    $("review-grid").replaceChildren(...state.pages.map((p, i) => {
      const move = (delta) => {
        const j = i + delta;
        if (j < 0 || j >= state.pages.length) return;
        [state.pages[i], state.pages[j]] = [state.pages[j], state.pages[i]];
        renderReview();
      };
      return el("div", { class: "page-card" }, [
        el("div", { class: "img-wrap" }, [thumbImage(p)]),
        el("span", { class: "num", text: String(existing + i + 1) }),
        el("div", { class: "tools" }, [
          el("button", { type: "button", title: "Move left", "aria-label": "Move left", text: "◀", disabled: i === 0, onclick: () => move(-1) }),
          el("button", { type: "button", title: "Rotate", "aria-label": "Rotate 90 degrees", text: "⟳", onclick: () => { p.rotation = (p.rotation + 90) % 360; renderReview(); } }),
          el("button", { type: "button", title: "Adjust corners / filter", "aria-label": "Adjust corners", text: "✂", disabled: !p.original, onclick: () => openCrop(p, "review") }),
          el("button", { type: "button", title: "Delete", "aria-label": "Delete page", text: "✕", onclick: () => {
            URL.revokeObjectURL(p.url);
            state.pages.splice(i, 1);
            renderReview();
            renderTray();
          } }),
          el("button", { type: "button", title: "Move right", "aria-label": "Move right", text: "▶", disabled: i === state.pages.length - 1, onclick: () => move(1) }),
        ]),
      ]);
    }));
  }

  $("back-to-scan").addEventListener("click", () => {
    renderTray();
    show("scan");
    startCamera();
  });

  // ------------------------------------------------------------------ upload + process
  function setProgress(percent, step, title) {
    $("progress-bar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
    $("progress-step").textContent = step || "";
    if (title) $("progress-title").textContent = title;
  }

  function showProgressError(message) {
    $("progress-error").textContent = message;
    $("progress-error").classList.remove("hidden");
    $("retry-doc").classList.remove("hidden");
    $("progress-home").classList.remove("hidden");
  }

  function resetProgressView(title) {
    $("progress-error").classList.add("hidden");
    $("retry-doc").classList.add("hidden");
    $("progress-home").classList.add("hidden");
    setProgress(0, "", title);
  }

  $("submit-doc").addEventListener("click", () => runUploadAndSubmit());

  async function runUploadAndSubmit() {
    resetProgressView("Uploading…");
    show("progress");
    state.phase = "upload";
    try {
      if (!state.docId) {
        if (state.target) {
          state.docId = state.target.id;
        } else {
          const payload = { document_type: state.type.code };
          const title = $("doc-title").value.trim();
          if (title) payload.title = title;
          const doc = await api("/documents/", { method: "POST", json: payload });
          state.docId = doc.id;
        }
      }
      // Upload pages that have not been uploaded yet (a retry continues where it stopped).
      const total = state.pages.length;
      for (let i = 0; i < total; i++) {
        const page = state.pages[i];
        if (page.uploaded) continue;
        setProgress((i / Math.max(total, 1)) * 100, `Uploading page ${i + 1} of ${total}…`);
        const form = new FormData();
        form.append("image", page.blob, page.name);
        form.append("rotation", String(page.rotation));
        await api(`/documents/${state.docId}/pages/`, { method: "POST", form });
        page.uploaded = true;
      }
      setProgress(100, "Upload complete");

      const status = state.target ? state.target.status : "draft";
      const endpoint = ["draft", "uploading"].includes(status) ? "submit" : status === "failed" ? "retry" : "reprocess";
      await api(`/documents/${state.docId}/${endpoint}/`, { method: "POST" });
      startPolling(state.docId);
    } catch (err) {
      showProgressError(err.message);
    }
  }

  $("retry-doc").addEventListener("click", async () => {
    if (state.phase === "upload") return runUploadAndSubmit();
    resetProgressView("Processing…");
    try {
      await api(`/documents/${state.docId}/retry/`, { method: "POST" });
      startPolling(state.docId);
    } catch (err) {
      showProgressError(err.message);
    }
  });

  function startPolling(docId) {
    stopPolling();
    state.docId = docId;
    state.phase = "process";
    resetProgressView("Processing…");
    show("progress");
    const tick = async () => {
      try {
        const s = await api(`/documents/${docId}/status/`);
        setProgress(s.progress, STEP_LABELS[s.current_step] || (s.current_step || "").replace(/_/g, " "));
        if (s.status === "ready") {
          stopPolling();
          openDocument(docId);
          return;
        }
        if (s.status === "failed") {
          stopPolling();
          $("progress-title").textContent = "Processing failed";
          showProgressError(s.error_message || "Something went wrong.");
          return;
        }
      } catch (err) {
        if (err.status === 404) { stopPolling(); showProgressError("The document no longer exists."); return; }
        // transient network problem: keep polling
      }
      state.pollTimer = setTimeout(tick, 2000);
    };
    tick();
  }

  function stopPolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }

  // ------------------------------------------------------------------ result
  async function openDocument(id) {
    let doc;
    try {
      doc = await api(`/documents/${id}/`);
    } catch (err) {
      toast(err.message, true);
      return;
    }
    state.result = doc;
    const type = state.types.find((t) => t.code === doc.document_type.code) || doc.document_type;

    if (doc.status === "processing") {
      startPolling(doc.id);
      return;
    }
    if (doc.status === "failed") {
      resetSession();
      state.docId = doc.id;
      state.phase = "process";
      resetProgressView("Processing failed");
      setProgress(doc.progress, "");
      show("progress");
      showProgressError(doc.error_message || "Something went wrong.");
      return;
    }
    if (doc.status === "draft" || doc.status === "uploading") {
      // An unfinished scan: continue adding pages.
      startScan(type, { id: doc.id, status: doc.status, page_count: doc.page_count });
      if (doc.page_count) toast(`${doc.page_count} page(s) already uploaded – add more or tap Done.`);
      return;
    }
    renderResult(doc);
    show("result");
  }

  function renderResult(doc) {
    $("result-title").textContent = doc.title;
    const badge = $("result-status");
    badge.textContent = doc.status;
    badge.className = `badge ${doc.status}`;
    const parts = [doc.document_type.name, `${doc.page_count} page${doc.page_count === 1 ? "" : "s"}`];
    if (doc.language) parts.push(`language: ${doc.language}`);
    if (doc.average_confidence != null) parts.push(`confidence ${Math.round(doc.average_confidence * 100)}%`);
    parts.push(new Date(doc.updated_at).toLocaleString());
    $("result-meta").textContent = parts.join(" · ");

    const low = (doc.analysis && doc.analysis.low_confidence_pages) || [];
    $("result-warning").classList.toggle("hidden", !low.length);
    $("result-warning").textContent = low.length
      ? `Page${low.length > 1 ? "s" : ""} ${low.map((p) => p.page).join(", ")} may be blurry or hard to read – check the text, or rescan.`
      : "";

    $("result-text").value = doc.full_text || "";
    $("save-text").disabled = true;
    $("dl-pdf").disabled = !doc.pdf_url;

    renderFields(doc);
    renderAnalysis(doc.analysis || {});
    $("result-pages").replaceChildren(...doc.pages.map((p) => el("div", { class: `page-card${p.is_low_quality ? " low" : ""}` }, [
      el("a", { href: p.image_url, target: "_blank", rel: "noopener", class: "img-wrap" }, [el("img", { src: p.image_url, alt: `Page ${p.order}`, loading: "lazy" })]),
      el("span", { class: "num", text: String(p.order) }),
      el("div", { class: "conf", text: p.ocr_confidence != null ? `OCR confidence ${Math.round(p.ocr_confidence * 100)}%` : "" }),
    ])));
  }

  // ---- field rendering
  function isCurrency(v) {
    return v && typeof v === "object" && !Array.isArray(v) && "amount" in v;
  }

  function formatValue(v) {
    if (v === null || v === undefined || v === "") return "—";
    if (isCurrency(v)) {
      const amount = typeof v.amount === "number" ? v.amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : v.amount;
      return `${v.currency_symbol || (v.currency_code ? v.currency_code + " " : "")}${amount}`;
    }
    if (Array.isArray(v)) return v.map(formatValue).join(", ");
    if (typeof v === "object") {
      return Object.entries(v).filter(([, x]) => x !== null && x !== "").map(([k, x]) => `${k}: ${formatValue(x)}`).join(", ");
    }
    if (typeof v === "number") return v.toLocaleString();
    return String(v);
  }

  function objectTable(rows) {
    const columns = [...new Set(rows.flatMap((r) => Object.keys(r || {})))];
    return el("div", { class: "table-wrap" }, [
      el("table", { class: "items" }, [
        el("thead", {}, [el("tr", {}, columns.map((c) => el("th", { text: c })))]),
        el("tbody", {}, rows.map((r) => el("tr", {}, columns.map((c) => el("td", { text: formatValue(r ? r[c] : null) }))))),
      ]),
    ]);
  }

  function cellTable(table) {
    const grid = Array.from({ length: table.row_count }, () => Array(table.column_count).fill(""));
    const headerRows = new Set();
    for (const cell of table.cells) {
      if (grid[cell.row]) grid[cell.row][cell.col] = cell.content;
      if (cell.kind === "columnHeader") headerRows.add(cell.row);
    }
    return el("div", { class: "table-wrap" }, [
      el("table", {}, grid.map((row, r) => el("tr", {}, row.map((text) => el(headerRows.has(r) ? "th" : "td", { text }))))),
    ]);
  }

  function renderFields(doc) {
    const fields = doc.extracted_fields || {};
    const box = $("fields");
    box.replaceChildren();
    const keys = Object.keys(fields);
    $("fields-card").classList.toggle("hidden", !keys.length);
    if (!keys.length) return;

    // Layout model (Form / Table): tables + markdown.
    if ("tables" in fields || "markdown" in fields) {
      (fields.tables || []).forEach((t, i) => {
        box.append(el("h3", { text: `Table ${i + 1}` }), cellTable(t));
      });
      if (!(fields.tables || []).length) box.append(el("p", { class: "muted", text: "No tables were found." }));
      return;
    }

    const display = doc.document_type.display_fields || [];
    const ordered = [...display.filter((k) => k in fields), ...keys.filter((k) => !display.includes(k)).sort()];
    const rows = [];
    const extras = [];
    for (const key of ordered) {
      const field = fields[key];
      const value = field && typeof field === "object" && "value" in field ? field.value : field;
      const label = key.replace(/([a-z])([A-Z])/g, "$1 $2");
      if (Array.isArray(value) && value.length && typeof value[0] === "object" && !isCurrency(value[0])) {
        extras.push(el("h3", { text: label }), objectTable(value));
        continue;
      }
      const conf = field && field.confidence != null ? el("span", { class: "conf-pill", text: `${Math.round(field.confidence * 100)}%` }) : null;
      rows.push(el("tr", {}, [el("th", { text: label }), el("td", {}, [formatValue(value), conf])]));
    }
    if (rows.length) box.append(el("table", {}, [el("tbody", {}, rows)]));
    box.append(...extras);
  }

  function renderAnalysis(a) {
    const box = $("analysis");
    box.replaceChildren();
    const groups = [
      ["Dates", (a.dates || []).map((d) => `${d.raw} → ${d.date}`)],
      ["Amounts", (a.amounts || []).map((x) => x.raw)],
      ["Emails", a.emails || []],
      ["Phone numbers", a.phones || []],
      ["GSTIN", a.gstins || []],
      ["PAN", a.pans || []],
    ].filter(([, items]) => items.length);
    if (a.word_count != null) box.append(el("p", { class: "muted", text: `${a.word_count} words · ${a.character_count || 0} characters` }));
    for (const [title, items] of groups) {
      box.append(el("strong", { text: title }), el("div", { class: "chips" }, items.map((t) => el("span", { class: "chip", text: t }))));
    }
    $("analysis-card").classList.toggle("hidden", !groups.length && a.word_count == null);
  }

  // ---- result actions
  $("result-text").addEventListener("input", () => { $("save-text").disabled = false; });

  $("save-text").addEventListener("click", async () => {
    try {
      const doc = await api(`/documents/${state.result.id}/`, { method: "PATCH", json: { full_text: $("result-text").value } });
      state.result = doc;
      $("save-text").disabled = true;
      toast("Text saved.");
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("copy-text").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("result-text").value);
      toast("Copied to the clipboard.");
    } catch {
      $("result-text").select();
      document.execCommand("copy");
      toast("Copied.");
    }
  });

  $("dl-pdf").addEventListener("click", async () => {
    // Open the tab synchronously so popup blockers allow it, then point it at the (signed) URL.
    const tab = window.open("", "_blank");
    try {
      const { url } = await api(`/documents/${state.result.id}/download/?format=pdf&link=true`);
      if (tab) tab.location.href = url;
      else window.location.href = url;
    } catch (err) {
      if (tab) tab.close();
      toast(err.message, true);
    }
  });

  async function downloadFile(format) {
    try {
      const res = await api(`/documents/${state.result.id}/download/?format=${format}`, { raw: true });
      const blob = await res.blob();
      const match = /filename="?([^"]+)"?/.exec(res.headers.get("Content-Disposition") || "");
      const a = el("a", { href: URL.createObjectURL(blob), download: match ? match[1] : `document.${format}` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (err) {
      toast(err.message, true);
    }
  }
  $("dl-txt").addEventListener("click", () => downloadFile("txt"));
  $("dl-json").addEventListener("click", () => downloadFile("json"));

  $("add-pages").addEventListener("click", () => {
    const doc = state.result;
    const type = state.types.find((t) => t.code === doc.document_type.code) || doc.document_type;
    if (doc.page_count >= type.max_pages) {
      toast(`${type.name} documents can have at most ${type.max_pages} pages.`, true);
      return;
    }
    startScan(type, { id: doc.id, status: doc.status, page_count: doc.page_count });
  });

  $("delete-doc").addEventListener("click", async () => {
    if (!confirm(`Delete “${state.result.title}” and all its files?`)) return;
    try {
      await api(`/documents/${state.result.id}/`, { method: "DELETE" });
      toast("Document deleted.");
      goHome();
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ------------------------------------------------------------------ boot
  (async function boot() {
    if (!state.access) {
      setAuthMode(false);
      show("auth");
      return;
    }
    try {
      await afterLogin();
    } catch {
      logout();
    }
  })();
})();
