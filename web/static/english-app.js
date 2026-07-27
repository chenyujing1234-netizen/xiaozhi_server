(function () {
  "use strict";

  var client = null;
  var ready = false;
  var currentUserMsg = null;
  var currentTutorMsg = null;
  var toastTimer = null;
  var connectTimeoutTimer = null;
  var ensureSessionPromise = null;
  var readyWaiters = [];
  var controlsBound = false;

  var photoObjectUrl = null;
  var photoUploading = false;

  var el = {
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    chatScroll: document.getElementById("chat-scroll"),
    messages: document.getElementById("messages"),
    welcome: document.getElementById("welcome-card"),
    btnTalk: document.getElementById("btn-talk"),
    btnTalkLabel: document.getElementById("btn-talk-label"),
    btnTalkHint: document.getElementById("btn-talk-hint"),
    btnPhoto: document.getElementById("btn-photo"),
    photoInput: document.getElementById("photo-input"),
    photoBar: document.getElementById("photo-bar"),
    photoThumb: document.getElementById("photo-thumb"),
    btnPhotoClear: document.getElementById("btn-photo-clear"),
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
    // idle | recording | speaking | disabled | guest
    var loggedIn = !!(window.SpeakPalAuth && SpeakPalAuth.isLoggedIn());
    var guest = !loggedIn;
    var canUse = ready || guest;
    el.btnTalk.classList.toggle("recording", state === "recording");
    el.btnTalk.classList.toggle("speaking", state === "speaking");
    if (state === "recording") {
      el.btnTalkLabel.textContent = "正在聆听…";
      el.btnTalkHint.textContent = "松开结束";
    } else if (state === "speaking") {
      el.btnTalkLabel.textContent = "按住停止";
      el.btnTalkHint.textContent = "打断导师回复";
    } else if (guest) {
      el.btnTalkLabel.textContent = "按住说话";
      el.btnTalkHint.textContent = "开始练习需登录";
    } else if (ready) {
      el.btnTalkLabel.textContent = "按住说话";
      el.btnTalkHint.textContent = "松开结束";
    } else {
      el.btnTalkLabel.textContent = "连接中…";
      el.btnTalkHint.textContent = "";
    }
    el.btnTalk.disabled = !canUse || state === "disabled" || photoUploading;
    if (el.btnPhoto) {
      el.btnPhoto.disabled = !canUse || state === "recording" || state === "disabled" || photoUploading;
    }
  }

  function enterGuestMode() {
    ready = false;
    hideOverlay();
    setStatus("", "未登录");
    setTalkUi("guest");
  }

  function notifyReadyWaiters() {
    var waiters = readyWaiters.slice();
    readyWaiters = [];
    waiters.forEach(function (resolve) {
      try { resolve(true); } catch (e) { /* noop */ }
    });
  }

  function waitUntilReady(timeoutMs) {
    if (ready) return Promise.resolve(true);
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () {
        if (done) return;
        done = true;
        resolve(false);
      }, timeoutMs || 20000);
      readyWaiters.push(function (ok) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(!!ok);
      });
    });
  }

  function authCallbacks() {
    return {
      toast: function (msg) { showToast(msg, 2600); },
      onLoggedIn: function (payload) {
        if (payload && payload.toast) showToast(payload.toast, 2200);
        updateAccountButton();
      },
    };
  }

  async function ensureSession() {
    if (ensureSessionPromise) return ensureSessionPromise;
    ensureSessionPromise = (async function () {
      if (!window.SpeakPalAuth) {
        showToast("登录模块加载失败，请刷新重试", 3000);
        return false;
      }
      if (!SpeakPalAuth.isLoggedIn()) {
        var loginResult = await SpeakPalAuth.requireLogin(authCallbacks());
        updateAccountButton();
        if (!loginResult || !loginResult.ok) {
          enterGuestMode();
          return false;
        }
      }
      if (!client) {
        showOverlay("正在连接 SpeakPal…");
        setStatus("", "连接中…");
        setTalkUi("disabled");
        initClient(SpeakPalAuth.getDeviceId());
      }
      if (!ready) {
        showOverlay("正在连接 SpeakPal…");
        var ok = await waitUntilReady(20000);
        if (!ok) {
          showToast("连接超时，请稍后重试", 3000);
          return false;
        }
      }
      return true;
    })().finally(function () {
      ensureSessionPromise = null;
    });
    return ensureSessionPromise;
  }

  function addPhotoMsg(dataUrl) {
    hideWelcome();
    var wrap = document.createElement("div");
    wrap.className = "msg photo";
    var bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.innerHTML = '<span class="msg-tag">我的照片</span>';
    var img = document.createElement("img");
    img.src = dataUrl;
    img.alt = "看图练英语";
    bubble.appendChild(img);
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollToBottom();
  }

  function showPhotoBar(dataUrl) {
    if (photoObjectUrl) {
      try { URL.revokeObjectURL(photoObjectUrl); } catch (e) { /* noop */ }
      photoObjectUrl = null;
    }
    el.photoThumb.src = dataUrl;
    el.photoBar.classList.remove("hidden");
  }

  function hidePhotoBar() {
    el.photoBar.classList.add("hidden");
    el.photoThumb.removeAttribute("src");
    if (photoObjectUrl) {
      try { URL.revokeObjectURL(photoObjectUrl); } catch (e) { /* noop */ }
      photoObjectUrl = null;
    }
  }

  function compressImageFile(file) {
    return new Promise(function (resolve, reject) {
      if (!file) {
        reject(new Error("未选择文件"));
        return;
      }
      var reader = new FileReader();
      reader.onerror = function () { reject(new Error("读取图片失败")); };
      reader.onload = function () {
        var img = new Image();
        img.onerror = function () { reject(new Error("图片无法解析")); };
        img.onload = function () {
          var maxSide = 960;
          var w = img.width;
          var h = img.height;
          if (w > maxSide || h > maxSide) {
            if (w >= h) {
              h = Math.round(h * maxSide / w);
              w = maxSide;
            } else {
              w = Math.round(w * maxSide / h);
              h = maxSide;
            }
          }
          var canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          var ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, w, h);

          var qualities = [0.82, 0.72, 0.62, 0.52, 0.42];
          var out = null;
          for (var i = 0; i < qualities.length; i++) {
            out = canvas.toDataURL("image/jpeg", qualities[i]);
            var b64 = out.split(",")[1] || "";
            // Omni 限制 Base64 后约 256KB，目标压到 180KB 以内
            if (b64.length <= 180 * 1024) break;
          }
          var finalB64 = (out && out.split(",")[1]) || "";
          if (!finalB64 || finalB64.length > 240 * 1024) {
            reject(new Error("图片仍过大，请换一张更小的照片"));
            return;
          }
          resolve(out);
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  function bindControls() {
    if (controlsBound) return;
    controlsBound = true;

    if (el.btnPhoto && el.photoInput) {
      el.btnPhoto.addEventListener("click", function () {
        if (photoUploading) return;
        ensureSession().then(function (ok) {
          if (!ok || !ready) return;
          el.photoInput.value = "";
          el.photoInput.click();
        });
      });

      el.photoInput.addEventListener("change", function () {
        var file = el.photoInput.files && el.photoInput.files[0];
        if (!file) return;
        photoUploading = true;
        setTalkUi("idle");
        showToast("正在压缩图片…", 2000);
        compressImageFile(file)
          .then(function (dataUrl) {
            addPhotoMsg(dataUrl);
            showPhotoBar(dataUrl);
            if (client) client.sendImage(dataUrl);
          })
          .catch(function (err) {
            showToast(err.message || "处理图片失败", 3200);
          })
          .finally(function () {
            photoUploading = false;
            setTalkUi(client && client.speaking ? "speaking" : "idle");
          });
      });
    }

    if (el.btnPhotoClear) {
      el.btnPhotoClear.addEventListener("click", function () {
        hidePhotoBar();
        if (client) client.clearImage();
        showToast("已清除图片", 1800);
      });
    }

    var btn = el.btnTalk;
    if (!btn) return;

    function onPress(e) {
      if (e.type === "mousedown" && e.button !== 0) return;
      e.preventDefault();
      if (!window.SpeakPalAuth || !SpeakPalAuth.isLoggedIn() || !ready || !client) {
        ensureSession().then(function (ok) {
          if (ok) showToast("已就绪，请再次按住说话", 2200);
        });
        return;
      }
      if (client.speaking) {
        client.abortPlayback();
        return;
      }
      if (client.recording) return;
      client.startTalk();
    }

    function onRelease(e) {
      e.preventDefault();
      if (client && client.recording) client.stopTalk();
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

  function clearConnectTimeout() {
    if (connectTimeoutTimer) {
      clearTimeout(connectTimeoutTimer);
      connectTimeoutTimer = null;
    }
  }

  function armConnectTimeout() {
    clearConnectTimeout();
    connectTimeoutTimer = setTimeout(function () {
      if (!ready) {
        showOverlay("连接超时，请检查网络后刷新页面重试");
        setStatus("error", "连接超时");
        showToast("若页面能打开但无法连接，可能是服务重启中，请稍后再试", 4000);
      }
    }, 15000);
  }

  function updateAccountButton() {
    var btn = document.getElementById("btn-account");
    if (!btn || !window.SpeakPalAuth) return;
    var user = SpeakPalAuth.getUser();
    btn.classList.remove("hidden");
    if (!user || !SpeakPalAuth.isLoggedIn()) {
      btn.textContent = "登录";
      btn.onclick = function () {
        SpeakPalAuth.requireLogin(authCallbacks()).then(function (result) {
          updateAccountButton();
          if (result && result.ok) {
            ensureSession();
          }
        });
      };
      return;
    }
    if (SpeakPalAuth.isWeChat) {
      btn.textContent = "已登录";
      btn.onclick = null;
      return;
    }
    var label = user.phone ? String(user.phone).replace(/^(\d{3})\d{4}(\d{4})$/, "$1****$2") : "账号";
    btn.textContent = label;
    btn.onclick = function () {
      if (confirm("确定退出登录吗？")) SpeakPalAuth.logout();
    };
  }

  function initClient(deviceId) {
    client = new SpeakPalClient({
      deviceId: deviceId,
      autoReconnect: true,
      callbacks: {
        connecting: function () {
          ready = false;
          setStatus("", "连接中…");
          setTalkUi("disabled");
          showOverlay("正在连接 SpeakPal…");
          armConnectTimeout();
        },
        connected: function () {
          setStatus("", "握手中…");
          showOverlay("正在准备对话…");
        },
        hello: function () {
          clearConnectTimeout();
          ready = true;
          hideOverlay();
          setStatus("ready", "就绪");
          setTalkUi("idle");
          notifyReadyWaiters();
          showToast("已连接：可拍图或按住说话练英语", 2400);
        },
        disconnected: function () {
          ready = false;
          currentUserMsg = null;
          currentTutorMsg = null;
          setStatus("error", "已断开，重连中…");
          setTalkUi("disabled");
          showOverlay("连接断开，正在重连…");
        },
        imageAck: function (obj) {
          if (obj && obj.cleared) return;
          if (obj && obj.ok) {
            showToast(obj.message || "图片已添加，按住说话开始看图练英语", 2800);
            setStatus("ready", "看图就绪");
          } else {
            hidePhotoBar();
            showToast((obj && obj.message) || "图片上传失败", 3200);
          }
        },
        imageSend: function () {
          setStatus("busy", "上传图片…");
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

    client.connect();
  }

  async function boot() {
    hideOverlay();
    bindControls();
    if (!window.SpeakPalAuth) {
      showOverlay("登录模块加载失败，请刷新重试");
      return;
    }
    var result = await SpeakPalAuth.initAuth(authCallbacks());
    updateAccountButton();
    if (result && result.ok) {
      showOverlay("正在连接 SpeakPal…");
      setStatus("", "连接中…");
      initClient(result.deviceId || SpeakPalAuth.getDeviceId());
      return;
    }
    enterGuestMode();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
