/* ============================================================
 * 设置对话框
 * 标签页：
 *   代理     代理服务器配置 + 连通性测试（模型下载/LLM/pip 均走代理）
 *   打标     各打标工具的配置（模型目录、阈值、DirectML 开关等）
 *   视频     抽帧间隔、缩略图参数、后台校验间隔
 *   模型下载 HuggingFace 仓库下载（自动配置 model_dir）
 *   依赖更新 pip 安装/更新依赖（走代理）
 *   任务     下载/依赖更新任务列表
 * ============================================================ */
const Settings = {
  settings: null,       // 后端返回的全量设置
  toolNames: [],        // 工具名列表

  /** 打开设置对话框 */
  async open() {
    const modal = document.getElementById("settings-modal");
    modal.classList.remove("hidden");
    this.switchTab("proxy");
    await this.load();
  },

  /** 加载设置 */
  async load() {
    try {
      const data = await API.get("/api/settings");
      this.settings = data.settings || {};
      this.fillProxyForm();
      this.fillVideoForm();
      this.fillTokenForm();
      this.renderToolConfigs();
      this.refreshJobs();
    } catch (e) {
      toast("读取设置失败：" + e.message, "err");
    }
  },

  // ---------------- 标签页切换 ----------------
  switchTab(name) {
    document.querySelectorAll("#settings-modal .tab").forEach(t => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    document.querySelectorAll("#settings-modal .tab-pane").forEach(p => {
      p.classList.toggle("active", p.id === "tab-" + name);
    });
  },

  // ---------------- 代理表单 ----------------
  fillProxyForm() {
    const s = this.settings;
    document.getElementById("set-proxy-enabled").checked = s.proxy_enabled === "true";
    document.getElementById("set-proxy-type").value = s.proxy_type || "http";
    document.getElementById("set-proxy-host").value = s.proxy_host || "";
    document.getElementById("set-proxy-port").value = s.proxy_port || "";
    document.getElementById("set-proxy-user").value = s.proxy_username || "";
    document.getElementById("set-proxy-pass").value = s.proxy_password || "";
  },

  collectProxy() {
    return {
      enabled: document.getElementById("set-proxy-enabled").checked,
      type: document.getElementById("set-proxy-type").value,
      host: document.getElementById("set-proxy-host").value.trim(),
      port: document.getElementById("set-proxy-port").value.trim(),
      username: document.getElementById("set-proxy-user").value.trim(),
      password: document.getElementById("set-proxy-pass").value,
    };
  },

  // ---------------- 视频 / 校验表单 ----------------
  fillVideoForm() {
    const s = this.settings;
    document.getElementById("set-tagging-parallel").value = s.tagging_parallel || "4";
    document.getElementById("set-video-interval").value = s.video_frame_interval_sec || "5";
    document.getElementById("set-video-maxframes").value = s.video_max_frames || "20";
    document.getElementById("set-video-thumbsec").value = s.video_thumb_frame_sec || "1";
    document.getElementById("set-thumb-size").value = s.thumb_size || "320";
    document.getElementById("set-thumb-cache-limit").value = s.thumb_cache_limit_mb || "200";
    document.getElementById("set-verify-interval").value = s.verify_interval_sec || "60";
  },

  // ---------------- HF Token 表单 ----------------
  fillTokenForm() {
    const s = this.settings;
    // 回显已保存的 HuggingFace 令牌（本地单机工具，仅回显给自己看）
    document.getElementById("dl-hf-token").value = s.hf_token || "";
  },

  // ---------------- 打标工具配置 ----------------
  renderToolConfigs() {
    const box = document.getElementById("tool-configs");
    box.innerHTML = "";
    // 由后端 settings 中 tool_ 开头的键推断工具名
    const toolNames = Object.keys(this.settings)
      .filter(k => k.startsWith("tool_"))
      .map(k => k.slice(5));
    this.toolNames = toolNames;
    if (!toolNames.length) {
      box.innerHTML = '<div class="hint">未发现打标工具配置。</div>';
      return;
    }
    for (const name of toolNames) {
      const cfg = this.settings["tool_" + name] || {};
      const div = document.createElement("div");
      div.className = "tool-config";
      div.style.cssText = "border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:10px";
      div.innerHTML =
        '<div style="font-weight:600;margin-bottom:8px">🔧 ' + escapeHtml(name) + "</div>" +
        this.configFields(name, cfg);
      box.appendChild(div);
    }
  },

  /** 生成工具配置字段（按配置键动态生成输入框） */
  configFields(name, cfg) {
    // 字段定义：key -> {label, type}
    const fieldDefs = [
      { key: "model_dir", label: "模型目录", type: "text" },
      { key: "input_size", label: "输入尺寸", type: "number" },
      { key: "threshold", label: "置信度阈值", type: "number" },
      { key: "use_directml", label: "使用 DirectML", type: "checkbox" },
      { key: "include_rating", label: "包含分级标签(wd14)", type: "checkbox" },
      { key: "base_url", label: "接口地址", type: "text" },
      { key: "api_key", label: "API 密钥", type: "password" },
      { key: "model", label: "模型名", type: "text" },
      { key: "prompt", label: "提示词", type: "textarea" },
      { key: "timeout", label: "超时(秒)", type: "number" },
    ];
    let html = "";
    for (const def of fieldDefs) {
      const val = cfg[def.key];
      if (val === undefined && !["base_url", "api_key", "model", "prompt", "timeout"].includes(def.key)) continue;
      const inputId = "cfg-" + name + "-" + def.key;
      if (def.type === "checkbox") {
        html += '<div class="form-row"><label style="width:auto">' +
          '<input type="checkbox" id="' + inputId + '" data-tool="' + name +
          '" data-key="' + def.key + '"' + (val ? " checked" : "") + "> " +
          def.label + "</label></div>";
      } else if (def.type === "textarea") {
        html += '<div class="form-row" style="align-items:flex-start"><label>' + def.label + "</label>" +
          '<textarea id="' + inputId + '" data-tool="' + name + '" data-key="' + def.key +
          '" style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);' +
          'border-radius:4px;padding:6px;min-height:60px;outline:none">' +
          escapeHtml(val ?? "") + "</textarea></div>";
      } else {
        const step = def.key === "threshold" ? "0.05" : def.key === "input_size" ? "1" : "";
        html += '<div class="form-row"><label>' + def.label + "</label>" +
          '<input type="' + def.type + '" id="' + inputId + '" data-tool="' + name +
          '" data-key="' + def.key + '" value="' + escapeHtml(val ?? "") + '"' +
          (step ? " step=\"" + step + "\"" : "") + "></div>";
      }
    }
    if (cfg.model_dir_hint) {
      html += '<div class="hint">' + escapeHtml(cfg.model_dir_hint) + "</div>";
    }
    return html;
  },

  // ---------------- 保存 ----------------
  collectSettings() {
    const s = {};
    // 代理
    const p = this.collectProxy();
    s.proxy_enabled = String(p.enabled);
    s.proxy_type = p.type;
    s.proxy_host = p.host;
    s.proxy_port = p.port;
    s.proxy_username = p.username;
    s.proxy_password = p.password;
    // 视频 / 校验
    s.tagging_parallel = document.getElementById("set-tagging-parallel").value;
    s.video_frame_interval_sec = document.getElementById("set-video-interval").value;
    s.video_max_frames = document.getElementById("set-video-maxframes").value;
    s.video_thumb_frame_sec = document.getElementById("set-video-thumbsec").value;
    s.thumb_size = document.getElementById("set-thumb-size").value;
    s.thumb_cache_limit_mb = document.getElementById("set-thumb-cache-limit").value;
    s.verify_interval_sec = document.getElementById("set-verify-interval").value;
    // HuggingFace 令牌
    s.hf_token = document.getElementById("dl-hf-token").value.trim();
    // 打标工具配置（从输入框收集）
    for (const name of this.toolNames) {
      const base = this.settings["tool_" + name] || {};
      const cfg = { ...base };
      document.querySelectorAll("#tool-configs [data-tool='" + name + "']").forEach(el => {
        const key = el.dataset.key;
        if (el.type === "checkbox") cfg[key] = el.checked;
        else if (el.type === "number") cfg[key] = parseFloat(el.value) || 0;
        else cfg[key] = el.value;
      });
      s["tool_" + name] = cfg;
    }
    return s;
  },

  async save() {
    try {
      await API.put("/api/settings", { settings: this.collectSettings() });
      toast("设置已保存（打标插件已重载）", "ok");
    } catch (e) {
      toast("保存失败：" + e.message, "err");
    }
  },

  // ---------------- 代理测试 ----------------
  async testProxy() {
    const p = this.collectProxy();
    const el = document.getElementById("proxy-test-result");
    el.textContent = "测试中…";
    el.className = "msg";
    try {
      const res = await API.post("/api/settings/test-proxy", { proxy: p });
      if (res.ok) {
        el.textContent = "✅ 代理可用，延迟 " + res.latency_ms + " ms";
        el.className = "msg ok";
      } else {
        el.textContent = "❌ " + (res.error || "连接失败");
        el.className = "msg err";
      }
    } catch (e) {
      el.textContent = "❌ " + e.message;
      el.className = "msg err";
    }
  },

  // ---------------- 模型下载 ----------------
  async downloadModel() {
    const repo = document.getElementById("dl-repo").value.trim();
    const tool = document.getElementById("dl-tool").value;
    if (!repo) { toast("请输入 HuggingFace 仓库地址", "err"); return; }
    try {
      const res = await API.post("/api/models/download", { repo_id: repo, tool });
      toast("下载任务已启动，请到“任务”页查看进度", "ok");
      this.switchTab("tasks");
      this.refreshJobs();
    } catch (e) {
      toast("下载启动失败：" + e.message, "err");
    }
  },

  // ---------------- 依赖更新 ----------------
  async updateDeps() {
    const packages = document.getElementById("deps-packages").value.trim();
    if (!packages) { toast("请输入要更新的包名", "err"); return; }
    try {
      const res = await API.post("/api/deps/update", { packages });
      toast("依赖更新任务已启动，请到“任务”页查看进度", "ok");
      this.switchTab("tasks");
      this.refreshJobs();
    } catch (e) {
      toast("更新启动失败：" + e.message, "err");
    }
  },

  /** 一键安装 onnxruntime-directml（GPU 加速） */
  async installDirectML() {
    const ok = await promptDialog({
      title: "安装 DirectML",
      message: "将卸载普通 onnxruntime 并安装 onnxruntime-directml（GPU 加速，速度提升数十倍）。" +
               "安装完成后需要重启程序。继续吗？输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/deps/install-directml", {});
      toast("DirectML 安装任务已启动，请到“任务”页查看进度", "ok");
      this.switchTab("tasks");
      this.refreshJobs();
    } catch (e) {
      toast("启动失败：" + e.message, "err");
    }
  },

  // ---------------- 任务列表 ----------------
  async refreshJobs() {
    const box = document.getElementById("jobs-list");
    try {
      const data = await API.get("/api/downloads");
      const jobs = data.jobs || [];
      if (!jobs.length) {
        box.innerHTML = '<div class="hint">暂无任务。</div>';
        return;
      }
      box.innerHTML = "";
      for (const j of jobs.reverse()) {
        const div = document.createElement("div");
        div.className = "job-item";
        div.innerHTML =
          '<div class="j-head"><span>' + escapeHtml(j.label) + "</span>" +
          '<span class="j-status ' + escapeHtml(j.status) + '">' + escapeHtml(j.status) + "</span></div>" +
          '<div class="j-msg">' + escapeHtml(j.message || "") +
          (j.progress ? "（" + j.progress + "%）" : "") + "</div>" +
          (j.lines && j.lines.length
            ? "<pre>" + escapeHtml(j.lines.join("\n")) + "</pre>"
            : "");
        box.appendChild(div);
      }
    } catch (e) {
      box.innerHTML = '<div class="hint">读取任务失败。</div>';
    }
  },

  /** 初始化事件 */
  init() {
    // 标签页切换
    document.querySelectorAll("#settings-modal .tab").forEach(t => {
      t.onclick = () => this.switchTab(t.dataset.tab);
    });
    document.getElementById("btn-save-settings").onclick = () => this.save();
    document.getElementById("btn-test-proxy").onclick = () => this.testProxy();
    document.getElementById("btn-download-model").onclick = () => this.downloadModel();
    document.getElementById("btn-update-deps").onclick = () => this.updateDeps();
    document.getElementById("btn-install-directml").onclick = () => this.installDirectML();
    // 显示/隐藏 HF 令牌
    document.getElementById("btn-toggle-token").onclick = () => {
      const inp = document.getElementById("dl-hf-token");
      inp.type = inp.type === "password" ? "text" : "password";
    };
    document.getElementById("btn-refresh-jobs").onclick = () => this.refreshJobs();
    document.querySelectorAll("[data-close='settings-modal']").forEach(el => {
      el.onclick = () => document.getElementById("settings-modal").classList.add("hidden");
    });
  },
};
