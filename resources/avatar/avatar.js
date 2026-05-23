/* Live2D persona viewer
 *
 * Hosted in a QWebEngineView. Communicates with Python via QWebChannel.
 * Python publishes:
 *   - bridge.modelChanged(absoluteUrl, configJson)   (signal)
 *   - bridge.speakingStart(envelopeJson, sampleRate, reaction)
 *   - bridge.speakingEnd()
 *   - bridge.expressionRequested(name)
 *   - bridge.motionRequested(group, index)
 *
 * JS publishes via slot invocations:
 *   - bridge.onReady(payloadJson)
 *   - bridge.onError(message)
 *   - bridge.onDragMoved(dx, dy)
 *   - bridge.onLog(message)
 */
(function () {
  "use strict";

  const PIXI_OK = !window.__pixiMissing && typeof PIXI !== "undefined";
  const PIXI_LIVE2D_OK =
    !window.__pixiLive2dMissing &&
    PIXI_OK &&
    typeof PIXI.live2d !== "undefined" &&
    typeof PIXI.live2d.Live2DModel !== "undefined";
  const CUBISM_OK = !window.__cubismCoreMissing && typeof Live2DCubismCore !== "undefined";
  const QWEBCHANNEL_OK = !window.__qwebchannelMissing && typeof QWebChannel !== "undefined";

  const banner = document.getElementById("banner");
  const stage = document.getElementById("stage");
  const dragHandle = document.getElementById("drag");

  let bridge = null;
  let app = null;
  let model = null;
  let modelConfig = {
    expression_map: {},
    scale: 0.25,
    anchor: "bottom-right",
    mirror: false,
    lip_sync_gain: 1.2,
  };
  let envelope = null;
  let envelopeStart = 0;
  let envelopeFps = 60;
  let mouthValue = 0;
  let speaking = false;
  let loadToken = 0;

  function showBanner(html) {
    banner.innerHTML = html;
    banner.classList.add("visible");
  }

  function hideBanner() {
    banner.classList.remove("visible");
    banner.innerHTML = "";
  }

  function log(msg) {
    if (bridge && typeof bridge.onLog === "function") {
      try { bridge.onLog(String(msg)); } catch (_) {}
    }
  }

  function report(msg) {
    console.error(msg);
    if (bridge && typeof bridge.onError === "function") {
      try { bridge.onError(String(msg)); } catch (_) {}
    }
  }

  function checkPrereqs() {
    const missing = [];
    if (!PIXI_OK) missing.push("PixiJS");
    if (!PIXI_LIVE2D_OK) missing.push("pixi-live2d-display");
    if (!CUBISM_OK) missing.push("Live2D Cubism Core (live2dcubismcore.min.js)");
    if (!QWEBCHANNEL_OK) missing.push("Qt WebChannel");
    if (missing.length) {
      showBanner(
        "<b>Persona disabled.</b><br>Missing: " +
          missing.map((m) => "<code>" + m + "</code>").join(", ") +
          ".<br>Run <code>python scripts/fetch_live2d_core.py</code> to install."
      );
      return false;
    }
    return true;
  }

  function setupStage() {
    app = new PIXI.Application({
      view: undefined,
      autoStart: true,
      backgroundAlpha: 0,
      antialias: true,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
      resizeTo: stage,
    });
    stage.appendChild(app.view);

    app.ticker.add(onTick);

    window.addEventListener("resize", () => {
      app.renderer.resize(stage.clientWidth, stage.clientHeight);
      layoutModel();
    });
  }

  function layoutModel() {
    if (!model || !app) return;
    const desired = Math.max(0.05, Math.min(2.0, Number(modelConfig.scale) || 0.25));
    const targetHeight = app.renderer.height * desired * 4;
    const scale = targetHeight / model.internalModel.height;
    model.scale.set(scale, scale);
    if (modelConfig.mirror) model.scale.x = -Math.abs(model.scale.x);

    const anchor = (modelConfig.anchor || "bottom-center").toLowerCase();
    const w = app.renderer.width;
    const h = app.renderer.height;
    const modelW = model.width;
    const modelH = model.height;

    let x = w / 2 - modelW / 2;
    let y = h - modelH;
    if (anchor.includes("left")) x = 8;
    else if (anchor.includes("right")) x = w - modelW - 8;
    if (anchor.includes("top")) y = 8;
    else if (anchor.includes("center") && !anchor.includes("bottom")) y = h / 2 - modelH / 2;

    model.position.set(x, y);
  }

  async function loadModel(absoluteUrl, configJson) {
    const myToken = ++loadToken;
    try {
      if (configJson) {
        try { modelConfig = Object.assign(modelConfig, JSON.parse(configJson)); }
        catch (e) { report("bad config json: " + e); }
      }
      if (!absoluteUrl) {
        showBanner(
          "<b>No avatar model selected.</b><br>Open <b>Settings &rarr; Persona</b> and pick a <code>.model3.json</code> file."
        );
        return;
      }
      hideBanner();
      log("loading model: " + absoluteUrl);
      if (model) {
        app.stage.removeChild(model);
        model.destroy({ children: true, texture: true, baseTexture: true });
        model = null;
      }
      const loaded = await PIXI.live2d.Live2DModel.from(absoluteUrl, { autoInteract: false });
      if (myToken !== loadToken) {
        try { loaded.destroy({ children: true, texture: true, baseTexture: true }); } catch (_) {}
        log("discarding stale model load (token " + myToken + " vs " + loadToken + ")");
        return;
      }
      sweepStaleLive2DModels();
      model = loaded;
      app.stage.addChild(model);
      layoutModel();
      if (bridge && typeof bridge.onReady === "function") {
        const exps = (model.internalModel.settings && model.internalModel.settings.expressions) || [];
        try {
          bridge.onReady(JSON.stringify({
            expressions: exps.map((e) => e.Name || e.name || ""),
            width: model.internalModel.width,
            height: model.internalModel.height,
          }));
        } catch (_) {}
      }
    } catch (exc) {
      report("model load failed: " + (exc && exc.message ? exc.message : exc));
      showBanner(
        "<b>Failed to load model.</b><br><code>" +
          String(exc && exc.message ? exc.message : exc) +
          "</code>"
      );
    }
  }

  function sweepStaleLive2DModels() {
    if (!app || !app.stage) return;
    const Live2DModel = PIXI.live2d && PIXI.live2d.Live2DModel;
    if (!Live2DModel) return;
    const survivors = [];
    for (const child of app.stage.children.slice()) {
      if (child instanceof Live2DModel) {
        app.stage.removeChild(child);
        try { child.destroy({ children: true, texture: true, baseTexture: true }); } catch (_) {}
        survivors.push(child);
      }
    }
    if (survivors.length) {
      log("swept " + survivors.length + " stale Live2D model(s) from stage");
    }
  }

  function setParam(id, value) {
    if (!model) return;
    try {
      const core = model.internalModel.coreModel;
      if (typeof core.setParameterValueById === "function") {
        core.setParameterValueById(id, value);
      } else if (typeof core.setParamFloat === "function") {
        core.setParamFloat(id, value);
      }
    } catch (_) {}
  }

  function applyExpression(reaction) {
    if (!model) return;
    const map = modelConfig.expression_map || {};
    const key = (reaction || "").toLowerCase();
    const name = map[key];
    if (!name) return;
    try {
      const im = model.internalModel;
      const manager = im && im.motionManager && im.motionManager.expressionManager;
      if (manager && typeof manager.setExpression === "function") {
        manager.setExpression(name);
      }
    } catch (exc) {
      log("expression set failed: " + exc);
    }
  }

  function playMotion(group, index) {
    if (!model) return;
    try {
      model.motion(group, index | 0);
    } catch (exc) {
      log("motion failed: " + exc);
    }
  }

  function onTick() {
    if (!model) return;
    if (speaking && envelope && envelope.length) {
      const elapsed = (performance.now() - envelopeStart) / 1000;
      const idx = Math.floor(elapsed * envelopeFps);
      if (idx >= envelope.length) {
        speaking = false;
        envelope = null;
      } else {
        const target = envelope[idx] || 0;
        mouthValue = mouthValue * 0.4 + target * 0.6;
      }
    } else {
      mouthValue *= 0.6;
      if (mouthValue < 0.01) mouthValue = 0;
    }
    setParam("ParamMouthOpenY", mouthValue);
  }

  function startSpeaking(envelopeJson, sampleRate, reaction) {
    try {
      const data = JSON.parse(envelopeJson || "[]");
      envelope = Array.isArray(data) ? data : [];
    } catch (e) {
      envelope = [];
    }
    envelopeStart = performance.now();
    envelopeFps = 60;
    speaking = true;
    if (reaction) applyExpression(reaction);
  }

  function stopSpeaking() {
    speaking = false;
  }

  function setupDrag() {
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    function onDown(e) {
      dragging = true;
      lastX = e.screenX;
      lastY = e.screenY;
      e.preventDefault();
    }
    function onMove(e) {
      if (!dragging) return;
      const dx = e.screenX - lastX;
      const dy = e.screenY - lastY;
      lastX = e.screenX;
      lastY = e.screenY;
      if (bridge && typeof bridge.onDragMoved === "function") {
        try { bridge.onDragMoved(dx, dy); } catch (_) {}
      }
    }
    function onUp() { dragging = false; }
    dragHandle.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  function applyOverlayMode(isOverlay) {
    document.body.classList.toggle("overlay", !!isOverlay);
  }

  function attachBridge(channel) {
    bridge = channel.objects.bridge;
    if (!bridge) {
      report("AvatarBridge not registered on channel");
      return;
    }
    if (bridge.modelChanged && bridge.modelChanged.connect) {
      bridge.modelChanged.connect(loadModel);
    }
    if (bridge.speakingStart && bridge.speakingStart.connect) {
      bridge.speakingStart.connect(startSpeaking);
    }
    if (bridge.speakingEnd && bridge.speakingEnd.connect) {
      bridge.speakingEnd.connect(stopSpeaking);
    }
    if (bridge.expressionRequested && bridge.expressionRequested.connect) {
      bridge.expressionRequested.connect(applyExpression);
    }
    if (bridge.motionRequested && bridge.motionRequested.connect) {
      bridge.motionRequested.connect(playMotion);
    }
    if (bridge.overlayModeChanged && bridge.overlayModeChanged.connect) {
      bridge.overlayModeChanged.connect(applyOverlayMode);
    }
    if (typeof bridge.requestInitialState === "function") {
      try { bridge.requestInitialState(); } catch (_) {}
    }
  }

  function boot() {
    if (!checkPrereqs()) return;
    setupStage();
    setupDrag();
    new QWebChannel(qt.webChannelTransport, attachBridge);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
