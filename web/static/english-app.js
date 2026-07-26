(function () {
  "use strict";

  var client = null;
  var ready = false;
  var currentUserMsg = null;
  var currentTutorMsg = null;
  var toastTimer = null;

  var el = {
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    chatScroll: document.getElementById("chat-scroll"),
    messages: document.getElementById("messages"),
    welcome: document.getElementById("welcome-card"),
    btnTalk: document.getElementById("btn-talk"),
    btnTalkLabel: document.getElementById("btn-talk-label"),
    btnTalkHint: document.getElementById("btn-talk-hint"),
    overlay: document.getElementById("overlay"),
    overlayText: document.getElementById("overlay-text"),
    toast: document.getElementById("toast"),
  };

  function setStatus(kind, text) {
    el.statusDot.className = "status-dot " + (kind || "");
    el.statusText.textContent = text;
  }

  function showOverlay(text) {
    el.overlayText.textContent = text || "加载中…";
    el.overlay.classList.remove("hidden");
  }

  function hideOverlay() {
    el.overlay.classList.add("hidden");
  }

  function showToast(text, ms) {
    el.toast.textContent = text;
    el.toast.classList.remove("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.toast.classList.add("hidden");
    }, ms || 2600);
  }

  function scrollToBottom() {
    requestAnimationFrame(function () {
      el.chatScroll.scrollTop = el.chatScroll.scrollHeight;
    });
  }

  function hideWelcome() {
    if (el.welcome) el.welcome.style.display = "none";
  }

  function ensureMsg(role, currentRef) {
    if (currentRef && currentRef.el && currentRef.el.isConnected) return currentRef;
    hideWelcome();
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    var tag = document.createElement("span");
    tag.className = "msg-tag";
    tag.textContent = role === "user" ? "你说" : role === "tutor" ? "导师" : "纠错";
    var text = document.createElement("div");
    text.className = "msg-text";
    bubble.appendChild(tag);
    bubble.appendChild(text);
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
    return { el: wrap, bubble: bubble, text: text, partial: false };
  }

  function setMsgContent(ref, content, partial) {
    if (!ref) return;
    ref.text.textContent = content || "";
    ref.partial = !!partial;
    ref.el.classList.toggle("partial", !!partial);
    scrollToBottom();
  }

  function addCorrection(data) {
    var wrap = document.createElement("div");
    wrap.className = "msg correction";
    var bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    var lines = [];
    if (data.zh_explain) lines.push(data.zh_explain);
    if (data.correct_en) lines.push("正确：" + data.correct_en);
    bubble.innerHTML = '<span class="msg-tag">纠错要点</span><div class="msg-text">' +
      escapeHtml(lines.join("\n")) + "</div>";
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setTalkUi(state) {
    // idle | recording | speaking | disabled
    el.btnTalk.classList.toggle("recording", state === "recording");
    el.btnTalk.classList.toggle("speaking", state === "speaking");
    if (state === "recording") {
      el.btnTalkLabel.textContent = "正在聆听…";
      el.btnTalkHint.textContent = "松开结束";
    } else if (state === "speaking") {
      el.btnTalkLabel.textContent = "按住停止";
      el.btnTalkHint.textContent = "打断导师回复";
    } else if (ready) {
      el.btnTalkLabel.textContent = "按住说话";
      el.btnTalkHint.textContent = "松开结束";
    } else {
      el.btnTalkLabel.textContent = "连接中…";
      el.btnTalkHint.textContent = "";
    }
    el.btnTalk.disabled = !ready || state === "disabled";
  }

  function bindTalkButton() {
    var btn = el.btnTalk;

    function onPress(e) {
      if (e.type === "mousedown" && e.button !== 0) return;
      e.preventDefault();
      if (!ready) return;
      if (client.speaking) {
        client.abortPlayback();
        return;
      }
      if (client.recording) return;
      client.startTalk();
    }

    function onRelease(e) {
      e.preventDefault();
      if (client.recording) client.stopTalk();
    }

    btn.addEventListener("mousedown", onPress);
    btn.addEventListener("mouseup", onRelease);
    btn.addEventListener("mouseleave", function () {
      if (client && client.recording) client.stopTalk();
    });
    btn.addEventListener("touchstart", onPress, { passive: false });
    btn.addEventListener("touchend", onRelease, { passive: false });
    btn.addEventListener("touchcancel", onRelease, { passive: false });
  }

  function initClient() {
    client = new SpeakPalClient({
      autoReconnect: true,
      callbacks: {
        connecting: function () {
          ready = false;
          setStatus("", "连接中…");
          setTalkUi("disabled");
          showOverlay("正在连接 SpeakPal…");
        },
        connected: function () {
          setStatus("", "握手中…");
          showOverlay("正在准备对话…");
        },
        hello: function () {
          ready = true;
          hideOverlay();
          setStatus("ready", "就绪");
          setTalkUi("idle");
          showToast("已连接，按住下方按钮开始说英语", 2200);
        },
        disconnected: function () {
          ready = false;
          currentUserMsg = null;
          currentTutorMsg = null;
          setStatus("error", "已断开，重连中…");
          setTalkUi("disabled");
          showOverlay("连接断开，正在重连…");
        },
        error: function (payload) {
          if (payload && payload.message) {
            showToast(payload.message, 3200);
            if (!ready) {
              showOverlay("连接失败：" + payload.message);
            }
          }
        },
        talkStart: function () {
          currentUserMsg = null;
          currentTutorMsg = null;
          setTalkUi("recording");
          setStatus("busy", "聆听中");
        },
        talkListening: function () {
          setTalkUi("recording");
        },
        talkTooShort: function () {
          setTalkUi("idle");
          setStatus("ready", "就绪");
          showToast("说话太短，请按住至少约 1 秒", 2600);
        },
        talkEnd: function () {
          setTalkUi("idle");
          setStatus("busy", "等待回复…");
        },
        stt: function (payload) {
          currentUserMsg = ensureMsg("user", currentUserMsg);
          setMsgContent(currentUserMsg, payload.text, payload.partial);
          if (!payload.partial) {
            currentUserMsg = null;
          }
        },
        ttsStart: function () {
          currentTutorMsg = ensureMsg("tutor", null);
          setTalkUi("speaking");
          setStatus("busy", "导师说话中");
        },
        ttsText: function (payload) {
          currentTutorMsg = ensureMsg("tutor", currentTutorMsg);
          setMsgContent(currentTutorMsg, payload.text, payload.partial);
        },
        ttsStop: function () {
          currentTutorMsg = null;
          setTalkUi("idle");
          setStatus("ready", "就绪");
        },
        correction: function (data) {
          addCorrection(data);
        },
        alert: function (obj) {
          if (obj.status === "Too Short") {
            showToast(obj.message || "请按住至少约 1 秒", 2600);
            setTalkUi("idle");
            setStatus("ready", "就绪");
            return;
          }
          showToast(obj.message || obj.status || "提示", 3000);
        },
        goodbye: function () {
          setTalkUi("idle");
          setStatus("ready", "就绪");
        },
        abort: function () {
          currentTutorMsg = null;
          setTalkUi("idle");
          setStatus("ready", "就绪");
          showToast("已停止播放", 1800);
        },
      },
    });

    bindTalkButton();
    client.connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initClient);
  } else {
    initClient();
  }
})();
