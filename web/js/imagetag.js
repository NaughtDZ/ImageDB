/* ============================================================
 * 标签侧车（.imgtag）导出 / 导入 —— 数据迁移兼容
 *
 * 功能：
 *   1. 导出：把选中项(或当前目录树)的标签写入各目录的 .imgtag（每目录一个 sqlite），
 *      便于迁移目录后不依赖 AI 重打标；
 *   2. 导入：读取目录树下的 .imgtag，把标签导回主库(source=import，不覆盖手动标签)。
 *
 * 说明：日常标签读写始终以 data/imagedb.sqlite 主库为准；.imgtag 仅在此显式导出/导入时使用。
 * ============================================================ */
const Imagetag = {
  /** 导出标签到 .imgtag */
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
    try {
      const res = await API.post("/api/tags/export", { scope_type: scope, scope_ids: ids });
      toast("导出完成：目录 " + res.dirs + " 个，媒体 " + res.media + " 个", "ok");
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  },

  /** 从 .imgtag 导入标签回主库 */
  async importTags() {
    if (!App.state.currentFolderId) { toast("请在左侧选择一个目录再导入", "err"); return; }
    const ok = await promptDialog({
      title: "从 .imgtag 导入标签",
      message: "将读取当前目录树下的 .imgtag，把标签导入主库（source=import，不覆盖手动标签）。继续吗？输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/tags/import", { folder_id: App.state.currentFolderId, overwrite: false });
      toast("导入完成：匹配 " + res.media + " 个媒体，新增 " + res.tags + " 个标签", "ok");
      await Gallery.load(true);
      await SidePanel.refresh();
    } catch (e) { toast("导入失败：" + e.message, "err"); }
  },
};
