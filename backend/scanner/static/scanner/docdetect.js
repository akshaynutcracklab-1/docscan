/* Document edge detection and perspective correction with OpenCV.js.
 *
 *   DocDetect.load()                     -> Promise<{cv}>  (loads OpenCV.js once, lazily)
 *   DocDetect.detectQuad(cv, rgbaMat)    -> [[x,y] tl, tr, br, bl] in source pixels, or null
 *   DocDetect.warp(cv, rgbaMat, quad)    -> new RGBA Mat with the page flattened ("scanned")
 *   DocDetect.blackWhite(cv, rgbaMat)    -> new RGBA Mat, adaptive-threshold black & white
 *
 * The core functions take `cv` explicitly and never touch the DOM, so they also run in Node (tests).
 * Callers own (and must .delete()) every Mat they pass in or get back.
 */
(function (root) {
  "use strict";

  const OPENCV_URL = "https://cdn.jsdelivr.net/npm/@techstark/opencv-js@4.12.0-release.1/dist/opencv.js";
  const MAX_OUTPUT_SIDE = 3000;
  let loading = null;

  /** Resolve once an OpenCV module (plain object, Emscripten thenable, or Promise) is ready. */
  function whenReady(candidate, done) {
    if (!candidate) return false;
    if (candidate.Mat) {
      done(candidate);
    } else if (typeof candidate.then === "function") {
      // Emscripten's Module.then is not a real Promise; never `await` it (it resolves to itself).
      candidate.then((mod) => done(mod));
    } else {
      candidate.onRuntimeInitialized = () => done(candidate);
    }
    return true;
  }

  function load(url = OPENCV_URL, timeoutMs = 90000) {
    if (loading) return loading;
    loading = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Timed out loading OpenCV.js")), timeoutMs);
      // Wrap in an object: resolving a Promise with the thenable module would loop forever.
      const done = (cv) => { clearTimeout(timer); resolve({ cv }); };
      if (root.cv && whenReady(root.cv, done)) return;
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.crossOrigin = "anonymous";
      script.onload = () => { if (!whenReady(root.cv, done)) reject(new Error("OpenCV.js did not initialise")); };
      script.onerror = () => { clearTimeout(timer); reject(new Error("Could not download OpenCV.js")); };
      document.head.append(script);
    }).catch((err) => {
      loading = null; // allow a later retry
      throw err;
    });
    return loading;
  }

  // ------------------------------------------------------------------ geometry

  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

  /** Order 4 points as top-left, top-right, bottom-right, bottom-left. */
  function orderQuad(points) {
    const bySum = [...points].sort((a, b) => (a[0] + a[1]) - (b[0] + b[1]));
    const byDiff = [...points].sort((a, b) => (a[1] - a[0]) - (b[1] - b[0]));
    const tl = bySum[0];
    const br = bySum[3];
    const tr = byDiff[0];
    const bl = byDiff[3];
    // Degenerate input (e.g. a diamond) can pick one point twice; fall back to angle ordering.
    const unique = new Set([tl, tr, br, bl]);
    if (unique.size === 4) return [tl, tr, br, bl].map((p) => [p[0], p[1]]);
    const cx = points.reduce((s, p) => s + p[0], 0) / 4;
    const cy = points.reduce((s, p) => s + p[1], 0) / 4;
    const sorted = [...points].sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
    // atan2 order starts at the left (-π): rotate so the point nearest top-left comes first.
    let start = 0;
    sorted.forEach((p, i) => { if (p[0] + p[1] < sorted[start][0] + sorted[start][1]) start = i; });
    return [0, 1, 2, 3].map((k) => sorted[(start + k) % 4]).map((p) => [p[0], p[1]]);
  }

  function polygonArea(q) {
    let a = 0;
    for (let i = 0; i < q.length; i++) {
      const [x1, y1] = q[i];
      const [x2, y2] = q[(i + 1) % q.length];
      a += x1 * y2 - x2 * y1;
    }
    return Math.abs(a) / 2;
  }

  function medianOf(gray) {
    const hist = new Uint32Array(256);
    const data = gray.data;
    for (let i = 0; i < data.length; i++) hist[data[i]]++;
    let acc = 0;
    for (let v = 0; v < 256; v++) {
      acc += hist[v];
      if (acc >= data.length / 2) return v;
    }
    return 127;
  }

  /** Largest convex 4-corner contour in a binary image, as points in that image's pixels. */
  function bestQuadIn(cv, binary, minArea, maxArea) {
    const contours = new cv.MatVector();
    const hierarchy = new cv.Mat();
    let best = null;
    try {
      cv.findContours(binary, contours, hierarchy, cv.RETR_LIST, cv.CHAIN_APPROX_SIMPLE);
      for (let i = 0; i < contours.size(); i++) {
        const contour = contours.get(i);
        const area = cv.contourArea(contour);
        if (area < minArea || area > maxArea || (best && area <= best.area)) {
          contour.delete();
          continue;
        }
        const peri = cv.arcLength(contour, true);
        const hull = new cv.Mat();
        const approx = new cv.Mat();
        cv.convexHull(contour, hull, false, true);
        // Increase tolerance until the outline simplifies to 4 corners (rounded corners, folds...).
        for (const eps of [0.02, 0.03, 0.045, 0.06, 0.08]) {
          cv.approxPolyDP(hull, approx, eps * peri, true);
          if (approx.rows <= 4) break;
        }
        if (approx.rows === 4 && cv.isContourConvex(approx)) {
          const pts = [];
          for (let k = 0; k < 4; k++) pts.push([approx.data32S[k * 2], approx.data32S[k * 2 + 1]]);
          const quadArea = polygonArea(orderQuad(pts));
          if (quadArea >= minArea && quadArea <= maxArea) best = { area: quadArea, pts };
        }
        hull.delete();
        approx.delete();
        contour.delete();
      }
    } finally {
      contours.delete();
      hierarchy.delete();
    }
    return best;
  }

  /**
   * Find the document's 4 corners.
   * Two passes on a downscaled copy: edges (auto Canny) and brightness (Otsu); the larger valid quad wins.
   */
  function detectQuad(cv, src, { maxSide = 640, minAreaRatio = 0.12 } = {}) {
    const scale = Math.min(1, maxSide / Math.max(src.cols, src.rows));
    const size = new cv.Size(Math.max(1, Math.round(src.cols * scale)), Math.max(1, Math.round(src.rows * scale)));
    const small = new cv.Mat();
    const gray = new cv.Mat();
    const blurred = new cv.Mat();
    const edges = new cv.Mat();
    const binary = new cv.Mat();
    const kernel = cv.getStructuringElement(cv.MORPH_RECT, new cv.Size(3, 3));
    try {
      cv.resize(src, small, size, 0, 0, cv.INTER_AREA);
      cv.cvtColor(small, gray, src.channels() === 4 ? cv.COLOR_RGBA2GRAY : cv.COLOR_RGB2GRAY);
      cv.GaussianBlur(gray, blurred, new cv.Size(5, 5), 0);

      const frameArea = size.width * size.height;
      const minArea = frameArea * minAreaRatio;
      const maxArea = frameArea * 0.995;

      const median = medianOf(blurred);
      cv.Canny(blurred, edges, Math.max(10, 0.66 * median), Math.min(255, Math.max(40, 1.33 * median)));
      cv.morphologyEx(edges, edges, cv.MORPH_CLOSE, kernel, new cv.Point(-1, -1), 2);
      cv.dilate(edges, edges, kernel, new cv.Point(-1, -1), 1);
      const fromEdges = bestQuadIn(cv, edges, minArea, maxArea);

      cv.threshold(blurred, binary, 0, 255, cv.THRESH_BINARY + cv.THRESH_OTSU);
      cv.morphologyEx(binary, binary, cv.MORPH_OPEN, kernel, new cv.Point(-1, -1), 2);
      const fromBrightness = bestQuadIn(cv, binary, minArea, maxArea);

      const best = [fromEdges, fromBrightness].filter(Boolean).sort((a, b) => b.area - a.area)[0];
      if (!best) return null;
      return orderQuad(best.pts.map(([x, y]) => [
        Math.min(src.cols - 1, Math.max(0, x / scale)),
        Math.min(src.rows - 1, Math.max(0, y / scale)),
      ]));
    } finally {
      [small, gray, blurred, edges, binary, kernel].forEach((m) => m.delete());
    }
  }

  /** Flatten the quad into an upright rectangle (perspective correction). */
  function warp(cv, src, quad, maxSide = MAX_OUTPUT_SIDE) {
    const [tl, tr, br, bl] = orderQuad(quad);
    let width = Math.max(dist(tl, tr), dist(bl, br));
    let height = Math.max(dist(tl, bl), dist(tr, br));
    const scale = Math.min(1, maxSide / Math.max(width, height));
    width = Math.max(1, Math.round(width * scale));
    height = Math.max(1, Math.round(height * scale));
    const from = cv.matFromArray(4, 1, cv.CV_32FC2, [...tl, ...tr, ...br, ...bl]);
    const to = cv.matFromArray(4, 1, cv.CV_32FC2, [0, 0, width - 1, 0, width - 1, height - 1, 0, height - 1]);
    const matrix = cv.getPerspectiveTransform(from, to);
    const dst = new cv.Mat();
    try {
      cv.warpPerspective(src, dst, matrix, new cv.Size(width, height), cv.INTER_LINEAR, cv.BORDER_REPLICATE, new cv.Scalar());
      return dst;
    } finally {
      from.delete();
      to.delete();
      matrix.delete();
    }
  }

  /** Crisp black & white "photocopy" look; robust to shadows thanks to the adaptive threshold. */
  function blackWhite(cv, src) {
    const gray = new cv.Mat();
    const bw = new cv.Mat();
    const out = new cv.Mat();
    try {
      cv.cvtColor(src, gray, src.channels() === 4 ? cv.COLOR_RGBA2GRAY : cv.COLOR_RGB2GRAY);
      let block = Math.round(Math.min(src.cols, src.rows) / 40) | 1; // must be odd
      block = Math.max(15, Math.min(block, 75));
      cv.adaptiveThreshold(gray, bw, 255, cv.ADAPTIVE_THRESH_GAUSSIAN_C, cv.THRESH_BINARY, block, 12);
      cv.cvtColor(bw, out, cv.COLOR_GRAY2RGBA);
      return out;
    } finally {
      gray.delete();
      bw.delete();
    }
  }

  root.DocDetect = { OPENCV_URL, load, whenReady, detectQuad, warp, blackWhite, orderQuad, polygonArea };
})(typeof window !== "undefined" ? window : globalThis);
