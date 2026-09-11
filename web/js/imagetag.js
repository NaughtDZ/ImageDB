/* ============================================================
 * 标签侧车（.imgtag）导出 / 导入 / 迁移自检
 *
 * 功能：
 *   1. 导出：把选中项(或当前目录树)的标签写入各目录 .imgtag，并列出写入失败的目录；
 *   2. 导入：读取目录树下 .imgtag，写回主库(source=import)，可勾选“覆盖旧导入标签”；
 *   3. 迁移自检：核对 .imgtag 与磁盘/主库三方一致性（缺 .imgtag / 孤儿引用 / 未覆盖）。
 * ============================================================ */
const Imagetag = {
  _busy: false,       // 是否有 .imgtag 后台任务在进行
  _pollTimer: null,

  init() {
    document.querySelectorAll("[data-close='imagetag-import-modal']").forEach(el => {
      el.onclick = () => document.getElementById("imagetag-import-modal").classList.add("hidden");
    });
    document.querySelectorAll("[data-close='imagetag-log-modal']").forEach(el => {
      el.onclick = () => {
        document.getElementById("imagetag-log-modal").classList.add("hidden");
        if (this._pollTimer) { clearTimeout(this._pollTimer); this._pollTimer = null; this._busy = false; }
      };
    });
    document.getElementById("imagetag-import-cancel").onclick = () =>
      document.getElementById("imagetag-import-modal").classList.add("hidden");
    document.getElementById("imagetag-import-go").onclick = () => this.doImport();
  },

  /* ---------------- 进度弹窗 + 后台任务轮询 ---------------- */
  _showProgress(title, text) {
    document.getElementById("imagetag-log-title").textContent = title;
    document.getElementById("imagetag-progress").classList.remove("hidden");
    document.getElementById("imagetag-progress-bar").style.width = "0%";
    document.getElementById("imagetag-progress-text").textContent = text || "准备中…";
    document.getElementById("imagetag-log").innerHTML = "";
    document.getElementById("imagetag-log-modal").classList.remove("hidden");
  },

  _updateProgress(job) {
    const pct = Math.max(0, Math.min(100, job.progress || 0));
    document.getElementById("imagetag-progress-bar").style.width = pct + "%";
    document.getElementById("imagetag-progress-text").textContent =
      (job.message || "") +
      (job.total ? "  （" + (job.done || 0) + " / " + job.total + "）" : "");
  },

  /** 启动 .imgtag 后台任务并轮询进度；完成后回调 onDone(result)。 */
  async _runJob(title, startFn, onDone) {
    if (this._busy) { toast("已有任务在进行中，请稍候", "err"); return; }
    this._busy = true;
    this._showProgress(title, "正在启动…");
    let jobId;
    try {
      const r = await startFn();
      jobId = r && r.job_id;
      if (!jobId) throw new Error("未返回任务 id");
    } catch (e) {
      this._busy = false;
      this._openLog(title, '<div class="row err">启动失败：' + escapeHtml(e.message) + "</div>");
      return;
    }
    const t0 = Date.now();
    const tick = async () => {
      let job;
      try {
        job = await API.get("/api/tags/jobs/" + jobId);
      } catch (e) {
        this._busy = false;
        this._openLog(title, '<div class="row err">读取进度失败：' + escapeHtml(e.message) + "</div>");
        return;
      }
      this._updateProgress(job);
      if (job.status === "running") {
        this._pollTimer = setTimeout(tick, Date.now() - t0 < 1500 ? 200 : 500);
        return;
      }
      this._busy = false;
      document.getElementById("imagetag-progress").classList.add("hidden");
      if (job.status === "failed") {
        this._openLog(title, '<div class="row err">失败：' + escapeHtml(job.error || "未知错误") + "</div>");
        return;
      }
      onDone(job.result || {});
    };
    tick();
  },

  /* ---------------- 导出 ---------------- */
  async exportTags() {
    let scope, ids = [];
    const sel = [...App.state.selected];
    if (sel.length > 0) { scope = "media"; ids = sel; }
    else if (App.state.currentFolderId) { scope = "folder"; ids = [App.state.currentFolderId]; }
    else { toast("请先选择素材或目录", "err"); return; }
    const ok = await promptDialog({
      title: "导出标签到 .imgtag",
      message: "将把所选内容（" + (scope === "media" ? "选中 " + ids.length + " 个素材" : "当前目录树") +
               "）的标签写入其所在目录的 .imgtag。不修改原图、不写主库。继续吗？输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    this._runJob("导出到 .imgtag",
      () => API.post("/api/tags/export", { scope_type: scope, scope_ids: ids }),
      (res) => this.showExportResult(res));
  },

  /* ---------------- 导入 ---------------- */
  async importTags() {
    if (!App.state.currentFolderId) { toast("请在左侧选择一个目录再导入", "err"); return; }
    const name = await this.folderName(App.state.currentFolderId);
    document.getElementById("imagetag-import-info").textContent =
      "导入目录树：当前选中目录「" + name + "」，读取其下 .imgtag 并写回主库。";
    document.getElementById("imagetag-import-modal").classList.remove("hidden");
  },

  async doImport() {
    const overwrite = document.getElementById("imagetag-import-overwrite").checked;
    document.getElementById("imagetag-import-modal").classList.add("hidden");
    this._runJob("从 .imgtag 导入标签",
      () => API.post("/api/tags/import", { folder_id: App.state.currentFolderId, overwrite }),
      async (res) => {
        toast("导入完成：匹配 " + (res.media || 0) + " 个媒体，新增 " + (res.tags || 0) + " 个标签" +
              (res.files ? "（读取 " + res.files + " 个 .imgtag）" : ""), "ok");
        await Gallery.load(true);
        await SidePanel.refresh();
      });
  },

  /* ---------------- 迁移自检 ---------------- */
  async selfCheck() {
    if (!App.state.currentFolderId) { toast("请在左侧选择一个目录再自检", "err"); return; }
    this._runJob("迁移自检",
      () => API.post("/api/tags/selfcheck", { folder_id: App.state.currentFolderId }),
      (res) => this.showSelfCheck(res));
  },


  /* ---------------- 结果展示 ---------------- */
  _openLog(title, html) {
    document.getElementById("imagetag-progress").classList.add("hidden");
    document.getElementById("imagetag-log-title").textContent = title;
    document.getElementById("imagetag-log").innerHTML = html;
    document.getElementById("imagetag-log-modal").classList.remove("hidden");
  },

  showExportResult(res) {
    const failed = res.failed || [];
    let html = '<div class="row ok">✅ 导出完成：目录 ' + (res.dirs || 0) + ' 个，媒体 ' + (res.media || 0) + " 个。</div>";
    if (failed.length) {
      html += '<div class="row err"><b>⚠️ 有 ' + failed.length + " 个目录写入失败：</b></div><ul>";
      for (const f of failed.slice(0, 50)) {
        html += '<li><span class="path">' + escapeHtml(f.dir) + "</span> — " + escapeHtml(f.error) + "</li>";
      }
      if (failed.length > 50) html += "<li>…其余略（共 " + failed.length + " 个）</li>";
      html += "</ul>";
    } else {
      html += '<div class="row ok">全部目录写入成功。</div>';
    }
    this._openLog("导出完成", html);
  },

  showSelfCheck(res) {
    let html = '<div class="row ok">已检查目录 ' + (res.dirs || 0) + " 个，媒体 " + (res.media || 0) + " 个。</div>";
    const mk = (label, arr, total) => {
      const n = total !== undefined ? total : (arr ? arr.length : 0);
      html += '<div class="row"><b>' + label + "</b>：" + n + " 项</div>";
      if (arr && arr.length) {
        html += "<ul>";
        for (const p of arr.slice(0, 50)) html += '<li><span class="path">' + escapeHtml(p) + "</span></li>";
        if (arr.length > 50) html += "<li>…其余略（共 " + arr.length + " 项）</li>";
        html += "</ul>";
      } else {
        html += '<div class="row ok">无</div>';
      }
    };
    mk("缺 .imgtag 的目录", res.missing_imgtag || []);
    mk(".imgtag 引用但磁盘不存在的文件", res.orphan_refs || [], res.orphan_total);
    mk("主库有媒体但 .imgtag 未覆盖", res.uncovered || [], res.uncovered_total);
    this._openLog("迁移自检", html);
  },

  /* ---------------- 目录名辅助 ---------------- */
  async folderName(id) {
    try {
      const data = await API.get("/api/tree");
      const find = (nodes) => {
        for (const n of nodes) {
          if (n.id === id) return n.name;
          const sub = find(n.children || []);
          if (sub) return sub;
        }
        return "";
      };
      return find(data.tree || []) || "#" + id;
    } catch (e) { return "#" + id; }
  },
};
