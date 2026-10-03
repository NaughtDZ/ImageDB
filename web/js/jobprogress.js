/* ============================================================
 * 通用后台任务进度弹窗
 * ------------------------------------------------------------
 * 与后端 app/jobs.py 配套：起任务 → 立即拿到 job_id → 轮询进度。
 * 复用同一套进度弹窗元素（#imagetag-log-modal / #imagetag-progress*），
 * 所以 .imgtag 导出导入、目录「重新扫描」用的是同一个界面。
 *
 * 为什么需要：网络盘上遍历上千个目录要一两分钟，同步请求期间页面
 * 完全没有反馈，用户会以为卡死。
 * ============================================================ */
const JobProgress = {
  _busy: false,      // 是否有后台任务正在进行（用于互斥）
  _timer: null,

  /** 是否有任务正在进行 */
  isBusy() { return this._busy; },

  /** 停止轮询（关闭弹窗时调用）。后台任务本身不受影响，仍会跑完。 */
  stopPolling() {
    if (this._timer) { clearTimeout(this._timer); this._timer = null; }
    this._busy = false;
  },

  /** 显示进度弹窗并归零 */
  show(title, text) {
    document.getElementById("imagetag-log-title").textContent = title;
    document.getElementById("imagetag-progress").classList.remove("hidden");
    document.getElementById("imagetag-progress-bar").style.width = "0%";
    document.getElementById("imagetag-progress-text").textContent = text || "准备中…";
    document.getElementById("imagetag-log").innerHTML = "";
    document.getElementById("imagetag-log-modal").classList.remove("hidden");
  },

  /** 用任务记录刷新进度条与文字 */
  update(job) {
    const pct = Math.max(0, Math.min(100, job.progress || 0));
    document.getElementById("imagetag-progress-bar").style.width = pct + "%";
    document.getElementById("imagetag-progress-text").textContent =
      (job.message || "") +
      (job.total ? "  （" + (job.done || 0) + " / " + job.total + "）" : "");
  },

  hide() {
    const el = document.getElementById("imagetag-progress");
    if (el) el.classList.add("hidden");
  },

  /** 失败/启动失败：隐藏进度条，在弹窗里显示红色错误行 */
  _fail(title, msg) {
    this.hide();
    document.getElementById("imagetag-log-title").textContent = title;
    document.getElementById("imagetag-log").innerHTML =
      '<div class="row err">' + escapeHtml(String(msg)) + "</div>";
    document.getElementById("imagetag-log-modal").classList.remove("hidden");
  },

  /**
   * 启动后台任务并显示进度条。
   * @param title     弹窗标题
   * @param startFn   返回 Promise<{job_id}> 的函数
   * @param onDone    成功回调，参数为 job.result
   * @param urlPrefix 轮询地址前缀，默认 "/api/tags/jobs/"
   * @param onFail    可选，失败回调（除了通用错误界面外的额外处理）
   */
  async run(title, startFn, onDone, urlPrefix, onFail) {
    if (this._busy) { toast("已有任务在进行中，请稍候", "err"); return; }
    this._busy = true;
    this.show(title, "正在启动…");
    let jobId;
    try {
      const r = await startFn();
      jobId = r && r.job_id;
      if (!jobId) throw new Error("未返回任务 id");
    } catch (e) {
      this._busy = false;
      this._fail(title, "启动失败：" + (e.message || e));
      if (onFail) onFail(e);
      return;
    }
    const t0 = Date.now();
    const tick = async () => {
      let job;
      try {
        job = await API.get((urlPrefix || "/api/tags/jobs/") + jobId);
      } catch (e) {
        this._busy = false;
        this._timer = null;
        this._fail(title, "读取进度失败：" + (e.message || e));
        return;
      }
      this.update(job);
      if (job.status === "running") {
        // 前 1.5 秒快速刷新，让用户马上看到"真的在跑"
        this._timer = setTimeout(tick, Date.now() - t0 < 1500 ? 200 : 500);
        return;
      }
      this._busy = false;
      this._timer = null;
      this.hide();
      if (job.status === "failed") {
        this._fail(title, "失败：" + (job.error || "未知错误"));
        if (onFail) onFail(new Error(job.error || "未知错误"));
        return;
      }
      onDone(job.result || {});
    };
    tick();
  },
};
