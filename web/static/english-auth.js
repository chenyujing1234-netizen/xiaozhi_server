/**
 * SpeakPal 登录：微信小程序 WebView 用 wx_code；其它环境用手机号验证码。
 * 机制对齐 /home/chenyj/huanDa
 */
(function (global) {
  "use strict";

  var TOKEN_KEY = "speakpal_token";
  var USER_KEY = "speakpal_user";
  var DEVICE_KEY = "speakpal_device_id";
  var API_BASE = "/api/auth";

  var isWeChat = /MicroMessenger/i.test(navigator.userAgent);
  var state = {
    token: null,
    user: null,
    wxLoginFailed: false,
    codeSent: false,
    countdown: 0,
  };
  var countdownTimer = null;
  var onLoggedIn = null;
  var loginInFlight = false;
  var loginWaitResolve = null;
  var formBound = false;

  function $(id) {
    return document.getElementById(id);
  }

  function apiError(data, fallback) {
    if (!data) return fallback;
    return data.error || data.detail || data.message || fallback;
  }

  /** 同域 POST JSON；fetch 失败时回退 XHR */
  function postJson(path, payload) {
    var url = API_BASE + path;
    var body = JSON.stringify(payload || {});
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
      credentials: "same-origin",
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { res: res, data: data };
      });
    }).catch(function (fetchErr) {
      return new Promise(function (resolve, reject) {
        try {
          var xhr = new XMLHttpRequest();
          xhr.open("POST", url, true);
          xhr.setRequestHeader("Content-Type", "application/json");
          xhr.onload = function () {
            var data = {};
            try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* noop */ }
            resolve({
              res: { ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status },
              data: data,
            });
          };
          xhr.onerror = function () {
            reject(fetchErr || new Error("XHR network error"));
          };
          xhr.send(body);
        } catch (e) {
          reject(fetchErr || e);
        }
      });
    });
  }

  function saveLogin(token, user) {
    state.token = token;
    state.user = user || {};
    try {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, JSON.stringify(state.user));
      if (state.user.device_id) {
        localStorage.setItem(DEVICE_KEY, state.user.device_id);
      }
    } catch (e) { /* noop */ }
  }

  function clearLogin() {
    state.token = null;
    state.user = null;
    try {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    } catch (e) { /* noop */ }
  }

  function isLoggedIn() {
    return !!(state.token && state.user && state.user.id);
  }

  function getDeviceId() {
    if (state.user && state.user.id) {
      return state.user.device_id || ("user-" + state.user.id);
    }
    try {
      return localStorage.getItem(DEVICE_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function showLoginModal() {
    var modal = $("login-modal");
    if (modal) modal.classList.remove("hidden");
    renderLoginUi();
  }

  function hideLoginModal() {
    var modal = $("login-modal");
    if (modal) modal.classList.add("hidden");
  }

  function setLoginError(msg) {
    var el = $("login-error");
    if (!el) return;
    if (msg) {
      el.textContent = msg;
      el.classList.remove("hidden");
    } else {
      el.textContent = "";
      el.classList.add("hidden");
    }
  }

  function setLoading(loading) {
    var sendBtn = $("btn-send-code");
    var loginBtn = $("btn-phone-login");
    var phone = $("login-phone");
    var code = $("login-code");
    if (sendBtn) {
      sendBtn.disabled = !!loading || !isPhoneValid();
      sendBtn.textContent = loading && !state.codeSent ? "发送中…" : "获取验证码";
    }
    if (loginBtn) {
      loginBtn.disabled = !!loading || !code || code.value.length < 6;
      loginBtn.textContent = loading && state.codeSent ? "登录中…" : "登录";
    }
    if (phone) phone.disabled = !!loading;
    if (code) code.disabled = !!loading;
    updateResendBtn();
  }

  function isPhoneValid() {
    var phone = $("login-phone");
    return phone && /^1[3-9]\d{9}$/.test(phone.value.trim());
  }

  function startCountdown() {
    state.countdown = 60;
    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(function () {
      state.countdown -= 1;
      if (state.countdown <= 0) {
        clearInterval(countdownTimer);
        countdownTimer = null;
        state.countdown = 0;
      }
      updateResendBtn();
    }, 1000);
    updateResendBtn();
  }

  function updateResendBtn() {
    var btn = $("btn-resend-code");
    if (!btn) return;
    if (state.countdown > 0) {
      btn.disabled = true;
      btn.textContent = state.countdown + "s";
    } else {
      btn.disabled = false;
      btn.textContent = "重新发送";
    }
  }

  function renderLoginUi() {
    var wxTip = $("wx-login-tip");
    var wxFail = $("wx-login-fail");
    var phoneForm = $("phone-login-form");
    var showPhone = !isWeChat || state.wxLoginFailed;

    if (wxTip) {
      wxTip.classList.toggle("hidden", !(isWeChat && !state.wxLoginFailed));
    }
    if (wxFail) {
      wxFail.classList.toggle("hidden", !(isWeChat && state.wxLoginFailed));
    }
    if (phoneForm) {
      phoneForm.classList.toggle("hidden", !showPhone);
    }

    var codeGroup = $("login-code-group");
    var sendBtn = $("btn-send-code");
    var loginBtn = $("btn-phone-login");
    if (codeGroup) codeGroup.classList.toggle("hidden", !state.codeSent);
    if (sendBtn) sendBtn.classList.toggle("hidden", state.codeSent);
    if (loginBtn) loginBtn.classList.toggle("hidden", !state.codeSent);

    var send = $("btn-send-code");
    if (send) send.disabled = !isPhoneValid();
    var login = $("btn-phone-login");
    var code = $("login-code");
    if (login) login.disabled = !code || code.value.length < 6;
  }

  function resolveLoginWait(result) {
    if (typeof loginWaitResolve !== "function") return;
    var resolve = loginWaitResolve;
    loginWaitResolve = null;
    resolve(result);
  }

  function finishLogin(token, user, toastMsg) {
    saveLogin(token, user);
    hideLoginModal();
    setLoginError("");
    var payload = { token: token, user: user, toast: toastMsg || "登录成功" };
    resolveLoginWait({ ok: true, user: user, deviceId: getDeviceId() });
    if (typeof onLoggedIn === "function") {
      try {
        onLoggedIn(payload);
      } catch (e) {
        console.warn("[SpeakPalAuth] onLoggedIn error", e);
      }
    }
  }

  function dismissLogin() {
    hideLoginModal();
    setLoginError("");
    resolveLoginWait({ ok: false, cancelled: true });
  }

  async function sendCode() {
    if (!isPhoneValid()) {
      setLoginError("请输入正确的手机号");
      return;
    }
    setLoginError("");
    setLoading(true);
    try {
      var phone = $("login-phone").value.trim();
      // 仅发短信：不带 code
      var out = await postJson("/send-code", { phone: phone });
      if (!out.res.ok || out.data.ok === false) {
        setLoginError(apiError(out.data, "发送失败，请重试"));
        return;
      }
      state.codeSent = true;
      startCountdown();
      renderLoginUi();
      if (global.SpeakPalAuth && global.SpeakPalAuth._toast) {
        global.SpeakPalAuth._toast("验证码已发送，请注意查收");
      }
    } catch (e) {
      console.warn("[SpeakPalAuth] send-code failed", e);
      setLoginError("网络错误，请检查连接");
    } finally {
      setLoading(false);
      renderLoginUi();
    }
  }

  async function doPhoneLogin() {
    var phoneEl = $("login-phone");
    var codeEl = $("login-code");
    if (!phoneEl || !codeEl || codeEl.value.length < 6) return;
    if (loginInFlight) return;
    loginInFlight = true;
    setLoginError("");
    setLoading(true);
    var phone = phoneEl.value.trim();
    var sms = codeEl.value.trim();
    // linkpal 在共享 443 HTTP/2 下，发码后的第二次 XHR 常 Failed to fetch；
    // 登录改为整页 GET，绕过该问题。
    var q = "phone=" + encodeURIComponent(phone)
      + "&sms=" + encodeURIComponent(sms)
      + "&go=1";
    window.location.href = API_BASE + "/send-code?" + q;
  }

  async function doWxLogin(code) {
    try {
      var res = await fetch(API_BASE + "/wx-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wx_code: code, code: code }),
      });
      var data = await res.json().catch(function () { return {}; });
      if (res.ok && data.token) {
        state.wxLoginFailed = false;
        finishLogin(data.token, data.user, "微信登录成功");
        return true;
      }
      state.wxLoginFailed = true;
      renderLoginUi();
      setLoginError(apiError(data, "微信自动登录失败，请用手机号登录"));
      return false;
    } catch (e) {
      state.wxLoginFailed = true;
      renderLoginUi();
      setLoginError("微信登录异常，请用手机号登录");
      return false;
    }
  }

  async function checkLogin() {
    var token = null;
    var userRaw = null;
    try {
      token = localStorage.getItem(TOKEN_KEY);
      userRaw = localStorage.getItem(USER_KEY);
    } catch (e) { /* noop */ }
    if (!token || !userRaw) return false;
    try {
      var res = await fetch(API_BASE + "/me", {
        headers: { Authorization: "Bearer " + token },
      });
      if (!res.ok) {
        clearLogin();
        return false;
      }
      var data = await res.json();
      if (!data.ok || !data.user) {
        clearLogin();
        return false;
      }
      saveLogin(token, data.user);
      return true;
    } catch (e) {
      // 网络抖动时暂时信任本地登录态
      try {
        state.token = token;
        state.user = JSON.parse(userRaw);
        if (state.user && state.user.id && !state.user.device_id) {
          state.user.device_id = "user-" + state.user.id;
        }
        return !!(state.user && state.user.id);
      } catch (e2) {
        clearLogin();
        return false;
      }
    }
  }

  function bindLoginForm() {
    if (formBound) return;
    formBound = true;
    var phone = $("login-phone");
    var code = $("login-code");
    var sendBtn = $("btn-send-code");
    var resendBtn = $("btn-resend-code");
    var loginBtn = $("btn-phone-login");
    var closeBtn = $("btn-login-close");
    var modal = $("login-modal");

    if (phone) {
      phone.addEventListener("input", function () {
        phone.value = phone.value.replace(/\D/g, "").slice(0, 11);
        setLoginError("");
        renderLoginUi();
      });
    }
    if (code) {
      code.addEventListener("input", function () {
        code.value = code.value.replace(/\D/g, "").slice(0, 6);
        setLoginError("");
        renderLoginUi();
        // 输满 6 位自动登录，减少点击被拦截的情况
        if (code.value.length === 6) {
          doPhoneLogin();
        }
      });
      code.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          doPhoneLogin();
        }
      });
    }
    if (sendBtn) sendBtn.addEventListener("click", sendCode);
    if (resendBtn) resendBtn.addEventListener("click", sendCode);
    if (loginBtn) loginBtn.addEventListener("click", doPhoneLogin);
    if (closeBtn) closeBtn.addEventListener("click", dismissLogin);
    if (modal) {
      modal.addEventListener("click", function (ev) {
        if (ev.target === modal) dismissLogin();
      });
    }
  }

  async function tryWxCodeLogin() {
    var params = new URLSearchParams(global.location.search);
    var wxCode = params.get("wx_code");
    if (!wxCode) {
      if (isWeChat) {
        // 小程序未带 code：需要时再用手机号
        state.wxLoginFailed = true;
      }
      return false;
    }
    var ok = await doWxLogin(wxCode);
    stripWxCodeFromUrl();
    return ok;
  }

  function stripWxCodeFromUrl() {
    try {
      var url = new URL(global.location.href);
      if (!url.searchParams.has("wx_code")) return;
      url.searchParams.delete("wx_code");
      global.history.replaceState({}, "", url.toString());
    } catch (e) { /* noop */ }
  }

  function applyAuthOptions(options) {
    options = options || {};
    if (options.onLoggedIn) onLoggedIn = options.onLoggedIn;
    if (options.toast) global.SpeakPalAuth._toast = options.toast;
  }

  /**
   * 进页静默校验登录；未登录不弹窗，允许先浏览页面。
   * @param {{ onLoggedIn?: Function, toast?: Function }} options
   * @returns {Promise<{ok:boolean, user?:object, deviceId?:string}>}
   */
  async function initAuth(options) {
    applyAuthOptions(options);
    bindLoginForm();
    hideLoginModal();

    if (await checkLogin()) {
      return { ok: true, user: state.user, deviceId: getDeviceId() };
    }

    await tryWxCodeLogin();
    if (isLoggedIn()) {
      return { ok: true, user: state.user, deviceId: getDeviceId() };
    }

    hideLoginModal();
    return { ok: false };
  }

  /**
   * 使用功能前要求登录：弹出登录层并等待完成或取消。
   * @param {{ onLoggedIn?: Function, toast?: Function }} options
   * @returns {Promise<{ok:boolean, cancelled?:boolean, user?:object, deviceId?:string}>}
   */
  async function requireLogin(options) {
    applyAuthOptions(options);
    bindLoginForm();

    if (isLoggedIn() || await checkLogin()) {
      hideLoginModal();
      return { ok: true, user: state.user, deviceId: getDeviceId() };
    }

    if (!isWeChat || state.wxLoginFailed) {
      state.wxLoginFailed = true;
    }
    setLoginError("");
    showLoginModal();
    renderLoginUi();

    if (isLoggedIn()) {
      return { ok: true, user: state.user, deviceId: getDeviceId() };
    }

    return new Promise(function (resolve) {
      loginWaitResolve = resolve;
    });
  }

  /**
   * 兼容旧调用：直接要求登录（不再作为进页门禁）。
   */
  async function ensureLogin(options) {
    var silent = await initAuth(options);
    if (silent.ok) return silent;
    return requireLogin(options);
  }

  function logout() {
    if (isWeChat) return;
    clearLogin();
    state.codeSent = false;
    state.countdown = 0;
    global.location.reload();
  }

  global.SpeakPalAuth = {
    initAuth: initAuth,
    requireLogin: requireLogin,
    ensureLogin: ensureLogin,
    isLoggedIn: isLoggedIn,
    getUser: function () { return state.user; },
    getToken: function () { return state.token; },
    getDeviceId: getDeviceId,
    logout: logout,
    dismissLogin: dismissLogin,
    isWeChat: isWeChat,
    _toast: null,
  };
})(window);
