/* ============================================================
 * 手动标签编辑器
 * 职责：对选中的单个/多个媒体手动添加/移除标签。
 * ============================================================ */
const TagsEditor = {
  ids: [],
  _session: 0,
  _refreshSeq: 0,
  _saving: false,
  /** 打开标签编辑器（针对当前选中项） */
  async open() {
    const sel = [...App.state.selected];
    if (sel.length === 0) {
      toast("请先选择要编辑标签的媒体", "err");
      return;
    }
    this.ids = sel;
    const session = ++this._session;
    this.currentTags = new Map();
    document.getElementById("tags-target-info").textContent =
      "正在编辑 " + sel.length + " 个媒体的标签（标签将应用到所有选中项）";
    document.getElementById("tags-list").textContent = "读取中…";
    document.getElementById("tags-modal").classList.remove("hidden");
    this.loadSuggestions();
    try { await this.refreshTags(session); }
    catch (e) {
      if (session === this._session) toast("读取标签失败：" + e.message, "err");
    }
  },

  /** 编辑会话持有固定 ID，不依赖画廊的当前选择或旧标签。 */
  async refreshTags(session) {
    const seq = ++this._refreshSeq;
    const ids = [...this.ids], items = [];
    let next = 0, failed = false;
    const worker = async () => {
      while (next < ids.length && !failed && session === this._session && seq === this._refreshSeq) {
        const id = ids[next++];
        try { items.push(await API.get("/api/media/" + id)); }
        catch (e) { failed = true; throw e; }
      }
    };
    // 大批选择仍需新数据，但最多 8 个并发请求，避免挤占后端线程池。
    await Promise.all(Array.from({ length: Math.min(8, ids.length) }, worker));
    if (session !== this._session || seq !== this._refreshSeq) return;
    const tagCount = new Map();
    for (const item of items) {
      for (const t of item.tags || []) {
        if (!tagCount.has(t.name)) tagCount.set(t.name, { sources: new Set(), count: 0 });
        const rec = tagCount.get(t.name);
        rec.sources.add(t.source);
        rec.count++;
      }
    }
    this.currentTags = tagCount;
    this.renderList();
  },

  /** 加载标签自动补全 */
  async loadSuggestions() {
    try {
      const data = await API.get("/api/tags?limit=500");
      const dl = document.getElementById("tags-suggest");
      dl.innerHTML = "";
      for (const t of data.tags) {
        const opt = document.createElement("option");
        opt.value = t.name;
        dl.appendChild(opt);
      }
    } catch (e) { /* 忽略 */ }
  },

  /** 渲染标签列表 */
  renderList() {
    const box = document.getElementById("tags-list");
    box.innerHTML = "";
    if (!this.currentTags.size) {
      box.innerHTML = '<div class="hint">暂无标签。输入标签名后点击添加。</div>';
      return;
    }
    for (const [name, rec] of this.currentTags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.innerHTML =
        escapeHtml(name) +
        ' <span class="src">' + escapeHtml([...rec.sources].join(",")) +
        " ×" + rec.count + "</span>" +
        ' <span class="del" title="移除该标签">✕</span>';
      chip.querySelector(".del").onclick = () => this.removeTag(name);
      box.appendChild(chip);
    }
  },

  /** 添加标签到会话目标 */
  async addTag() {
    const input = document.getElementById("tags-new");
    const name = input.value.trim();
    if (name) await this.mutateTag(name, false);
  },

  async removeTag(name) { await this.mutateTag(name, true); },

  async mutateTag(name, remove) {
    if (this._saving || !this.ids.length) return;
    const ids = [...this.ids], session = this._session;
    this._saving = true;
    document.getElementById("tags-add").disabled = true;
    try {
      for (const id of ids) {
        await API.post("/api/media/" + id + "/tags" + (remove ? "/remove" : ""), { tags: [name] });
      }
      if (session === this._session) {
        if (!remove) document.getElementById("tags-new").value = "";
        toast((remove ? "已移除标签：" : "已添加标签：") + name, "ok");
      }
    } catch (e) {
      if (session === this._session) toast("标签更新失败（部分目标可能已更新）：" + e.message, "err");
    } finally {
      // 即使部分写入失败，也重新读取会话目标，不能回到旧列表记录。
      if (session === this._session) {
        try {
          await this.refreshTags(session);
          if (session === this._session) await Gallery.load(false, true);
        } catch (e) {
          if (session === this._session) toast("刷新标签失败：" + e.message, "err");
        }
      }
      this._saving = false;
      document.getElementById("tags-add").disabled = false;
    }
  },

  /** 初始化事件 */
  init() {
    document.getElementById("tags-add").onclick = () => this.addTag();
    document.getElementById("tags-new").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.addTag();
    });
    document.querySelectorAll("[data-close='tags-modal']").forEach(el => {
      el.onclick = () => {
        ++this._session;
        this.ids = [];
        document.getElementById("tags-modal").classList.add("hidden");
      };
    });
  },
};