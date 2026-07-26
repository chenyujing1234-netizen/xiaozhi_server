(function () {
  "use strict";

  var UPLINK_RATE = 16000;
  var DOWNLINK_RATE = 24000;
  var FRAME_MS = 60;
  var FRAME_SAMPLES = (UPLINK_RATE * FRAME_MS) / 1000;
  var OMNI_READY_MS = 800;
  var MIN_HOLD_MS = 900;
  var MIN_UPLINK_FRAMES = 5;

  var ws = null;
  var sessionId = null;
  var connected = false;
  var recording = false;
  var speaking = false;
  var uplinkLive = false;
  var uplinkFrames = 0;
  var pendingFrames = [];
  var talkStartTime = 0;
  var userTurnText = "";
  var spacePttActive = false;

  var captureCtx = null;
  var captureStream = null;
  var captureSource = null;
  var captureWorklet = null;
  var captureProcessor = null;
  var captureGain = null;
  var pcmCarry = new Int16Array(0);
  var captureSrcRate = UPLINK_RATE;

  var playCtx = null;
  var nextPlayTime = 0;
  var activeSources = [];
  var playbackMuted = false;

  var params = new URLSearchParams(window.location.search);
  var wsHost = params.get("ws_host") || window.location.hostname;
  var wsPort = params.get("ws_port") || "8000";
  var wsPath = params.get("ws_path") || "/xiaozhi/english/v1/";
  var WS_URL = "ws://" + wsHost + ":" + wsPort + wsPath;

  var WORKLET_CODE = [
    "class CaptureProcessor extends AudioWorkletProcessor {",
    "  process(inputs) {",
    "    const ch = inputs[0] && inputs[0][0];",
    "    if (ch && ch.length) this.port.postMessage(ch);",
    "    return true;",
    "  }",
    "}",
    "registerProcessor('capture-processor', CaptureProcessor);",
  ].join("\n");

  var el = {
    connDot: document.getElementById("conn-dot"),
    connText: document.getElementById("conn-text"),
    sessionText: document.getElementById("session-text"),
    stateText: document.getElementById("state-text"),
    userBubble: document.getElementById("user-bubble"),
    tutorBubble: document.getElementById("tutor-bubble"),
    correctionBubble: document.getElementById("correction-bubble"),
    userText: document.getElementById("user-text"),
    tutorText: document.getElementById("tutor-text"),
    correctionText: document.getElementById("correction-text"),
    hintText: document.getElementById("hint-text"),
    btnConnect: document.getElementById("btn-connect"),
    btnTalk: document.getElementById("btn-talk"),
    btnStop: document.getElementById("btn-stop"),
    logBox: document.getElementById("log-box"),
    wsUrl: document.getElementById("ws-url"),
  };

  el.wsUrl.textContent = WS_URL;

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function log(msg) {
    var line = new Date().toLocaleTimeString() + " " + msg;
    el.logBox.textContent = (el.logBox.textContent ? el.logBox.textContent + "\n" : "") + line;
    el.logBox.scrollTop = el.logBox.scrollHeight;
  }

  function setConn(on, busy) {
    el.connDot.className = "dot " + (on ? (busy ? "dot-busy" : "dot-on") : "dot-off");
    el.connText.textContent = on ? (busy ? "会话中" : "已连接") : "未连接";
  }

  function setState(text) {
    el.stateText.textContent = text;
  }

  function sendJson(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (sessionId) obj.session_id = sessionId;
    ws.send(JSON.stringify(obj));
    log("→ " + JSON.stringify(obj));
  }

  function resampleFloat32(input, srcRate, dstRate) {
    if (srcRate === dstRate) return input;
    var outLen = Math.max(1, Math.round(input.length * dstRate / srcRate));
    var out = new Float32Array(outLen);
    for (var i = 0; i < outLen; i++) {
      var srcPos = i * srcRate / dstRate;
      var idx = Math.floor(srcPos);
      var frac = srcPos - idx;
      var s0 = input[Math.min(idx, input.length - 1)];
      var s1 = input[Math.min(idx + 1, input.length - 1)];
      out[i] = s0 + (s1 - s0) * frac;
    }
    return out;
  }

  function floatToInt16(float32) {
    var out = new Int16Array(float32.length);
    for (var i = 0; i < float32.length; i++) {
      var s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  function concatInt16(a, b) {
    var merged = new Int16Array(a.length + b.length);
    merged.set(a);
    merged.set(b, a.length);
    return merged;
  }

  function sendPcmFrame(frame) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    var copy = frame.length === FRAME_SAMPLES ? frame : new Int16Array(frame);
    ws.send(copy.buffer.slice(copy.byteOffset, copy.byteOffset + copy.byteLength));
    uplinkFrames += 1;
  }

  function flushUplink(int16, force) {
    pcmCarry = concatInt16(pcmCarry, int16);
    while (pcmCarry.length >= FRAME_SAMPLES) {
      var frame = new Int16Array(pcmCarry.subarray(0, FRAME_SAMPLES));
      pcmCarry = pcmCarry.subarray(FRAME_SAMPLES);
      if (uplinkLive && ((recording && !speaking) || force)) {
        sendPcmFrame(frame);
      } else if (recording && !speaking) {
        pendingFrames.push(frame);
      }
    }
  }

  function flushPendingFrames() {
    uplinkLive = true;
    talkStartTime = Date.now();
    for (var i = 0; i < pendingFrames.length; i++) {
      sendPcmFrame(pendingFrames[i]);
    }
    pendingFrames = [];
    log("Omni 就绪，已补发 " + uplinkFrames + " 帧缓存音频");
  }

  function flushRemainder(force) {
    if (pcmCarry.length > 0 && (force || (recording && !speaking))) {
      var padded = new Int16Array(FRAME_SAMPLES);
      padded.set(pcmCarry);
      if (uplinkLive || force) {
        sendPcmFrame(padded);
      } else {
        pendingFrames.push(padded);
      }
      pcmCarry = new Int16Array(0);
    }
  }

  async function ensurePlayContext() {
    if (!playCtx) {
      playCtx = new AudioContext({ sampleRate: DOWNLINK_RATE });
    }
    if (playCtx.state === "suspended") {
      await playCtx.resume();
    }
    return playCtx;
  }

  function stopLocalPlayback() {
    playbackMuted = true;
    for (var i = 0; i < activeSources.length; i++) {
      try {
        activeSources[i].stop(0);
      } catch (e) { /* already stopped */ }
      try {
        activeSources[i].disconnect();
      } catch (e2) { /* ignore */ }
    }
    activeSources = [];
    nextPlayTime = 0;
  }

  async function playDownlinkPcm(arrayBuffer) {
    if (playbackMuted || !speaking) return;
    var ctx = await ensurePlayContext();
    if (playbackMuted || !speaking) return;
    var int16 = new Int16Array(arrayBuffer);
    if (!int16.length) return;

    var float32 = new Float32Array(int16.length);
    for (var i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7fff);
    }

    var buffer = ctx.createBuffer(1, float32.length, DOWNLINK_RATE);
    buffer.copyToChannel(float32, 0);
    var src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    src.onended = function () {
      var idx = activeSources.indexOf(src);
      if (idx >= 0) activeSources.splice(idx, 1);
    };

    var now = ctx.currentTime;
    if (nextPlayTime < now) nextPlayTime = now;
    try {
      src.start(nextPlayTime);
      nextPlayTime += buffer.duration;
      activeSources.push(src);
    } catch (e) {
      log("播放失败: " + e.message);
    }
  }

  async function startMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("浏览器不支持麦克风 API");
    }
    if (!window.isSecureContext) {
      throw new Error(
        "非安全页面无法使用麦克风。请用 SSH 隧道访问 http://localhost:8002/english-test/，或为站点配置 HTTPS"
      );
    }

    captureStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });

    captureCtx = new (window.AudioContext || window.webkitAudioContext)();
    captureSrcRate = captureCtx.sampleRate;
    captureSource = captureCtx.createMediaStreamSource(captureStream);
    captureGain = captureCtx.createGain();
    captureGain.gain.value = 0;

    function onCaptureFloat32(float32) {
      if (!recording || speaking) return;
      var resampled = resampleFloat32(float32, captureSrcRate, UPLINK_RATE);
      flushUplink(floatToInt16(resampled), false);
    }

    var useWorklet = captureCtx.audioWorklet
      && typeof captureCtx.audioWorklet.addModule === "function";

    if (useWorklet) {
      var blob = new Blob([WORKLET_CODE], { type: "application/javascript" });
      var workletUrl = URL.createObjectURL(blob);
      try {
        await captureCtx.audioWorklet.addModule(workletUrl);
        captureWorklet = new AudioWorkletNode(captureCtx, "capture-processor");
        captureWorklet.port.onmessage = function (ev) {
          onCaptureFloat32(ev.data);
        };
        captureSource.connect(captureWorklet);
        captureWorklet.connect(captureGain);
        log("麦克风已开启 (AudioWorklet, " + captureSrcRate + "Hz → 16kHz)");
      } finally {
        URL.revokeObjectURL(workletUrl);
      }
    } else {
      captureProcessor = captureCtx.createScriptProcessor(4096, 1, 1);
      captureProcessor.onaudioprocess = function (ev) {
        onCaptureFloat32(ev.inputBuffer.getChannelData(0));
      };
      captureSource.connect(captureProcessor);
      captureProcessor.connect(captureGain);
      log("麦克风已开启 (兼容模式 ScriptProcessor, " + captureSrcRate + "Hz → 16kHz)");
    }

    captureGain.connect(captureCtx.destination);

    if (captureCtx.state === "suspended") {
      await captureCtx.resume();
    }

    pcmCarry = new Int16Array(0);
    uplinkFrames = 0;
  }

  function stopMic() {
    if (captureProcessor) {
      captureProcessor.onaudioprocess = null;
      captureProcessor.disconnect();
      captureProcessor = null;
    }
    if (captureWorklet) {
      captureWorklet.port.onmessage = null;
      captureWorklet.disconnect();
      captureWorklet = null;
    }
    if (captureGain) {
      captureGain.disconnect();
      captureGain = null;
    }
    if (captureSource) {
      captureSource.disconnect();
      captureSource = null;
    }
    if (captureStream) {
      captureStream.getTracks().forEach(function (t) { t.stop(); });
      captureStream = null;
    }
    if (captureCtx) {
      captureCtx.close().catch(function () {});
      captureCtx = null;
    }
  }

  function handleJson(obj) {
    log("← " + JSON.stringify(obj));
    var type = obj.type;

    if (type === "hello") {
      sessionId = obj.session_id;
      el.sessionText.textContent = sessionId || "—";
      setState("已握手，可以说话");
      el.btnTalk.disabled = false;
      el.hintText.classList.add("hidden");
      return;
    }

    if (type === "stt" && obj.text) {
      userTurnText = obj.text;
      el.userText.textContent = userTurnText;
      el.userBubble.classList.remove("hidden");
      var label = obj.partial ? "识别中: " : "识别: ";
      setState(label + userTurnText);
      return;
    }

    if (type === "correction") {
      var zh = obj.zh_explain || "";
      var en = obj.correct_en || "";
      var kind = obj.error_type || "mixed";
      var lines = [];
      if (kind && kind !== "none") lines.push("类型: " + kind);
      if (zh) lines.push(zh);
      if (en) lines.push("正确: " + en);
      if (lines.length && el.correctionText && el.correctionBubble) {
        el.correctionText.textContent = lines.join("\n");
        el.correctionBubble.classList.remove("hidden");
        log("纠错要点: " + lines.join(" | "));
      }
      return;
    }

    if (type === "tts") {
      if (obj.state === "start") {
        speaking = true;
        playbackMuted = false;
        stopLocalPlayback();
        playbackMuted = false;
        nextPlayTime = 0;
        setConn(true, true);
        setState("导师正在说话…");
        el.btnTalk.disabled = true;
        el.btnStop.classList.remove("hidden");
        el.btnStop.disabled = false;
        el.tutorBubble.classList.remove("hidden");
      } else if ((obj.state === "delta" || obj.state === "sentence_start") && obj.text) {
        if (playbackMuted) return;
        el.tutorText.textContent = obj.text;
        el.tutorBubble.classList.remove("hidden");
        setState("导师回复中…");
      } else if (obj.state === "stop") {
        speaking = false;
        setConn(true, false);
        setState(playbackMuted ? "已中断播放，可再次按住说话" : "回复完成，可再次按住说话");
        el.btnTalk.disabled = !connected;
        el.btnStop.classList.add("hidden");
      }
      return;
    }

    if (type === "alert") {
      log("⚠ " + (obj.message || obj.status || "alert"));
      if (obj.status === "Too Short") {
        setState(obj.message || "请按住按钮至少 1 秒再说话");
        el.btnTalk.disabled = false;
        setConn(connected, false);
        return;
      }
      setState("服务端提示: " + (obj.message || obj.status));
      return;
    }

    if (type === "goodbye") {
      setState("本轮结束，可再次按住说话");
      recording = false;
      speaking = false;
      stopMic();
      el.btnTalk.classList.remove("recording");
      el.btnTalk.disabled = !connected;
      setConn(connected, false);
    }
  }

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    setState("连接中…");
    el.btnConnect.disabled = true;
    log("连接 " + WS_URL);

    ws = new WebSocket(WS_URL);
    ws.binaryType = "arraybuffer";

    ws.onopen = function () {
      connected = true;
      setConn(true, false);
      setState("已连接，握手中…");
      el.btnConnect.textContent = "已连接";
      ws.send(JSON.stringify({
        type: "hello",
        version: 1,
        client: "web",
        transport: "websocket",
        audio_params: {
          format: "pcm",
          sample_rate: UPLINK_RATE,
          channels: 1,
          frame_duration: FRAME_MS,
        },
      }));
      log("→ hello (web pcm)");
    };

    ws.onmessage = function (ev) {
      if (ev.data instanceof ArrayBuffer) {
        playDownlinkPcm(ev.data);
        return;
      }
      try {
        handleJson(JSON.parse(ev.data));
      } catch (e) {
        log("JSON 解析失败: " + ev.data);
      }
    };

    ws.onerror = function () {
      log("WebSocket 错误");
      setState("连接失败");
    };

    ws.onclose = function () {
      connected = false;
      sessionId = null;
      recording = false;
      speaking = false;
      stopMic();
      setConn(false, false);
      setState("连接已断开");
      el.btnConnect.disabled = false;
      el.btnConnect.textContent = "连接服务器";
      el.btnTalk.disabled = true;
      el.btnTalk.classList.remove("recording");
      el.sessionText.textContent = "—";
      ws = null;
      log("连接关闭");
    };
  }

  async function startTalk() {
    if (!connected || recording || speaking) return;

    el.btnTalk.disabled = true;
    pendingFrames = [];
    uplinkFrames = 0;
    uplinkLive = false;
    userTurnText = "";
    el.userText.textContent = "";
    el.userBubble.classList.add("hidden");
    el.tutorText.textContent = "";
    if (el.correctionText) el.correctionText.textContent = "";
    if (el.correctionBubble) el.correctionBubble.classList.add("hidden");

    try {
      await startMic();
      recording = true;
      el.btnTalk.classList.add("recording");
      setConn(true, true);
      setState("正在连接 Omni…");

      sendJson({ type: "listen", state: "start", mode: "auto" });
      await sleep(OMNI_READY_MS);
      flushPendingFrames();

      el.btnTalk.disabled = false;
      setState("正在聆听…请说英语（按住至少 1 秒再松手）");
    } catch (err) {
      log("麦克风失败: " + err.message);
      setState("无法访问麦克风，请检查浏览器权限");
      stopMic();
      recording = false;
      uplinkLive = false;
      el.btnTalk.disabled = !connected;
      sendJson({ type: "listen", state: "stop" });
    }
  }

  function stopTalk() {
    if (!recording) return;

    var held = talkStartTime ? (Date.now() - talkStartTime) : 0;
    flushRemainder(true);
    recording = false;
    uplinkLive = false;
    el.btnTalk.classList.remove("recording");
    el.btnTalk.disabled = true;
    stopMic();

    if (uplinkFrames < MIN_UPLINK_FRAMES || held < MIN_HOLD_MS) {
      log("说话太短：" + uplinkFrames + " 帧 / " + held + "ms，请按住至少 1 秒");
      setState("说话太短，请按住按钮至少 1 秒再松手");
      sendJson({ type: "listen", state: "stop" });
      el.btnTalk.disabled = false;
      setConn(connected, false);
      return;
    }

    log("已上传 " + uplinkFrames + " 帧音频（" + held + "ms），提交识别…");
    setState("已松手，等待识别与回复…");
    sendJson({ type: "listen", state: "stop" });
    setConn(true, true);
  }

  function isTypingTarget(target) {
    if (!target) return false;
    var tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable;
  }

  function isSpaceKey(e) {
    return e.code === "Space";
  }

  function hasPttModifier(e) {
    return e.altKey || e.ctrlKey || e.metaKey;
  }

  function onSpaceDown(e) {
    if (!isSpaceKey(e)) return;
    if (e.repeat) return;
    if (hasPttModifier(e)) return;
    if (isTypingTarget(e.target)) return;
    e.preventDefault();
    // 鼠标已在按住说话时，忽略空格，避免误触结束
    if (recording && !spacePttActive) return;
    if (!spacePttActive) {
      spacePttActive = true;
      startTalk();
    }
  }

  function onSpaceUp(e) {
    if (!isSpaceKey(e)) return;
    if (isTypingTarget(e.target)) return;
    e.preventDefault();
    if (spacePttActive) {
      spacePttActive = false;
      stopTalk();
    }
  }

  function abortPlayback() {
    if (!connected && !speaking) return;
    log("→ abort（停止播放）");
    stopLocalPlayback();
    speaking = false;
    sendJson({ type: "abort", reason: "user" });
    el.btnStop.classList.add("hidden");
    el.btnTalk.disabled = !connected;
    setState("已中断播放");
    setConn(connected, false);
  }

  el.btnConnect.addEventListener("click", connect);
  el.btnStop.addEventListener("click", abortPlayback);

  el.btnTalk.addEventListener("mousedown", function (e) {
    if (hasPttModifier(e)) return;
    e.preventDefault();
    startTalk();
  });
  el.btnTalk.addEventListener("mouseup", stopTalk);
  el.btnTalk.addEventListener("mouseleave", function () {
    if (recording) stopTalk();
  });

  el.btnTalk.addEventListener("touchstart", function (e) {
    e.preventDefault();
    startTalk();
  }, { passive: false });
  el.btnTalk.addEventListener("touchend", function (e) {
    e.preventDefault();
    stopTalk();
  }, { passive: false });

  document.addEventListener("keydown", onSpaceDown);
  document.addEventListener("keyup", onSpaceUp);
  window.addEventListener("blur", function () {
    if (spacePttActive) {
      spacePttActive = false;
      stopTalk();
    }
  });

  log("页面就绪。请先点击「连接服务器」，再按住按钮或空格键说话。");
})();
