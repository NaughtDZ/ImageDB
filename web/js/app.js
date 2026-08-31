/* ============================================================
 * 应用主模块：全局状态 + 初始化 + 顶栏事件
 * ============================================================ */
const App = {
  // 全局状态
  state: {
    tree: [],              // 目录树（后端返回）
    currentFolderId: null, // 当前选中的目录 id（null = 全部）
    items: [],             // 当前画廊展示的媒体
    total: 0,              // 当前筛选条件下的总数
    page: 1,               // 当前页码
    pageSize: 60,
    selected: new Set(),   // 选中的媒体 id 集合
    lastAnchor: null,      // 上次单击的媒体 id（Shift 区间选择的锚点）
    expandedFolders: new Set(),  // 目录树展开状态（展开的节点 id 集合）
    filters: { q: "", dir: "", tags: "", type: "" },
  },

  /** 应用初始化 */
  async init() {
    this.bindEvents();
    // 启动时优先读取数据库构建目录树
    await TreeView.refresh();
    // 加载第一页媒体
    await Gallery.load(true);
    // 初始化查看器键盘事件
    Viewer.initKeyboard();
    // 初始化各对话框组件
    Tagger.init();
    TagsEditor.init();
    Settings.init();
    // 初始化画廊框选
    Gallery.initBoxSelect();
    // 初始化分页导航
    Gallery.initPager();
    // 初始化缩略图大小滑块
    Gallery.initThumbSlider();
    // 初始化右侧标签侧边栏
    SidePanel.init();
    Imagetag.init();
    // 初始化目录标签管理
    FolderTags.init();
    Recycle.init();
  },

  /** 绑定顶栏与全局事件 */
  bindEvents() {
    // 搜索
    document.getElementById("btn-search").onclick = () => this.applyFilters();
    document.getElementById("btn-reset").onclick = () => {
      document.getElementById("search-q").value = "";
      document.getElementById("search-dir").value = "";
      document.getElementById("search-tags").value = "";
      document.getElementById("search-type").value = "";
      this.applyFilters();
    };
    // 回车触发搜索
    ["search-q", "search-dir", "search-tags"].forEach(id => {
      document.getElementById(id).addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.applyFilters();
      });
    });

    // 顶栏按钮
    document.getElementById("btn-import").onclick = () => this.importFolder();
    document.getElementById("btn-rescan").onclick = () => this.rescanCurrent();
    document.getElementById("btn-verify").onclick = () => this.verifyAll();
    document.getElementById("btn-settings").onclick = () => Settings.open();
    document.getElementById("btn-add-folder").onclick = () => this.importFolder();

    // 工具栏
    document.getElementById("btn-tag").onclick = () => Tagger.open();
    document.getElementById("btn-view").onclick = () => this.openViewer();
    document.getElementById("btn-manual-tags").onclick = () => TagsEditor.open();
    document.getElementById("btn-export-tags").onclick = () => Imagetag.exportTags();
    document.getElementById("btn-import-tags").onclick = () => Imagetag.importTags();
    document.getElementById("btn-selfcheck-tags").onclick = () => Imagetag.selfCheck();
    document.getElementById("btn-clear-sel").onclick = () => Gallery.clearSelection();
    document.getElementById("btn-delete-sel").onclick = () => this.deleteSelected();
    document.getElementById("btn-trash-sel").onclick = () => this.trashSelected();
    document.getElementById("btn-move-sel").onclick = () => this.moveSelected();
    document.getElementById("btn-recycle").onclick = () => Recycle.open();

    document.getElementById("sort-select").onchange = () => Gallery.load(true);

    // 点击空白处关闭右键菜单
    document.addEventListener("click", () => {
      const m = document.getElementById("context-menu");
      if (m) m.remove();
    });
  },

  /** 从搜索框收集筛选条件并重新加载 */
  applyFilters() {
    this.state.filters = {
      q: document.getElementById("search-q").value.trim(),
      dir: document.getElementById("search-dir").value.trim(),
      // 标签搜索：空格/逗号分隔多个标签，AND 语义（后端处理）
      tags: document.getElementById("search-tags").value.trim(),
      type: document.getElementById("search-type").value,
    };
    Gallery.load(true);
  },

  /** 导入目录 */
  async importFolder() {
    const path = await promptDialog({
      title: "导入目录",
      message: "输入要导入的目录绝对路径（将递归扫描其中的图片/视频路径并入库）：",
      placeholder: "例如 D:\\Pictures\\收藏",
      okText: "导入",
    });
    if (!path) return;
    try {
      // 启动后台导入任务（多线程统计 + 逐目录导入，带进度条）
      const res = await API.post("/api/library/import", { path });
      this.showImportProgress(res.job_id);
    } catch (e) {
      toast("导入启动失败：" + e.message, "err");
    }
  },

  /** 显示导入进度条并轮询进度 */
  showImportProgress(jobId) {
    const bar = document.getElementById("import-progress-bar");
    const fill = document.getElementById("import-progress-fill");
    const text = document.getElementById("import-progress-text");
    bar.classList.remove("hidden");
    fill.style.width = "0%";

    const poll = async () => {
      try {
        const job = await API.get("/api/library/import/jobs/" + jobId);
        fill.style.width = (job.progress || 0) + "%";
        text.textContent = job.message || "";
        if (job.status === "done" || job.status === "failed") {
          bar.classList.add("hidden");
          if (job.status === "done") {
            toast(job.message || "导入完成", "ok");
          } else {
            toast(job.message || "导入失败", "err");
          }
          await TreeView.refresh();
          await Gallery.load(true);
          return;
        }
        setTimeout(poll, 400);
      } catch (e) {
        bar.classList.add("hidden");
        toast("导入进度获取失败：" + e.message, "err");
      }
    };
    poll();
  },

  /** 重新扫描当前目录 */
  async rescanCurrent() {
    if (!this.state.currentFolderId) {
      toast("请先在左侧选择一个目录", "err");
      return;
    }
    try {
      const res = await API.post("/api/library/rescan", { folder_id: this.state.currentFolderId });
      toast("扫描完成：新增 " + res.added + "，清理缺失 " + res.removed_media, "ok");
      await TreeView.refresh();
      await Gallery.load(true);
    } catch (e) {
      toast("扫描失败：" + e.message, "err");
    }
  },

  /** 全库校验缺失 */
  async verifyAll() {
    try {
      const res = await API.post("/api/library/verify", {});
      toast("校验完成：清理目录 " + res.removed_folders + " 个，媒体 " + res.removed_media + " 个", "ok");
      await TreeView.refresh();
      await Gallery.load(true);
    } catch (e) {
      toast("校验失败：" + e.message, "err");
    }
  },

  /** 从数据库中删除选中项（不删除磁盘文件） */
  async deleteSelected() {
    const sel = [...this.state.selected];
    if (sel.length === 0) {
      toast("请先选择要删除的素材", "err");
      return;
    }
    const ok = await promptDialog({
      title: "确认删除",
      message: "将从数据库中删除 " + sel.length + " 个素材的记录（磁盘文件不会被删除）。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/media/delete", { media_ids: sel });
      toast("已删除 " + res.removed + " 个记录", "ok");
      // 清理选择与画廊
      this.state.selected.clear();
      this.state.lastAnchor = null;
      await Gallery.load(true);
      await TreeView.refresh();
      SidePanel.refresh();
    } catch (e) {
      toast("删除失败：" + e.message, "err");
    }
  },

  /** 把选中的素材移到回收站（需输入 yes 确认）。
   * 本地固定盘进系统回收站；网络/可移动盘进"应用内回收站"（可还原）。 */
  async trashSelected() {
    const sel = [...this.state.selected];
    if (sel.length === 0) { toast("请先选择要移到回收站的素材", "err"); return; }
    const ok = await promptDialog({
      title: "移到回收站",
      message: "将把 " + sel.length + " 个素材的文件移到回收站（可从回收站还原），并删除数据库记录。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/media/trash", { media_ids: sel });
      // app_trash>0 表示已移入"应用内回收站"（可从回收站功能还原），否则为系统回收站
      const note = res.app_trash > 0 ? "（可从回收站功能还原）" : "";
      toast((res.message || "已移到回收站") + note, "ok");
      this.state.selected.clear();
      this.state.lastAnchor = null;
      await Gallery.load(true);
      await TreeView.refresh();
      SidePanel.refresh();
    } catch (e) {
      toast("操作失败：" + e.message, "err");
    }
  },

  /** 把选中的素材移动到指定目录（需输入 yes 确认） */
  async moveSelected() {
    const sel = [...this.state.selected];
    if (sel.length === 0) { toast("请先选择要移动的素材", "err"); return; }
    // 先选目标目录
    const dest = await promptDialog({
      title: "移动到目录",
      message: "输入目标目录的绝对路径（必须是已导入的目录）：",
      placeholder: "例如 D:\\Pictures\\收藏",
      okText: "下一步",
    });
    if (!dest) return;
    const ok = await promptDialog({
      title: "确认移动",
      message: "将把 " + sel.length + " 个素材移动到 " + dest + "，并更新数据库记录。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/media/move", { media_ids: sel, dest_dir: dest });
      toast("已移动 " + res.moved + " 个素材", "ok");
      this.state.selected.clear();
      this.state.lastAnchor = null;
      await Gallery.load(true);
      await TreeView.refresh();
      SidePanel.refresh();
    } catch (e) {
      toast("移动失败：" + e.message, "err");
    }
  },

  /** 打开查看器 */
  openViewer() {
    const sel = [...this.state.selected];
    if (this.state.items.length === 0) {
      toast("没有可查看的素材", "err");
      return;
    }
    if (sel.length > 1) {
      // 多选：按选中顺序查看
      const list = this.state.items.filter(i => sel.includes(i.id));
      Viewer.open(list, 0);
    } else if (sel.length === 1) {
      // 单选：仍传入完整列表，但定位到选中项（便于浏览/幻灯片循环）
      const idx = this.state.items.findIndex(i => i.id === sel[0]);
      Viewer.open(this.state.items, idx >= 0 ? idx : 0);
    } else {
      // 未选中：查看当前列表
      Viewer.open(this.state.items, 0);
    }
  },
};

// 页面加载完成后初始化
window.addEventListener("DOMContentLoaded", () => App.init());
