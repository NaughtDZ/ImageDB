/* ============================================================
 * 以图搜图：上传图片 → 完全相同 / 视觉近似 / 标签相似
 * 只在内存里保留查询图；结果通过 /api/similar/search 取回。
 * ============================================================ */
const SimilarSearch = {
  file: null,
  previewUrl: null,
  gen: 0,              // 请求代次：只接受最新一次结果，防止旧响应覆盖
  timer: null,
  jobId: null,
  pollTimer: null,

  /** 不同模式的阈值含义不同，界面上要跟着变 */
  MODES: {
    auto:    { label: "完全相同 + 近似 + 标签", min: 0,   max: 0,    step: 1,    def: 0,    unit: "", show: false },
    exact:   { label: "只找完全相同的文件",    min: 0,   max: 0,    step: 1,    def: 0,    unit: "", show: false },
    similar: { label: "找视觉近似（汉明距离）", min: 0,   max: 16,   step: 1,    def: 6,    unit: "位", show: true },
    tag:     { label: "找标签相似（Jaccard）", min: 10,  max: 90,   step: 5,    def: 30,   unit: "%",  show: true },
  },

  init() {
    document.getElementById("btn-similar").onclick = () => this.open();
    document.querySelectorAll("[data-close='similar-modal']").forEach(el => {
      el.onclick = () => this.close();
    });
    document.getElementById("similar-choose").onclick = () =>
      document.getElementById("similar-file").click();
    document.getElementById("similar-file").addEventListener("change", (e) => {
      if (e.target.files && e.target.files[0]) this.pick(e.target.files[0]);
    });
    document.getElementById("similar-mode").addEventListener("change", () => {
      this.syncThreshold();
      if (this.file) this.run();
    });
    const slider = document.getElementById("similar-threshold");
    slider.addEventListener("input", () => this.syncThresholdLabel());
    slider.addEventListener("change", () => {
      if (this.file) this.schedule();
    });

    const zone = document.getElementById("similar-drop");
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, (e) => {
      e.preventDefault(); zone.classList.add("over");
    }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, (e) => {
      e.preventDefault(); zone.classList.remove("over");
    }));
    zone.addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) this.pick(f);
    });
    // 直接 Ctrl+V 粘贴剪贴板里的图
    document.addEventListener("paste", (e) => {
      if (document.getElementById("similar-modal").classList.contains("hidden")) return;
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.startsWith("image/")) {
          const f = it.getAsFile();
          if (f) { this.pick(f); e.preventDefault(); }
          return;
        }
      }
    });
    document.getElementById("similar-build").onclick = () => this.buildIndex();
    document.getElementById("similar-stop").onclick = () => this.stopIndex();
  },

  open() {
    document.getElementById("similar-modal").classList.remove("hidden");
    this.syncThreshold();
    this.refreshStatus();
  },

  close() {
    document.getElementById("similar-modal").classList.add("hidden");
    this.gen++;                       // 作废在途请求
    if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null; }
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  },

  mode() { return document.getElementById("similar-mode").value || "auto"; },
  cfg() { return this.MODES[this.mode()] || this.MODES.auto; },

  syncThreshold() {
    const cfg = this.cfg();
    const wrap = document.getElementById("similar-threshold-wrap");
    const slider = document.getElementById("similar-threshold");
    wrap.classList.toggle("hidden", !cfg.show);
    slider.min = cfg.min; slider.max = cfg.max; slider.step = cfg.step;
    if (Number(slider.value) < cfg.min || Number(slider.value) > cfg.max) {
      slider.value = cfg.def;
    }
    this.syncThresholdLabel();
  },

  syncThresholdLabel() {
    const cfg = this.cfg();
    const v = Number(document.getElementById("similar-threshold").value);
    document.getElementById("similar-threshold-label").textContent =
      cfg.show ? "相近度门槛：" + v + cfg.unit : "使用默认门槛";
  },

  thresholdValue() {
    const cfg = this.cfg();
    if (!cfg.show) return -1;
    const v = Number(document.getElementById("similar-threshold").value);
    return this.mode() === "tag" ? v / 100 : v;
  },

  pick(file) {
    if (!file.type || !file.type.startsWith("image/")) {
      toast("请选择一张图片（jpg/png/webp/gif 等）", "err");
      return;
    }
    if (file.size > 64 * 1024 * 1024) {
      toast("图片太大（上限 64 MB）", "err");
      return;
    }
    this.file = file;
    if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
    this.previewUrl = URL.createObjectURL(file);
    const img = document.getElementById("similar-preview");
    img.src = this.previewUrl;
    img.classList.remove("hidden");
    document.getElementById("similar-drop-hint").classList.add("hidden");
    this.run();
  },

  schedule() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.run(), 260);   // 拖滑条时防抖，避免连打请求
  },

  async run() {
    if (!this.file) return;
    const gen = ++this.gen;
    const mode = this.mode();
    const threshold = this.thresholdValue();
    const box = document.getElementById("similar-results");
    box.innerHTML = '<div class="similar-empty">搜索中…</div>';
    document.getElementById("similar-note").textContent = "";
    try {
      const qs = "mode=" + encodeURIComponent(mode) +
                 "&threshold=" + encodeURIComponent(threshold) + "&top=200";
      const resp = await fetch("/api/similar/search?" + qs, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: this.file,
      });
      let payload = null;
      try { payload = await resp.json(); } catch (e) { /* 忽略 */ }
      if (gen !== this.gen) return;            // 已有更新的查询，丢弃本次结果
      if (!resp.ok) {
        throw new Error((payload && (payload.detail || payload.message)) || resp.statusText);
      }
      this.render(payload);
    } catch (e) {
      if (gen !== this.gen) return;
      box.innerHTML = '<div class="similar-empty err">搜索失败：' +
        escapeHtml(e.message) + "</div>";
    }
  },

  badge(item) {
    if (item.kind === "exact") return { text: "完全相同", cls: "exact" };
    if (item.kind === "tag") {
      const s = Math.round((item.score || 0) * 100);
      return { text: "标签 " + s + "%", cls: "tag" };
    }
    const d = item.distance == null ? "?" : item.distance;
    return { text: "近似 · 距离 " + d, cls: "near" };
  },

  render(payload) {
    const box = document.getElementById("similar-results");
    const note = document.getElementById("similar-note");
    const items = (payload && payload.items) || [];
    const indexed = payload ? payload.indexed : 0;
    if (!payload || !payload.count) {
      let why = "";
      if (!indexed) why = "还没有建立索引：先点下面的「建立索引」，索引完成后再搜。";
      else if (payload && payload.note) why = payload.note;
      else why = "没有找到达到门槛的图片，可以把门槛调低一些。";
      box.innerHTML = '<div class="similar-empty">' + escapeHtml(why) + "</div>";
    } else {
      box.innerHTML = items.map((it, i) => {
        const b = this.badge(it);
        const thumb = "/api/media/" + it.id + "/thumbnail";
        return '<div class="similar-card" data-index="' + i + '" title="' +
          escapeHtml(it.path) + '">' +
          '<img loading="lazy" src="' + thumb + '" alt="">' +
          '<span class="similar-badge ' + b.cls + '">' + escapeHtml(b.text) + "</span>" +
          '<span class="similar-name">' + escapeHtml(it.filename) + "</span>" +
          "</div>";
      }).join("");
      box.querySelectorAll(".similar-card").forEach(card => {
        card.onclick = () => {
          const idx = Number(card.dataset.index);
          if (window.Viewer && Viewer.open) Viewer.open(items, idx);
        };
      });
    }
    if (note) {
      note.textContent = "已索引 " + indexed + " / " + (payload ? payload.index_total : 0) +
        " 张 · 命中 " + (payload ? payload.count : 0) + " 张" +
        (payload && payload.note ? " · " + payload.note : "");
    }
    this.refreshStatus();
  },

  async refreshStatus() {
    try {
      const st = await API.get("/api/similar/status");
      if (this.gen < 0) return;
      const el = document.getElementById("similar-index-state");
      el.textContent = "索引：" + st.indexed + " / " + st.total + " 张" +
        (st.running ? "（正在建立…）" : "");
      document.getElementById("similar-stop").classList.toggle("hidden", !st.running);
      document.getElementById("similar-build").disabled = !!st.running;
    } catch (e) { /* 状态失败不影响搜索 */ }
  },

  async buildIndex() {
    try {
      const r = await API.post("/api/similar/index/start", { folder_id: 0 });
      this.jobId = r.job_id;
      toast("已开始建立索引，可以继续用程序", "ok");
      this.watchJob(r.job_id);
    } catch (e) {
      toast("启动失败：" + e.message, "err");
    }
  },

  async stopIndex() {
    try {
      await API.post("/api/similar/index/cancel", {});
      toast("已请求停止，当前这张处理完就停", "ok");
    } catch (e) {
      toast("停止失败：" + e.message, "err");
    }
  },

  watchJob(jid) {
    if (this.pollTimer) clearInterval(this.pollTimer);
    const tick = async () => {
      try {
        const job = await API.get("/api/library/jobs/" + jid);
        const done = job.done || 0, total = job.total || 0;
        const pct = job.progress || (total ? Math.floor(done / total * 100) : 0);
        const state = document.getElementById("similar-index-state");
        state.textContent = "索引中 " + pct + "% · " + (job.message || "");
        if (job.status !== "running") {
          clearInterval(this.pollTimer); this.pollTimer = null;
          const res = job.result || {};
          let text = "索引完成：新增 " + (res.added || 0) +
            " · 已有 " + (res.skipped || 0) + " · 失败 " + (res.failed || 0) +
            (res.skipped_large ? " · 超大图跳过 " + res.skipped_large : "");
          const reasons = res.failed_by_reason || {};
          const names = Object.keys(reasons);
          if (names.length) {
            const zh = { decode: "解不开", unreadable: "读不到", empty: "空文件",
                         unsupported: "格式不支持", unknown: "未知" };
            text += "（" + names.map(k => (zh[k] || k) + " " + reasons[k]).join(" · ") + "）";
          }
          state.textContent = text;
          if (res.failed && res.failure_file) {
            note.textContent = "失败清单已写到 " + res.failure_file + "（可直接用记事本打开）";
          }
          this.refreshStatus();
          if (this.file) this.run();
        }
      } catch (e) {
        clearInterval(this.pollTimer); this.pollTimer = null;
        this.refreshStatus();
      }
    };
    tick();
    this.pollTimer = setInterval(tick, 1000);
  },
};
