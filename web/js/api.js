/* ============================================================
 * API 封装：统一 fetch 与错误处理
 * ============================================================ */
window.API = {
  /** 发起请求：method 默认 GET，body 为对象时自动 JSON 序列化 */
  async request(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (e) {
      throw new Error("网络请求失败：" + e.message);
    }
    if (!resp.ok) {
      // 尝试解析后端返回的错误详情
      let detail = resp.statusText;
      try {
        const j = await resp.json();
        detail = j.detail || j.message || detail;
      } catch (e) { /* 忽略解析失败 */ }
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    if (resp.status === 204) return null;
    return resp.json();
  },
  get(url) { return this.request("GET", url); },
  post(url, body) { return this.request("POST", url, body); },
  put(url, body) { return this.request("PUT", url, body); },
  del(url, body) { return this.request("DELETE", url, body); },
};

/* ============================================================
 * 轻提示
 * ============================================================ */
window.toast = function (msg, type) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = type === "err" ? "err" : type === "ok" ? "ok" : "";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.className = "hidden"; }, 3000);
};

/* ============================================================
 * 通用输入对话框（Promise 封装）
 * resolve(null) = 取消；resolve(字符串) = 输入值
 * ============================================================ */
window.promptDialog = function ({ title, message, placeholder, okText }) {
  return new Promise((resolve) => {
    const modal = document.getElementById("prompt-modal");
    document.getElementById("prompt-title").textContent = title || "输入";
    document.getElementById("prompt-msg").textContent = message || "";
    const input = document.getElementById("prompt-input");
    input.value = "";
    input.placeholder = placeholder || "";
    modal.classList.remove("hidden");
    const finish = (val) => { modal.classList.add("hidden"); resolve(val); };
    document.getElementById("prompt-ok").onclick = () => finish(input.value.trim() || null);
    document.getElementById("prompt-cancel").onclick = () => finish(null);
    input.onkeydown = (e) => { if (e.key === "Enter") finish(input.value.trim() || null); };
    input.focus();
  });
};

/* ============================================================
 * HTML 转义（防止路径/文件名注入 DOM）
 * ============================================================ */
window.escapeHtml = function (s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
};

/* 格式化文件大小 */
window.formatSize = function (bytes) {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
};

/* 格式化视频时长（秒 → mm:ss / h:mm:ss） */
window.formatDuration = function (sec) {
  if (!sec || sec <= 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? h + ":" + mm + ":" + ss : mm + ":" + ss;
};
