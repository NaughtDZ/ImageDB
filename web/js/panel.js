/* ============================================================
 * 右侧标签详情侧边栏
 *
 * 功能：
 *   1. 点击缩略图（单选/多选）时，自动显示所选素材的标签；
 *   2. 多选时按“并集去重”展示所有素材的标签；
 *   3. 点击标签 = 用该标签筛选画廊；
 *   4. 每个标签可重命名 / 删除（全局生效：应用到所有含有该标签的素材）；
 *   5. 输入框添加新标签（应用到所有已选素材）。
 * ============================================================ */
const SidePanel = {
  /** 打开侧边栏 */
  open() {
    const sp = document.getElementById("side-panel");
    const saved = parseInt(localStorage.getItem("imagedb.panelWidth") || "260", 10);
    sp.style.width = (isNaN(saved) ? 260 : saved) + "px";
    sp.style.minWidth = "220px";
    sp.classList.add("open");
  },

  /** 关闭侧边栏 */
  close() {
    const sp = document.getElementById("side-panel");
    sp.classList.remove("open");
    sp.style.width = "";
    sp.style.minWidth = "";
  },

  /** 是否可见 */
  isOpen() {
    return document.getElementById("side-panel").classList.contains("open");
  },

  /**
   * 刷新：根据当前选中集合重新聚合标签并渲染。
   * 在 Gallery 选择变化时调用。
   */
  async refresh() {
    // 附加数据面板同步刷新（独立模块，独立处理，互不干扰）
    MetadataPanel.refresh([...App.state.selected]);
    // 收集选中项的标签（并集去重）
    const sel = [...App.state.selected];
    if (sel.length === 0) {
      this.renderEmpty();
      return;
    }
    // 从当前已加载列表收集标签（无需请求后端，速度快）
    const tagMap = new Map();  // name -> {sources:Set, count:number, confidence:number}
    for (const item of App.state.items) {
      if (!sel.includes(item.id)) continue;
      if (!item.tags) continue;
      for (const t of item.tags) {
        if (!tagMap.has(t.name)) {
          tagMap.set(t.name, { sources: new Set(), count: 0, confidence: 0 });
        }
        const rec = tagMap.get(t.name);
        rec.sources.add(t.source || "manual");
        rec.count++;
        rec.confidence = Math.max(rec.confidence, t.confidence || 0);
      }
    }
    this.renderList(sel, tagMap);
  },

  /** 空选择状态 */
  renderEmpty() {
    const list = document.getElementById("panel-tags-list");
    const info = document.getElementById("panel-target-info");
    const empty = document.getElementById("panel-empty");
    info.textContent = "未选择素材";
    list.innerHTML = "";
    empty.classList.remove("hidden");
  },

  /** 渲染标签列表 */
  renderList(sel, tagMap) {
    const info = document.getElementById("panel-target-info");
    const list = document.getElementById("panel-tags-list");
    const empty = document.getElementById("panel-empty");

    info.textContent = "已选 " + sel.length + " 个素材 · 共 " + tagMap.size + " 个标签";
    empty.classList.add("hidden");
    list.innerHTML = "";

    if (tagMap.size === 0) {
      list.innerHTML = '<div class="hint">选中素材暂无标签，可在上方输入框添加。</div>';
      return;
    }

    // 按出现次数降序排列
    const entries = [...tagMap.entries()].sort((a, b) => b[1].count - a[1].count);
    for (const [name, rec] of entries) {
      const chip = document.createElement("span");
      chip.className = "tag-chip panel-tag";
      chip.dataset.tag = name;
      chip.title = "点击筛选该标签";
      chip.innerHTML =
        '<span class="t-name">' + escapeHtml(name) + "</span>" +
        '<span class="src">×' + rec.count +
        (rec.sources.size > 1 ? " (" + rec.sources.size + "源)" : "") +
        (rec.confidence > 0 && rec.confidence < 1 ? " " + (rec.confidence * 100).toFixed(0) + "%" : "") +
        "</span>" +
        '<span class="ops">' +
        '  <span class="op rename" title="重命名标签（应用到所有含该标签的素材）">✎</span>' +
        '  <span class="op del" title="删除标签（应用到所有含该标签的素材）">✕</span>' +
        "</span>";

      // 点击标签主体：按该标签筛选画廊
      chip.querySelector(".t-name").onclick = (e) => {
        e.stopPropagation();
        document.getElementById("search-tags").value = name;
        App.applyFilters();
      };

      // 重命名
      chip.querySelector(".op.rename").onclick = async (e) => {
        e.stopPropagation();
        const newName = await promptDialog({
          title: "重命名标签",
          message: "标签「" + name + "」将重命名为（应用到所有含该标签的素材）：",
          placeholder: name,
        });
        if (!newName || newName === name) return;
        try {
          await API.post("/api/tags/rename", { old_name: name, new_name: newName });
          toast("标签已重命名为：" + newName, "ok");
          await this.afterMutation();
        } catch (err) {
          toast("重命名失败：" + err.message, "err");
        }
      };

      // 删除
      chip.querySelector(".op.del").onclick = async (e) => {
        e.stopPropagation();
        const ok = await promptDialog({
          title: "确认删除标签",
          message: "标签「" + name + "」将被删除，所有含有该标签的素材都会移除它。输入 yes 确认：",
          placeholder: "yes",
        });
        if (ok !== "yes") return;
        try {
          await API.post("/api/tags/delete", { name });
          toast("标签已删除：" + name, "ok");
          await this.afterMutation();
        } catch (err) {
          toast("删除失败：" + err.message, "err");
        }
      };

      list.appendChild(chip);
    }
  },

  /** 标签变更后：刷新侧边栏 + 画廊（标签数据变化） */
  async afterMutation() {
    await Gallery.load(true);   // 重新加载画廊（含最新标签）
    await this.refresh();
  },

  /** 添加标签到所有选中项 */
  async addTag() {
    const input = document.getElementById("panel-tag-input");
    const name = input.value.trim();
    const sel = [...App.state.selected];
    if (!name) return;
    if (sel.length === 0) { toast("请先选择素材", "err"); return; }
    try {
      await API.post("/api/media/tags/batch", {
        media_ids: sel, tags: [name], action: "add",
      });
      input.value = "";
      toast("已添加标签：" + name, "ok");
      await this.afterMutation();
    } catch (e) {
      toast("添加失败：" + e.message, "err");
    }
  },

  /** 加载标签自动补全（从已有标签中联想） */
  async loadSuggestions() {
    try {
      const data = await API.get("/api/tags?limit=500");
      const dl = document.getElementById("panel-tags-suggest");
      dl.innerHTML = "";
      for (const t of data.tags) {
        const opt = document.createElement("option");
        opt.value = t.name;
        dl.appendChild(opt);
      }
    } catch (e) { /* 忽略 */ }
  },

  /** 初始化事件 */
  init() {
    MetadataPanel.init();
    document.getElementById("btn-panel-close").onclick = () => this.close();
    document.getElementById("btn-panel-add-tag").onclick = () => this.addTag();
    document.getElementById("panel-tag-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.addTag();
    });
    this.loadSuggestions();
  },
};
