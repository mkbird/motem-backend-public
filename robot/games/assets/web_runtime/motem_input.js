// Motem Web visual-control runtime.
//
// The third InputBridge producer (after iOS FaceDetector and macOS
// MacFaceDetector). Runs the MediaPipe vision stack in the browser and writes
// the latest control state to `window.__motem_input` as a JSON string each
// frame. The Godot Web build's InputBridge._poll_web_input() reads that global
// via JavaScriptBridge — the game code itself is unchanged and input-source
// agnostic.
//
// Contract (all fields optional; absent => "not detected"):
//   { face_x, face_y, face_detected,
//     body_lean_x, body_lean_y, body_detected, is_ducking, is_hands_up,
//     hand_x, hand_y, hand_gesture }
//
// v1 scope: face control only (face_position). Body-pose / hand-gesture
// producers are added later behind the same contract.

const MP_VERSION = "0.10.14";
const MP_CDN = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VERSION}`;
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

const _state = {
  face_x: 0.5, face_y: 0.5, face_detected: false,
};
function _publish() { window.__motem_input = JSON.stringify(_state); }
_publish();

// Minimal status HUD (top-left) + camera preview (bottom-right).
const status = document.createElement("div");
status.style.cssText =
  "position:fixed;left:8px;top:8px;z-index:99999;font:12px monospace;" +
  "background:rgba(0,0,0,.6);color:#0f0;padding:4px 8px;border-radius:4px;pointer-events:none;";
status.textContent = "[motem] init…";
document.body.appendChild(status);

const video = document.createElement("video");
video.autoplay = true; video.playsInline = true; video.muted = true;
video.style.cssText =
  "position:fixed;right:8px;bottom:8px;width:140px;height:105px;z-index:99999;" +
  "transform:scaleX(-1);border:1px solid #0f0;border-radius:4px;opacity:.85;";
document.body.appendChild(video);

async function main() {
  try {
    const { FaceLandmarker, FilesetResolver } =
      await import(`${MP_CDN}/vision_bundle.mjs`);

    status.textContent = "[motem] requesting camera…";
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" }, audio: false,
    });
    video.srcObject = stream;
    await video.play();

    status.textContent = "[motem] loading wasm…";
    const fileset = await FilesetResolver.forVisionTasks(`${MP_CDN}/wasm`);

    // Stream the model with a visible progress %, then hand the bytes over.
    status.textContent = "[motem] model 0%…";
    const resp = await fetch(MODEL_URL);
    const total = parseInt(resp.headers.get("content-length") || "0", 10);
    const reader = resp.body.getReader();
    const chunks = []; let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value); received += value.length;
      const pct = total ? Math.round(received * 100 / total) : "?";
      status.textContent =
        `[motem] model ${pct}% (${(received / 1048576).toFixed(1)}MB)`;
    }
    const buf = new Uint8Array(received);
    let off = 0; for (const c of chunks) { buf.set(c, off); off += c.length; }

    status.textContent = "[motem] init landmarker…";
    const landmarker = await FaceLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetBuffer: buf },
      runningMode: "VIDEO",
      numFaces: 1,
    });

    status.textContent = "[motem] running";
    let last = -1;
    const loop = () => {
      const t = performance.now();
      if (video.readyState >= 2 && t !== last) {
        last = t;
        const res = landmarker.detectForVideo(video, t);
        if (res && res.faceLandmarks && res.faceLandmarks.length > 0) {
          const nose = res.faceLandmarks[0][1]; // nose tip
          // front camera is mirrored -> mirror x so head-right = control-right
          _state.face_x = 1.0 - nose.x;
          _state.face_y = nose.y;
          _state.face_detected = true;
          status.textContent =
            `[motem] face ${_state.face_x.toFixed(2)},${_state.face_y.toFixed(2)}`;
        } else {
          _state.face_detected = false;
          status.textContent = "[motem] no face";
        }
        _publish();
      }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  } catch (e) {
    status.textContent = "[motem] ERROR: " + e;
    console.error("[motem]", e);
  }
}
main();
