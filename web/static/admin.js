(function () {
  "use strict";

  var TOKEN_KEY = "xiaozhi_admin_token";
  var schema = [];
  var values = {};
  var dirty = false;

  var el = {
    authPanel: document.getElementById("auth-panel"),
    adminMain: document.getElementById("admin-main"),
    tokenInput: document.getElementById("admin-token"),
    btnLogin: document.getElementById("btn-login"),
    authHint: document.getElementById("auth-hint"),
    sections: document.getElementById("config-sections"),
    infoBar: document.getElementById("info-bar"),
    btnSave: document.getElementById("btn-save"),
    saveStatus: document.getElementById("save-status"),
  };

  var GROUPS = {
    english: { title: "English 服务", desc: "口语陪练路由、模型与聆听参数" },
    system: { title: "系统", desc: "日志与其他全局选项" },
  };

  function token() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function setToken(t) {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  }

  function headers() {
    return {
      "Content-Type": "application/json",
      Authorization: "Bearer " + token(),
    };
  }

  function setStatus(text, kind) {
    el.saveStatus.textContent = text || "";
    el.saveStatus.className = "save-status" + (kind ? " " + kind : "");
  }

  function showToast(text) {
    var node = document.getElementById("admin-toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "admin-toast";
      node.className = "admin-toast hidden";
      document.body.appendChild(node);
    }
    node.textContent = text;
    node.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      node.classList.add("hidden");
    }, 3200);
  }

  function markDirty() {
    dirty = true;
    el.btnSave.disabled = false;
    setStatus("有未保存的更改");
  }

  function fieldVisible(item) {
    if (!item.when) return true;
    var mode = values.english_service_mode || "router";
    return item.when.english_service_mode === mode;
  }

  function renderSections() {
    el.sections.innerHTML = "";
    var nestedByParent = {};
    var topLevel = [];
    schema.forEach(function (item) {
      if (item.nested_under) {
        if (!nestedByParent[item.nested_under]) nestedByParent[item.nested_under] = [];
        nestedByParent[item.nested_under].push(item);
        return;
      }
      topLevel.push(item);
    });

    var byGroup = {};
    topLevel.forEach(function (item) {
      var g = item.group || "other";
      if (!byGroup[g]) byGroup[g] = [];
      byGroup[g].push(item);
    });

    Object.keys(byGroup).forEach(function (groupKey) {
      var meta = GROUPS[groupKey] || { title: groupKey, desc: "" };
      var section = document.createElement("section");
      section.className = "config-section";
      section.innerHTML =
        "<h2>" + meta.title + "</h2>" +
        (meta.desc ? '<p class="section-desc">' + meta.desc + "</p>" : "") +
        '<div class="fields"></div>';
      var fields = section.querySelector(".fields");

      byGroup[groupKey].forEach(function (item) {
        var nested = nestedByParent[item.key] || [];
        fields.appendChild(buildField(item, nested));
      });
      el.sections.appendChild(section);
    });
    refreshFieldVisibility();
  }

  function buildField(item, nestedItems) {
    nestedItems = nestedItems || [];
    var wrap = document.createElement("div");
    wrap.className = "field";
    wrap.dataset.key = item.key;
    if (!fieldVisible(item)) wrap.classList.add("hidden");

    var label = document.createElement("div");
    label.className = "field-label";
    label.innerHTML = escapeHtml(item.label) +
      (item.hot ? '<span class="badge-hot">热更新</span>' : "");
    wrap.appendChild(label);

    if (item.description) {
      var desc = document.createElement("div");
      desc.className = "field-desc";
      desc.textContent = item.description;
      wrap.appendChild(desc);
    }

    if (item.type === "enum") {
      wrap.appendChild(buildEnumField(item));
    } else if (item.type === "bool") {
      wrap.appendChild(buildBoolField(item));
    } else if (item.type === "float") {
      wrap.appendChild(buildNumberField(item));
    } else {
      wrap.appendChild(buildTextField(item));
    }

    if (nestedItems.length) {
      var subWrap = document.createElement("div");
      subWrap.className = "field-nested";
      nestedItems.forEach(function (sub) {
        subWrap.appendChild(buildNestedField(sub));
      });
      wrap.appendChild(subWrap);
    }
    return wrap;
  }

  function buildNestedField(item) {
    var wrap = document.createElement("div");
    wrap.className = "field-sub";
    wrap.dataset.key = item.key;
    if (!fieldVisible(item)) wrap.classList.add("hidden");

    if (item.label) {
      var label = document.createElement("div");
      label.className = "field-sub-label";
      label.textContent = item.label;
      wrap.appendChild(label);
    }
    if (item.description) {
      var desc = document.createElement("div");
      desc.className = "field-desc";
      desc.textContent = item.description;
      wrap.appendChild(desc);
    }
    if (item.type === "enum") {
      wrap.appendChild(buildEnumField(item, "sub"));
    } else if (item.type === "bool") {
      wrap.appendChild(buildBoolField(item));
    } else if (item.type === "float") {
      wrap.appendChild(buildNumberField(item));
    } else {
      wrap.appendChild(buildTextField(item));
    }
    return wrap;
  }

  function buildEnumField(item, variant) {
    var box = document.createElement("div");
    box.className = "field-options" + (variant === "sub" ? " field-options-sub" : "");
    (item.options || []).forEach(function (opt) {
      var row = document.createElement("label");
      row.className = "option-row" + (values[item.key] === opt.value ? " selected" : "");
      row.innerHTML =
        '<input type="radio" name="' + item.key + '" value="' + escapeAttr(opt.value) + '"' +
        (values[item.key] === opt.value ? " checked" : "") + ">" +
        '<div class="option-text"><strong>' + escapeHtml(opt.label) + "</strong></div>";
      row.querySelector("input").addEventListener("change", function () {
        values[item.key] = opt.value;
        markDirty();
        refreshFieldVisibility();
        box.querySelectorAll(".option-row").forEach(function (r) {
          r.classList.toggle("selected", r.querySelector("input").checked);
        });
      });
      box.appendChild(row);
    });
    return box;
  }

  function buildBoolField(item) {
    var row = document.createElement("label");
    row.className = "toggle-row";
    var checked = !!values[item.key];
    row.innerHTML =
      '<input type="checkbox"' + (checked ? " checked" : "") + ">" +
      "<span>" + (checked ? "已开启" : "已关闭") + "</span>";
    row.querySelector("input").addEventListener("change", function (e) {
      values[item.key] = e.target.checked;
      markDirty();
      row.querySelector("span").textContent = e.target.checked ? "已开启" : "已关闭";
    });
    return row;
  }

  function buildNumberField(item) {
    var input = document.createElement("input");
    input.type = "number";
    input.step = "0.1";
    input.value = values[item.key];
    if (item.min != null) input.min = String(item.min);
    if (item.max != null) input.max = String(item.max);
    input.addEventListener("input", function () {
      values[item.key] = parseFloat(input.value);
      markDirty();
    });
    return input;
  }

  function buildTextField(item) {
    var input = document.createElement("input");
    input.type = "text";
    input.value = values[item.key] || "";
    input.addEventListener("input", function () {
      values[item.key] = input.value;
      markDirty();
    });
    return input;
  }

  function refreshFieldVisibility() {
    schema.forEach(function (item) {
      var nodes = el.sections.querySelectorAll('.field[data-key="' + item.key + '"], .field-sub[data-key="' + item.key + '"]');
      nodes.forEach(function (node) {
        if (fieldVisible(item)) node.classList.remove("hidden");
        else node.classList.add("hidden");
      });
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(s) {
    return String(s).replace(/"/g, "&quot;");
  }

  function showMain(show) {
    el.authPanel.classList.toggle("hidden", show);
    el.adminMain.classList.toggle("hidden", !show);
  }

  function loadConfig() {
    return fetch("/api/admin/config", { headers: headers() })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          throw new Error(res.data.error || "加载失败");
        }
        schema = res.data.schema || [];
        values = res.data.values || {};
        el.infoBar.textContent =
          "主机 " + (res.data.public_host || "-") +
          " · 配置保存后立即生效（新 listen / 新轮次）";
        dirty = false;
        el.btnSave.disabled = true;
        setStatus("");
        renderSections();
        showMain(true);
      });
  }

  function saveConfig() {
    var patch = {};
    schema.forEach(function (item) {
      if (!fieldVisible(item) && item.when) return;
      patch[item.key] = values[item.key];
    });
    el.btnSave.disabled = true;
    setStatus("保存中…");
    return fetch("/api/admin/config", {
      method: "PATCH",
      headers: headers(),
      body: JSON.stringify({ values: patch }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.data.error || "保存失败");
        values = res.data.values || values;
        dirty = false;
        var msg = res.data.message || "已保存";
        setStatus("✓ " + msg, "ok");
        showToast(msg);
        renderSections();
      })
      .catch(function (err) {
        setStatus(err.message || "保存失败", "err");
        el.btnSave.disabled = false;
      });
  }

  el.btnLogin.addEventListener("click", function () {
    var t = (el.tokenInput.value || "").trim();
    if (!t) {
      el.authHint.textContent = "请输入 ADMIN_TOKEN";
      el.authHint.className = "hint err";
      return;
    }
    setToken(t);
    el.authHint.textContent = "";
    loadConfig().catch(function (err) {
      setToken("");
      el.authHint.textContent = err.message || "验证失败";
      el.authHint.className = "hint err";
      showMain(false);
    });
  });

  el.btnSave.addEventListener("click", saveConfig);

  el.tokenInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") el.btnLogin.click();
  });

  if (token()) {
    loadConfig().catch(function () {
      setToken("");
      showMain(false);
    });
  } else {
    showMain(false);
  }
})();
