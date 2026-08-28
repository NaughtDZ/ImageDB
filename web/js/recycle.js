/* ============================================================
 * 应用回收站模块
 * ============================================================
 * 这些素材来自"不支持系统回收站"的盘（如网络盘 K:）——
 * 它们被移到本地 data/recycle_bin/ 暂存，绝不彻底删除、
 * 也绝不会丢失。本模块负责：
 *   - 列出回收站素材；
 *   - 单条还原（移回原目录，恢复数据库记录与标签）；
 *   - 单条/全部"清空"（彻底删除，需输入 yes 二次确认）。
 */
const Recycle = {
  // 当前回收站条目缓存（供"清空"使用）
  _items: [],

  /** 初始化：绑定关闭与底部按钮 */
  init() {
    document.querySelectorAll("[data-close='recycle-modal']").forEach((el) => {
      el.onclick = () => document.getElementById("recycle-modal").classList.add("hidden");
    });
    document.getElementById("recycle-refresh").onclick = () => this.refresh();
    document.getElementById("recycle-clear-all").onclick = () => this.clearAll();
    // 点击遮罩空白处关闭
    document.getElementById("recycle-modal").addEventListener("click", (e) => {
      if (e.target === e.currentTarget) this.close();
    });
  },

  /** 关闭弹窗 */
  close() {
    document.getElementById("recycle-modal").classList.add("hidden");
  },

  /** 打开回收站弹窗并刷新列表 */
  async open() {
    document.getElementById("recycle-modal").classList.remove("hidden");
    await this.refresh();
  },

  /** 刷新回收站列表 */
  async refresh() {
    const info = document.getElementById("recycle-info");
    const list = document.getElementById("recycle-list");
    list.innerHTML = '<div class="hint">加载中…</div>';
    try {
      const data = await API.get("/api/recycle/list");
      const items = data.items || [];
      this._items = items;
      info.textContent =
        "共 " + items.length + " 项（来自不支持系统回收站的盘，可还原或彻底删除）";
      list.innerHTML = "";
      const clearBtn = document.getElementById("recycle-clear-all");
      clearBtn.disabled = items.length === 0;
      if (!items.length) {
        list.innerHTML = '<div class="hint">回收站为空。</div>';
        return;
      }
      for (const it of items) list.appendChild(this.renderRow(it));
    } catch (e) {
      info.textContent = "";
      list.innerHTML =
        '<div class="hint">加载失败：' + window.escapeHtml(e.message) + "</div>";
    }
  },

  /** 渲染单个回收站条目 */
  renderRow(it) {
    const row = document.createElement("div");
    row.className = "recycle-row";
    const icon = it.type === "video" ? "🎞" : "🖼";
    row.innerHTML =
      '<span class="r-icon">' + icon + "</span>" +
      '<div class="r-main">' +
        '<div class="r-name">' + window.escapeHtml(it.filename) + "</div>" +
        '<div class="r-path" title="' + window.escapeHtml(it.path) + '">' + window.escapeHtml(it.path) + "</div>" +
        '<div class="r-meta">' +
          window.escapeHtml(it.type) + " · " + window.formatSize(it.size) +
          (it.ext ? " · " + window.escapeHtml(it.ext) : "") +
          " · 回收于 " + window.escapeHtml(it.created_at || "") +
        "</div>" +
      "</div>" +
      '<div class="r-actions">' +
        '<button class="btn" data-op="restore">⟲ 还原</button>' +
        '<button class="btn danger" data-op="delete">🗑 彻底删除</button>' +
      "</div>";

    row.querySelector('[data-op="restore"]').onclick = async () => {
      const btn = row.querySelector('[data-op="restore"]');
      btn.disabled = true;
      try {
        const res = await API.post("/api/recycle/restore", { ids: [it.id] });
        toast(
          res.restored ? "已还原 " + res.restored + " 项" : "还原失败：" + (res.errors || []).join("；"),
          res.restored ? "ok" : "err"
        );
      } catch (e) {
        toast("还原失败：" + e.message, "err");
      }
      await this.refresh();
    };

    row.querySelector('[data-op="delete"]').onclick = () => this.deleteOne(it);
    return row;
  },

  /** 彻底删除单条（需输入 yes 确认） */
  async deleteOne(it) {
    const ok = await window.promptDialog({
      title: "彻底删除",
      message: "将从磁盘永久删除：" + it.filename + "（不可还原）。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/recycle/delete", { ids: [it.id] });
      toast(res.deleted ? "已彻底删除 " + res.deleted + " 项" : "删除失败", res.deleted ? "ok" : "err");
    } catch (e) {
      toast("删除失败：" + e.message, "err");
    }
    await this.refresh();
  },

  /** 清空回收站（需输入 yes 两次确认） */
  async clearAll() {
    if (!this._items.length) {
      toast("回收站为空", "err");
      return;
    }
    const ok1 = await window.promptDialog({
      title: "清空回收站",
      message: "将彻底删除回收站内 " + this._items.length + " 项（不可还原）。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok1 !== "yes") return;
    const ok2 = await window.promptDialog({
      title: "再次确认清空",
      message: "这是第二次确认：这些文件将被永久删除且不可恢复。再输入一次 yes：",
      placeholder: "yes",
    });
    if (ok2 !== "yes") return;
    const ids = this._items.map((it) => it.id);
    try {
      const res = await API.post("/api/recycle/delete", { ids });
      toast(res.deleted ? "已彻底删除 " + res.deleted + " 项" : "删除失败", res.deleted ? "ok" : "err");
    } catch (e) {
      toast("清空失败：" + e.message, "err");
    }
    await this.refresh();
  },
};
