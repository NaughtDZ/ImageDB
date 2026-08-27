/* ============================================================
 * 手动标签编辑器
 * 职责：对选中的单个/多个媒体手动添加/移除标签。
 * ============================================================ */
const TagsEditor = {
  /** 打开标签编辑器（针对当前选中项） */
  async open() {
    const sel = [...App.state.selected];
    if (sel.length === 0) {
      toast("请先选择要编辑标签的媒体", "err");
      return;
    }
    this.ids = sel;
    // 收集选中项的全部标签
    this.currentTags = new Map();  // name -> {sources, count}
    const tagCount = new Map();
    for (const id of sel) {
      const item = App.state.items.find(i => i.id === id);
      if (item && item.tags) {
        for (const t of item.tags) {
          const key = t.name;
          if (!tagCount.has(key)) tagCount.set(key, { name: key, sources: new Set(), count: 0 });
          const rec = tagCount.get(key);
          rec.sources.add(t.source);
          rec.count++;
        }
      }
    }
    this.currentTags = tagCount;

    document.getElementById("tags-target-info").textContent =
      "正在编辑 " + sel.length + " 个媒体的标签（标签将应用到所有选中项）";
    this.renderList();
    this.loadSuggestions();
    document.getElementById("tags-modal").classList.remove("hidden");
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
        ' <span class="src">' + [...rec.sources].join(",") +
        " ×" + rec.count + "</span>" +
        ' <span class="del" title="移除该标签">✕</span>';
      chip.querySelector(".del").onclick = () => this.removeTag(name);
      box.appendChild(chip);
    }
  },

  /** 添加标签到所有选中项 */
  async addTag() {
    const input = document.getElementById("tags-new");
    const name = input.value.trim();
    if (!name) return;
    try {
      for (const id of this.ids) {
        await API.post("/api/media/" + id + "/tags", { tags: [name] });
      }
      input.value = "";
      toast("已添加标签：" + name, "ok");
      await this.open();  // 重新加载
      Gallery.load(true);
    } catch (e) {
      toast("添加失败：" + e.message, "err");
    }
  },

  /** 移除标签 */
  async removeTag(name) {
    try {
      for (const id of this.ids) {
        await API.post("/api/media/" + id + "/tags/remove", { tags: [name] });
      }
      toast("已移除标签：" + name, "ok");
      await this.open();
      Gallery.load(true);
    } catch (e) {
      toast("移除失败：" + e.message, "err");
    }
  },

  /** 初始化事件 */
  init() {
    document.getElementById("tags-add").onclick = () => this.addTag();
    document.getElementById("tags-new").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.addTag();
    });
    document.querySelectorAll("[data-close='tags-modal']").forEach(el => {
      el.onclick = () => document.getElementById("tags-modal").classList.add("hidden");
    });
  },
};