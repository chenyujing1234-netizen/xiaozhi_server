/**
 * SpeakPal 英语练习 Web 客户端（共享逻辑）
 * 测试页与正式页共用：WebSocket + PCM 上下行 + 按住说话
 */
(function (global) {
  "use strict";

  var UPLINK_RATE = 16000;
  var DOWNLINK_RATE = 24000;
  var FRAME_MS = 60;
  var FRAME_SAMPLES = (UPLINK_RATE * FRAME_MS) / 1000;
  var OMNI_READY_MS = 800;
  var MIN_HOLD_MS = 900;
  var MIN_UPLINK_FRAMES = 5;

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

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
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

  function getOrCreateDeviceId(storageKey) {
    var key = storageKey || "speakpal_device_id";
    try {
      var id = localStorage.getItem(key);
      if (id) return id;
      id = "web-" + Math.random().toString(36).slice(2, 10);
      localStorage.setItem(key, id);
      return id;
    } catch (e) {
      return "web-anon";
    }
  }

  function buildDefaultWsUrl(deviceId) {
    var params = new URLSearchParams(global.location.search);
    if (params.get("ws_url")) return params.get("ws_url");

    var path = params.get("ws_path") || "/xiaozhi/english/v1/";
    var q = "device_id=" + encodeURIComponent(deviceId);
    if (params.get("client_id")) q += "&client_id=" + encodeURIComponent(params.get("client_id"));

    var proto = global.location.protocol === "https:" ? "wss:" : "ws:";
    var hostname = params.get("ws_host") || global.location.hostname;

    if (params.get("ws_port")) {
      return proto + "//" + hostname + ":" + params.get("ws_port") + path + "?" + q;
    }

    var pagePort = global.location.port;
    var standardTls = global.location.protocol === "https:" && (!pagePort || pagePort === "443");
    var standardHttp = global.location.protocol === "http:" && (!pagePort || pagePort === "80");

    if (standardTls || standardHttp) {
      return proto + "//" + hostname + path + "?" + q;
    }

    // 自定义 HTTPS 端口（如 :8443）：WebSocket 走同一端口，避免防火墙拦截第二端口
    if (global.location.protocol === "https:" && pagePort) {
      return proto + "//" + hostname + ":" + pagePort + path + "?" + q;
    }

    var wsPort = global.location.protocol === "https:" ? "8444" : "8000";
    return proto + "//" + hostname + ":" + wsPort + path + "?" + q;
  }

  function SpeakPalClient(options) {
    options = options || {};
    this.callbacks = options.callbacks || {};
    this.debug = !!options.debug;
    this.autoReconnect = options.autoReconnect !== false;
    this.reconnectDelayMs = options.reconnectDelayMs || 2500;
    this.deviceId = options.deviceId || getOrCreateDeviceId(options.storageKey);
    this.wsUrl = options.wsUrl || buildDefaultWsUrl(this.deviceId);

    this.ws = null;
    this.sessionId = null;
    this.connected = false;
    this.recording = false;
    this.speaking = false;
    this.uplinkLive = false;
    this.uplinkFrames = 0;
    this.pendingFrames = [];
    this.talkStartTime = 0;
    this.userTurnText = "";
    this._reconnectTimer = null;
    this._destroyed = false;

    this.captureCtx = null;
    this.captureStream = null;
    this.captureSource = null;
    this.captureWorklet = null;
    this.captureProcessor = null;
    this.captureGain = null;
    this.pcmCarry = new Int16Array(0);
    this.captureSrcRate = UPLINK_RATE;

    this.playCtx = null;
    this.nextPlayTime = 0;
    this.activeSources = [];
    this.playbackMuted = false;
  }

  SpeakPalClient.prototype._emit = function (name, payload) {
    var fn = this.callbacks[name];
    if (typeof fn === "function") fn(payload);
    if (this.debug) {
      var logFn = this.callbacks.log;
      if (typeof logFn === "function") logFn("[core:" + name + "] " + JSON.stringify(payload || {}));
    }
  };

  SpeakPalClient.prototype._sendJson = function (obj) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.sessionId) obj.session_id = this.sessionId;
    this.ws.send(JSON.stringify(obj));
    this._emit("send", obj);
  };

  SpeakPalClient.prototype._sendPcmFrame = function (frame) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    var copy = frame.length === FRAME_SAMPLES ? frame : new Int16Array(frame);
    this.ws.send(copy.buffer.slice(copy.byteOffset, copy.byteOffset + copy.byteLength));
    this.uplinkFrames += 1;
  };

  SpeakPalClient.prototype._flushUplink = function (int16, force) {
    this.pcmCarry = concatInt16(this.pcmCarry, int16);
    while (this.pcmCarry.length >= FRAME_SAMPLES) {
      var frame = new Int16Array(this.pcmCarry.subarray(0, FRAME_SAMPLES));
      this.pcmCarry = this.pcmCarry.subarray(FRAME_SAMPLES);
      if (this.uplinkLive && ((this.recording && !this.speaking) || force)) {
        this._sendPcmFrame(frame);
      } else if (this.recording && !this.speaking) {
        this.pendingFrames.push(frame);
      }
    }
  };

  SpeakPalClient.prototype._flushPendingFrames = function () {
    this.uplinkLive = true;
    this.talkStartTime = Date.now();
    for (var i = 0; i < this.pendingFrames.length; i++) {
      this._sendPcmFrame(this.pendingFrames[i]);
    }
    this.pendingFrames = [];
    this._emit("uplinkReady", { frames: this.uplinkFrames });
  };

  SpeakPalClient.prototype._flushRemainder = function (force) {
    if (this.pcmCarry.length > 0 && (force || (this.recording && !this.speaking))) {
      var padded = new Int16Array(FRAME_SAMPLES);
      padded.set(this.pcmCarry);
      if (this.uplinkLive || force) {
        this._sendPcmFrame(padded);
      } else {
        this.pendingFrames.push(padded);
      }
      this.pcmCarry = new Int16Array(0);
    }
  };

  SpeakPalClient.prototype._ensurePlayContext = async function () {
    if (!this.playCtx) {
      this.playCtx = new (global.AudioContext || global.webkitAudioContext)({ sampleRate: DOWNLINK_RATE });
    }
    if (this.playCtx.state === "suspended") await this.playCtx.resume();
    return this.playCtx;
  };

  SpeakPalClient.prototype.stopLocalPlayback = function () {
    this.playbackMuted = true;
    for (var i = 0; i < this.activeSources.length; i++) {
      try { this.activeSources[i].stop(0); } catch (e) { /* noop */ }
      try { this.activeSources[i].disconnect(); } catch (e2) { /* noop */ }
    }
    this.activeSources = [];
    this.nextPlayTime = 0;
  };

  SpeakPalClient.prototype._playDownlinkPcm = async function (arrayBuffer) {
    if (this.playbackMuted || !this.speaking) return;
    var ctx = await this._ensurePlayContext();
    if (this.playbackMuted || !this.speaking) return;
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
    var self = this;
    src.onended = function () {
      var idx = self.activeSources.indexOf(src);
      if (idx >= 0) self.activeSources.splice(idx, 1);
    };
    var now = ctx.currentTime;
    if (this.nextPlayTime < now) this.nextPlayTime = now;
    try {
      src.start(this.nextPlayTime);
      this.nextPlayTime += buffer.duration;
      this.activeSources.push(src);
    } catch (e) {
      this._emit("error", { message: "播放失败: " + e.message });
    }
  };

  SpeakPalClient.prototype._stopMic = function () {
    if (this.captureProcessor) {
      this.captureProcessor.onaudioprocess = null;
      this.captureProcessor.disconnect();
      this.captureProcessor = null;
    }
    if (this.captureWorklet) {
      this.captureWorklet.port.onmessage = null;
      this.captureWorklet.disconnect();
      this.captureWorklet = null;
    }
    if (this.captureGain) { this.captureGain.disconnect(); this.captureGain = null; }
    if (this.captureSource) { this.captureSource.disconnect(); this.captureSource = null; }
    if (this.captureStream) {
      this.captureStream.getTracks().forEach(function (t) { t.stop(); });
      this.captureStream = null;
    }
    if (this.captureCtx) {
      this.captureCtx.close().catch(function () {});
      this.captureCtx = null;
    }
  };

  SpeakPalClient.prototype._startMic = async function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("当前浏览器不支持麦克风");
    }
    if (!global.isSecureContext) {
      throw new Error("请使用 HTTPS 访问本页，微信内也需安全连接才能录音");
    }
    this.captureStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    this.captureCtx = new (global.AudioContext || global.webkitAudioContext)();
    this.captureSrcRate = this.captureCtx.sampleRate;
    this.captureSource = this.captureCtx.createMediaStreamSource(this.captureStream);
    this.captureGain = this.captureCtx.createGain();
    this.captureGain.gain.value = 0;
    var self = this;
    function onCaptureFloat32(float32) {
      if (!self.recording || self.speaking) return;
      var resampled = resampleFloat32(float32, self.captureSrcRate, UPLINK_RATE);
      self._flushUplink(floatToInt16(resampled), false);
    }
    var useWorklet = this.captureCtx.audioWorklet
      && typeof this.captureCtx.audioWorklet.addModule === "function";
    if (useWorklet) {
      var blob = new Blob([WORKLET_CODE], { type: "application/javascript" });
      var workletUrl = URL.createObjectURL(blob);
      try {
        await this.captureCtx.audioWorklet.addModule(workletUrl);
        this.captureWorklet = new AudioWorkletNode(this.captureCtx, "capture-processor");
        this.captureWorklet.port.onmessage = function (ev) { onCaptureFloat32(ev.data); };
        this.captureSource.connect(this.captureWorklet);
        this.captureWorklet.connect(this.captureGain);
      } finally {
        URL.revokeObjectURL(workletUrl);
      }
    } else {
      this.captureProcessor = this.captureCtx.createScriptProcessor(4096, 1, 1);
      this.captureProcessor.onaudioprocess = function (ev) {
        onCaptureFloat32(ev.inputBuffer.getChannelData(0));
      };
      this.captureSource.connect(this.captureProcessor);
      this.captureProcessor.connect(this.captureGain);
    }
    this.captureGain.connect(this.captureCtx.destination);
    if (this.captureCtx.state === "suspended") await this.captureCtx.resume();
    this.pcmCarry = new Int16Array(0);
    this.uplinkFrames = 0;
  };

  SpeakPalClient.prototype._handleJson = function (obj) {
    this._emit("message", obj);
    var type = obj.type;
    if (type === "hello") {
      this.sessionId = obj.session_id;
      this._emit("hello", { sessionId: this.sessionId });
      return;
    }
    if (type === "stt" && obj.text) {
      this.userTurnText = obj.text;
      this._emit("stt", { text: obj.text, partial: !!obj.partial });
      return;
    }
    if (type === "correction") {
      this._emit("correction", obj);
      return;
    }
    if (type === "image_ack") {
      this._emit("imageAck", obj);
      return;
    }
    if (type === "tts") {
      if (obj.state === "start") {
        this.speaking = true;
        this.playbackMuted = false;
        this.stopLocalPlayback();
        this.playbackMuted = false;
        this.nextPlayTime = 0;
        this._emit("ttsStart", {});
      } else if ((obj.state === "delta" || obj.state === "sentence_start") && obj.text) {
        if (!this.playbackMuted) {
          this._emit("ttsText", { text: obj.text, partial: obj.state === "delta" });
        }
      } else if (obj.state === "stop") {
        this.speaking = false;
        this._emit("ttsStop", { aborted: this.playbackMuted });
      }
      return;
    }
    if (type === "alert") {
      this._emit("alert", obj);
      return;
    }
    if (type === "goodbye") {
      this.recording = false;
      this.speaking = false;
      this._stopMic();
      this._emit("goodbye", {});
    }
  };

  SpeakPalClient.prototype.connect = function () {
    var self = this;
    if (this._destroyed) return;
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    this._emit("connecting", { url: this.wsUrl });
    this.ws = new WebSocket(this.wsUrl);
    this.ws.binaryType = "arraybuffer";
    this.ws.onopen = function () {
      self.connected = true;
      self._emit("connected", {});
      self._sendJson({
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
      });
    };
    this.ws.onmessage = function (ev) {
      if (ev.data instanceof ArrayBuffer) {
        self._playDownlinkPcm(ev.data);
        return;
      }
      try {
        self._handleJson(JSON.parse(ev.data));
      } catch (e) {
        self._emit("error", { message: "JSON 解析失败" });
      }
    };
    this.ws.onerror = function () {
      self._emit("error", { message: "连接异常" });
    };
    this.ws.onclose = function () {
      self.connected = false;
      self.sessionId = null;
      self.recording = false;
      self.speaking = false;
      self._stopMic();
      self.ws = null;
      self._emit("disconnected", {});
      if (self.autoReconnect && !self._destroyed) {
        self._reconnectTimer = setTimeout(function () { self.connect(); }, self.reconnectDelayMs);
      }
    };
  };

  SpeakPalClient.prototype.startTalk = async function () {
    if (!this.connected || this.recording || this.speaking) return false;
    this.pendingFrames = [];
    this.uplinkFrames = 0;
    this.uplinkLive = false;
    this.userTurnText = "";
    this._emit("talkStart", {});
    try {
      await this._startMic();
      this.recording = true;
      this._sendJson({ type: "listen", state: "start", mode: "auto" });
      await sleep(OMNI_READY_MS);
      this._flushPendingFrames();
      this._emit("talkListening", {});
      return true;
    } catch (err) {
      this._stopMic();
      this.recording = false;
      this.uplinkLive = false;
      this._sendJson({ type: "listen", state: "stop" });
      this._emit("error", { message: err.message || String(err) });
      return false;
    }
  };

  SpeakPalClient.prototype.stopTalk = function () {
    if (!this.recording) return;
    var held = this.talkStartTime ? (Date.now() - this.talkStartTime) : 0;
    this._flushRemainder(true);
    this.recording = false;
    this.uplinkLive = false;
    this._stopMic();
    if (this.uplinkFrames < MIN_UPLINK_FRAMES || held < MIN_HOLD_MS) {
      this._sendJson({ type: "listen", state: "stop" });
      this._emit("talkTooShort", { frames: this.uplinkFrames, heldMs: held });
      return;
    }
    this._sendJson({ type: "listen", state: "stop" });
    this._emit("talkEnd", { frames: this.uplinkFrames, heldMs: held });
  };

  SpeakPalClient.prototype.abortPlayback = function () {
    if (!this.connected && !this.speaking) return;
    this.stopLocalPlayback();
    this.speaking = false;
    this._sendJson({ type: "abort", reason: "user" });
    this._emit("abort", {});
  };

  SpeakPalClient.prototype.sendImage = function (base64Jpeg) {
    if (!this.connected) {
      this._emit("error", { message: "尚未连接，无法上传图片" });
      return false;
    }
    if (!base64Jpeg) {
      this._emit("error", { message: "图片为空" });
      return false;
    }
    // 去掉 data URL 前缀，只传纯 base64
    var data = String(base64Jpeg);
    if (data.indexOf(",") >= 0) data = data.split(",")[1];
    this._sendJson({ type: "image", format: "jpeg", data: data });
    this._emit("imageSend", { bytesEstimate: Math.floor(data.length * 0.75) });
    return true;
  };

  SpeakPalClient.prototype.clearImage = function () {
    if (!this.connected) return;
    this._sendJson({ type: "image_clear" });
  };

  SpeakPalClient.prototype.destroy = function () {
    this._destroyed = true;
    this.autoReconnect = false;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    if (this.ws) this.ws.close();
    this._stopMic();
    if (this.playCtx) this.playCtx.close().catch(function () {});
  };

  SpeakPalClient.getDeviceId = getOrCreateDeviceId;
  SpeakPalClient.buildDefaultWsUrl = buildDefaultWsUrl;
  SpeakPalClient.UPLINK_RATE = UPLINK_RATE;
  SpeakPalClient.DOWNLINK_RATE = DOWNLINK_RATE;

  global.SpeakPalClient = SpeakPalClient;
})(window);
