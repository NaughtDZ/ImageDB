/* ============================================================
 * 打标对话框
 * 职责：
 *   1. 列出可用打标工具（含加载状态/错误提示）；
 *   2. 选择作用范围：选中媒体 / 当前目录（整棵子树）；
 *   3. 启动打标任务并轮询进度，支持取消。
 * ============================================================ */
const Tagger = {
  tools: [],           // 工具列表
  scopeType: "media",  // 'media' | 'folder'
  scopeIds: [],        // 目标 id
  jobId: null,         // 当前任务 id
  pollTimer: null,     // 进度轮询定时器
  _pollGeneration: 0,
  _viewGeneration: 0,
  _visible: false,
  _starting: false,

  /** 打开打标对话框（默认范围：选中媒体） */
  async open() {
    if (this.jobId || this._starting) return this.showDialog();
    const sel = [...App.state.selected];
    if (sel.length === 0 && App.state.currentFolderId === null) {
      toast("请先选择要打标的媒体，或在左侧选择目录", "err");
      return;
    }
    if (sel.length > 0) {
      this.scopeType = "media";
      this.scopeIds = sel;
    } else {
      this.scopeType = "folder";
      this.scopeIds = [App.state.currentFolderId];
    }
    return this.showDialog();
  },

  /** 已有任务时重连原范围，不用新的目录覆盖它。 */
  async openWithFolder(folderId) {
    if (!this.jobId && !this._starting) {
      this.scopeType = "folder";
      this.scopeIds = [folderId];
    }
    return this.showDialog();
  },

  async showDialog() {
    const generation = ++this._viewGeneration;
    this._visible = true;
    await this.loadTools();
    if (!this._visible || generation !== this._viewGeneration) return;
    this.renderTools();
    this.renderScope();
    document.getElementById("tagger-start").disabled = !!this.jobId || this._starting;
    document.getElementById("tagger-progress-row").style.display = this.jobId ? "" : "none";
    document.getElementById("tagger-cancel-job").style.display = this.jobId ? "" : "none";
    document.getElementById("tagger-msg").textContent = this.jobId ? "正在重新连接任务…" : this._starting ? "正在启动…" : "";
    document.getElementById("tagger-modal").classList.remove("hidden");
    if (this.jobId) this.pollProgress();
  },

  stopPolling() {
    ++this._pollGeneration;
    if (this.pollTimer) clearTimeout(this.pollTimer);
    this.pollTimer = null;
  },

  close() {
    this._visible = false;
    ++this._viewGeneration;
    this.stopPolling();
    document.getElementById("tagger-modal").classList.add("hidden");
  },

  /** 加载工具列表 */
  async loadTools() {
    try {
      const data = await API.get("/api/tagging/tools");
      this.tools = data.tools || [];
    } catch (e) {
      this.tools = [];
      toast("加载打标工具失败：" + e.message, "err");
    }
  },

  /** 渲染工具选择 */
  renderTools() {
    const box = document.getElementById("tagger-tools");
    box.innerHTML = "";
    if (!this.tools.length) {
      box.innerHTML = '<div class="hint">未发现打标工具，请到设置页检查插件加载情况。</div>';
      return;
    }
    this.tools.forEach((t, i) => {
      const div = document.createElement("div");
      div.className = "tool-option" + (i === 0 ? " selected" : "");
      div.dataset.tool = t.name;
      div.innerHTML =
        '<span class="t-name">' + escapeHtml(t.display_name) + "</span>" +
        '<span class="t-desc">' + escapeHtml(t.description || "") + "</span>" +
        '<span class="t-state ' + (t.loaded ? "ok" : "err") + '">' +
        (t.loaded ? "● 就绪" : "○ " + escapeHtml(t.error || "未加载")) + "</span>";
      div.onclick = () => {
        box.querySelectorAll(".tool-option").forEach(x => x.classList.remove("selected"));
        div.classList.add("selected");
      };
      box.appendChild(div);
    });
  },

  /** 渲染作用范围说明 */
  renderScope() {
    const box = document.getElementById("tagger-scope");
    box.innerHTML = "";
    const opts = [];
    if (this.scopeType === "media") {
      opts.push({ label: "选中媒体（" + this.scopeIds.length + " 项）", value: "media" });
    } else {
      opts.push({ label: "当前目录（含全部子目录）", value: "folder" });
    }
    for (const o of opts) {
      const div = document.createElement("div");
      div.className = "scope-option selected";
      div.innerHTML = '<span>🎯 ' + escapeHtml(o.label) + "</span>";
      box.appendChild(div);
    }
  },

  /** 启动打标 */
  async start() {
    if (this.jobId || this._starting) return;
    const selected = document.querySelector("#tagger-tools .tool-option.selected");
    if (!selected) { toast("请选择打标工具", "err"); return; }
    const tool = selected.dataset.tool;
    const overwrite = document.getElementById("tagger-overwrite").checked;

    this._starting = true;
    document.getElementById("tagger-start").disabled = true;
    try {
      const res = await API.post("/api/tagging/run", {
        tool,
        scope_type: this.scopeType,
        scope_ids: this.scopeIds,
        overwrite,
      });
      if (!res.job_id) throw new Error("未返回任务 id");
      this.jobId = res.job_id;
      document.getElementById("tagger-start").disabled = true;
      document.getElementById("tagger-cancel-job").style.display = "";
      document.getElementById("tagger-progress-row").style.display = "";
      document.getElementById("tagger-msg").textContent = "任务已启动…";
      if (this._visible) this.pollProgress();
    } catch (e) {
      toast("启动打标失败：" + e.message, "err");
      document.getElementById("tagger-msg").className = "msg err";
      document.getElementById("tagger-msg").textContent = e.message;
    } finally {
      this._starting = false;
      document.getElementById("tagger-start").disabled = !!this.jobId;
    }
  },

  /** 轮询任务进度 */
  pollProgress() {
    this.stopPolling();
    const generation = this._pollGeneration, jobId = this.jobId;
    const current = () => this._visible && generation === this._pollGeneration && jobId === this.jobId;
    const tick = async () => {
      if (!jobId || !current()) return;
      try {
        const job = await API.get("/api/tagging/jobs/" + jobId);
        if (!current()) return;
        const bar = document.getElementById("tagger-progress-bar");
        const text = document.getElementById("tagger-progress-text");
        if (job.total > 0) {
          bar.style.width = (job.done / job.total * 100) + "%";
          text.textContent = job.done + " / " + job.total + (job.message ? "（" + job.message + "）" : "");
        } else {
          text.textContent = job.message || "";
        }
        if (["done", "failed", "cancelled"].includes(job.status)) {
          this.stopPolling();
          this.jobId = null;
          document.getElementById("tagger-start").disabled = false;
          document.getElementById("tagger-cancel-job").style.display = "none";
          const msg = document.getElementById("tagger-msg");
          if (job.status === "done") {
            msg.className = "msg ok";
            msg.textContent = "✅ 打标完成，共处理 " + job.total + " 个文件";
            toast("打标完成", "ok");
          } else if (job.status === "failed") {
            msg.className = "msg err";
            msg.textContent = "❌ " + (job.message || "打标失败");
            toast("打标失败：" + job.message, "err");
          } else {
            msg.textContent = "已取消";
          }
          // 刷新画廊与右侧标签栏（打标后标签已变化）
          Gallery.load(true).then(() => SidePanel.refresh());
          return;
        }
        this.pollTimer = setTimeout(tick, 1000);
      } catch (e) {
        if (!current()) return;
        this.stopPolling();
        document.getElementById("tagger-msg").textContent = "读取进度失败：" + e.message + "（关闭后重开可重连）";
      }
    };
    return tick();
  },

  /** 取消任务 */
  async cancel() {
    if (!this.jobId) return;
    try {
      await API.post("/api/tagging/jobs/" + this.jobId + "/cancel", {});
      toast("已发送取消请求", "ok");
    } catch (e) {
      toast("取消失败：" + e.message, "err");
    }
  },

  /** 初始化事件 */
  init() {
    document.getElementById("tagger-start").onclick = () => this.start();
    document.getElementById("tagger-cancel-job").onclick = () => this.cancel();
    document.querySelectorAll("[data-close='tagger-modal']").forEach(el => {
      el.onclick = () => this.close();
    });
  },
};
