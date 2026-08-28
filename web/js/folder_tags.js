/* ============================================================
 * 目录标签管理组件
 * 职责：对某个目录（含全部子目录）的所有文件标签进行批量管理。
 *   - 显示该目录下所有标签的聚合（名称 + 覆盖的媒体数）；
 *   - 对整个目录批量添加标签；
 *   - 对整个目录批量移除某标签；
 *   - 全局重命名 / 删除标签（应用到所有含该标签的素材）。
 * 解决"画廊一页只能显示部分图片，无法管理整目录标签"的问题。
 * ============================================================ */
const FolderTags = {
  folderId: null,

  /** 打开目录标签管理（树右键菜单调用） */
  async open(node) {
    this.folderId = node.id;
    document.getElementById("folder-tags-title").textContent =
      "（" + node.name + "）";
    document.getElementById("folder-tag-input").value = "";
    document.getElementById("folder-tags-modal").classList.remove("hidden");
    await this.refresh();
    await this.loadSuggestions();
  },

  /** 刷新标签聚合列表 */
  async refresh() {
    try {
      const data = await API.get("/api/library/" + this.folderId + "/tags");
      const tags = data.tags || [];
      document.getElementById("folder-tags-info").textContent =
        "共 " + tags.length + " 个标签 · " +
        (tags.length ? "（按覆盖媒体数排序）" : "该目录暂无标签");
      const list = document.getElementById("folder-tags-list");
      list.innerHTML = "";
      if (!tags.length) {
        list.innerHTML = '<div class="hint">该目录（含子目录）暂无标签，可在上方输入框添加。</div>';
        return;
      }
      for (const t of tags) {
        list.appendChild(this.renderTagRow(t));
      }
    } catch (e) {
      toast("加载目录标签失败：" + e.message, "err");
    }
  },

  /** 渲染单个标签行 */
  renderTagRow(t) {
    const row = document.createElement("div");
    row.className = "tag-row";
    row.style.cssText =
      'display:flex;align-items:center;gap:10px;padding:6px 10px;' +
      'border:1px solid var(--border);border-radius:6px;background:var(--bg-hover)';
    row.innerHTML =
      '<span style="white-space:normal;word-break:break-all">' + escapeHtml(t.name) + "</span>" +
      '<span style="color:var(--text-dim);font-size:12px;white-space:nowrap">×' + t.media_count + "</span>" +
      '<span style="margin-left:auto;display:flex;gap:6px">' +
      '  <span class="op" data-op="remove" title="从该目录所有文件移除此标签" style="cursor:pointer;color:var(--text-dim)">🗑 从目录移除</span>' +
      '  <span class="op" data-op="rename" title="全局重命名（所有含此标签的素材）" style="cursor:pointer;color:var(--text-dim)">✎ 重命名</span>' +
      '  <span class="op" data-op="delete" title="全局删除（所有含此标签的素材）" style="cursor:pointer;color:var(--text-dim)">✕ 删除</span>' +
      "</span>";

    // 从目录移除（批量）
    row.querySelector('[data-op="remove"]').onclick = async (e) => {
      e.stopPropagation();
      await this.removeFromFolder(t.name);
    };
    // 全局重命名
    row.querySelector('[data-op="rename"]').onclick = async (e) => {
      e.stopPropagation();
      await this.renameTag(t.name);
    };
    // 全局删除
    row.querySelector('[data-op="delete"]').onclick = async (e) => {
      e.stopPropagation();
      await this.deleteTag(t.name);
    };
    return row;
  },

  /** 从该目录所有文件移除某标签 */
  async removeFromFolder(name) {
    const ok = await promptDialog({
      title: "从目录移除标签",
      message: "将从「" + document.getElementById("folder-tags-title").textContent +
               "」目录的所有文件中移除标签「" + name + "」。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      await API.post("/api/library/" + this.folderId + "/tags", {
        tags: [name], action: "remove",
      });
      toast("已从目录移除标签：" + name, "ok");
      await this.refresh();
    } catch (e) { toast("移除失败：" + e.message, "err"); }
  },

  /** 全局重命名标签 */
  async renameTag(oldName) {
    const newName = await promptDialog({
      title: "重命名标签",
      message: "标签「" + oldName + "」将全局重命名为（应用到所有含该标签的素材）：",
      placeholder: oldName,
    });
    if (!newName || newName === oldName) return;
    try {
      await API.post("/api/tags/rename", { old_name: oldName, new_name: newName });
      toast("已重命名为：" + newName, "ok");
      await this.refresh();
    } catch (e) { toast("重命名失败：" + e.message, "err"); }
  },

  /** 全局删除标签 */
  async deleteTag(name) {
    const ok = await promptDialog({
      title: "删除标签",
      message: "标签「" + name + "」将被删除，所有含该标签的素材都会移除它。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      await API.post("/api/tags/delete", { name });
      toast("已删除标签：" + name, "ok");
      await this.refresh();
    } catch (e) { toast("删除失败：" + e.message, "err"); }
  },

  /** 添加标签到目录所有文件 */
  async addToFolder() {
    const input = document.getElementById("folder-tag-input");
    const name = input.value.trim();
    if (!name) return;
    try {
      await API.post("/api/library/" + this.folderId + "/tags", {
        tags: [name], action: "add",
      });
      input.value = "";
      toast("已添加标签：" + name, "ok");
      await this.refresh();
    } catch (e) { toast("添加失败：" + e.message, "err"); }
  },

  /** 加载标签自动补全 */
  async loadSuggestions() {
    try {
      const data = await API.get("/api/tags?limit=500");
      const dl = document.getElementById("folder-tags-suggest");
      dl.innerHTML = "";
      for (const t of data.tags) {
        const opt = document.createElement("option");
        opt.value = t.name;
        dl.appendChild(opt);
      }
    } catch (e) {}
  },

  /** 初始化事件 */
  init() {
    document.getElementById("btn-folder-tag-add").onclick = () => this.addToFolder();
    document.getElementById("folder-tag-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.addToFolder();
    });
    document.querySelectorAll("[data-close='folder-tags-modal']").forEach(el => {
      el.onclick = () => document.getElementById("folder-tags-modal").classList.add("hidden");
    });
  },
};
