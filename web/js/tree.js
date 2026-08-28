/* ============================================================
 * 目录树组件
 * 职责：
 *   1. 从后端 /api/tree 读取目录树（纯数据库构建，启动优先）；
 *   2. 渲染树形结构，点击节点切换筛选；
 *   3. 右键菜单：重新扫描 / 从库中移除 / 校验缺失 / 整个目录打标 / 添加图片；
 *   4. 点击目录时先校验磁盘是否存在，不存在则自动清理并刷新；
 *   5. 展开状态保存在 App.state.expandedFolders，点击节点或重新渲染
 *      都不会丢失已展开的子目录（修复"点子目录整树收起"问题）。
 * ============================================================ */
const TreeView = {
  /** 刷新目录树（重新请求后端） */
  async refresh() {
    try {
      const data = await API.get("/api/tree");
      App.state.tree = data.tree || [];
      this.render();
    } catch (e) {
      toast("加载目录树失败：" + e.message, "err");
    }
  },

  /** 渲染目录树 */
  render() {
    const box = document.getElementById("tree");
    box.innerHTML = "";
    // “全部媒体”根节点
    const allNode = document.createElement("div");
    allNode.className = "tree-node" + (App.state.currentFolderId === null ? " active" : "");
    allNode.dataset.folderId = "all";
    allNode.innerHTML = '<span class="caret">▾</span><span class="label">📁 全部媒体</span>';
    allNode.onclick = () => {
      App.state.currentFolderId = null;
      Gallery.load(true);
      this.updateActiveHighlight(null);
    };
    box.appendChild(allNode);

    if (App.state.tree.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "尚未导入目录，点击右上角 ＋ 添加";
      empty.style.padding = "20px";
      box.appendChild(empty);
      return;
    }
    for (const root of App.state.tree) {
      this.renderNode(box, root, 0);
    }
  },

  /** 递归渲染单个节点（按 expandedFolders 恢复展开状态） */
  renderNode(parent, node, depth) {
    const hasKids = node.children && node.children.length > 0;
    const isExpanded = App.state.expandedFolders.has(node.id);
    const div = document.createElement("div");
    div.className = "tree-node" + (App.state.currentFolderId === node.id ? " active" : "");
    div.dataset.folderId = node.id;
    div.style.paddingLeft = (6 + depth * 14) + "px";
    div.innerHTML =
      '<span class="caret">' + (hasKids ? (isExpanded ? "▾" : "▸") : "·") + "</span>" +
      '<span class="label" title="' + escapeHtml(node.path) + '">📁 ' + escapeHtml(node.name) + "</span>" +
      '<span class="count">' + (node.media_count || "") + "</span>";
    // 点击目录：先校验磁盘存在性（不存在则后端自动清理），再切换筛选
    div.onclick = async (e) => {
      e.stopPropagation();
      try {
        const res = await API.post("/api/library/check", { folder_id: node.id });
        if (!res.exists) {
          toast("目录已不存在，已自动从库中移除", "err");
          await this.refresh();
          await Gallery.load(true);
          return;
        }
      } catch (err) { /* 网络错误不阻塞切换 */ }
      App.state.currentFolderId = node.id;
      Gallery.load(true);
      // 只更新高亮，不重建整树（保留展开状态）
      this.updateActiveHighlight(node.id);
      SidePanel.refresh();
    };
    // 右键菜单
    div.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      this.showContextMenu(e, node);
    };
    parent.appendChild(div);

    // 子节点容器（按展开状态显示）
    if (hasKids) {
      const kids = document.createElement("div");
      kids.className = "tree-children" + (isExpanded ? "" : " hidden");
      kids.dataset.parent = node.id;
      parent.appendChild(kids);
      // 展开时渲染子节点
      if (isExpanded) {
        for (const child of node.children) this.renderNode(kids, child, depth + 1);
      }
      // 点击箭头展开/折叠（维护 expandedFolders 状态）
      div.querySelector(".caret").onclick = (e) => {
        e.stopPropagation();
        const willExpand = kids.classList.toggle("hidden");
        div.querySelector(".caret").textContent = willExpand ? "▸" : "▾";
        if (willExpand) {
          App.state.expandedFolders.delete(node.id);
        } else {
          App.state.expandedFolders.add(node.id);
          if (kids.children.length === 0) {
            for (const child of node.children) this.renderNode(kids, child, depth + 1);
          }
        }
      };
    }
  },

  /** 只更新目录树的高亮选中态（不重建整树） */
  updateActiveHighlight(folderId) {
    document.querySelectorAll("#tree .tree-node").forEach(n => {
      const target = folderId === null ? "all" : String(folderId);
      const shouldActive = n.dataset.folderId === target;
      if (n.classList.contains("active") !== shouldActive) {
        n.classList.toggle("active", shouldActive);
      }
    });
  },

  /** 右键菜单 */
  showContextMenu(e, node) {
    document.getElementById("context-menu")?.remove();
    const menu = document.createElement("div");
    menu.id = "context-menu";
    menu.className = "context-menu";
    menu.style.left = e.clientX + "px";
    menu.style.top = e.clientY + "px";

    const items = [
      { label: "查看此目录", fn: () => {
        App.state.currentFolderId = node.id;
        Gallery.load(true);
        this.updateActiveHighlight(node.id);
      } },
      { label: "重新扫描", fn: () => this.rescan(node) },
      { label: "整个目录打标…", fn: () => {
        App.state.currentFolderId = node.id;
        Tagger.openWithFolder(node.id);
      } },
      { label: "管理目录标签…", fn: () => FolderTags.open(node) },
      { label: "添加图片到该目录…", fn: () => this.addMedia(node) },
      { label: "校验缺失", fn: () => this.verify(node) },
      { label: "从库中移除（仅删记录）", fn: () => this.remove(node) },
    ];
    for (const it of items) {
      const d = document.createElement("div");
      d.className = "item";
      d.textContent = it.label;
      d.onclick = (ev) => { ev.stopPropagation(); menu.remove(); it.fn(); };
      menu.appendChild(d);
    }
    document.body.appendChild(menu);
    // 点击其他位置时由 app.js 的全局监听移除
  },

  /** 向指定目录添加单个图片文件（文件须已存在于磁盘） */
  async addMedia(node) {
    const path = await promptDialog({
      title: "添加图片到「" + node.name + "」",
      message: "输入要添加的图片文件绝对路径（该文件必须已经存在于磁盘上）：",
      placeholder: "例如 D:\\Pictures\\new_photo.jpg",
      okText: "添加",
    });
    if (!path) return;
    try {
      const res = await API.post("/api/media/add", { folder_id: node.id, path });
      toast(res.added ? "已添加图片" : "该路径已在库中", res.added ? "ok" : "");
      await this.refresh();
      // 如果当前正浏览该目录则刷新画廊
      if (App.state.currentFolderId === node.id) await Gallery.load(true);
    } catch (e) {
      toast("添加失败：" + e.message, "err");
    }
  },

  /** 重新扫描某目录 */
  async rescan(node) {
    try {
      const res = await API.post("/api/library/rescan", { folder_id: node.id });
      toast("扫描完成：新增 " + res.added + "，清理缺失 " + res.removed_media, "ok");
      await this.refresh();
      await Gallery.load(true);
    } catch (e) {
      toast("扫描失败：" + e.message, "err");
    }
  },

  /** 校验某目录（不存在则自动清理） */
  async verify(node) {
    try {
      const res = await API.post("/api/library/check", { folder_id: node.id });
      if (res.exists) {
        toast("目录存在，未发现缺失", "ok");
      } else {
        toast("目录已不存在，已自动清理（媒体 " + res.removed_media + " 个）", "err");
      }
      await this.refresh();
    } catch (e) {
      toast("校验失败：" + e.message, "err");
    }
  },

  /** 从库中移除某目录（不动磁盘文件） */
  async remove(node) {
    const ok = await promptDialog({
      title: "确认移除",
      message: "仅从数据库中移除目录 “" + node.name + "” 及其记录，磁盘文件不受影响。输入 yes 确认：",
      placeholder: "yes",
    });
    if (ok !== "yes") return;
    try {
      const res = await API.post("/api/library/remove", { folder_id: node.id });
      toast("已移除，清理媒体记录 " + res.removed_media + " 个", "ok");
      if (App.state.currentFolderId === node.id) App.state.currentFolderId = null;
      await this.refresh();
      await Gallery.load(true);
    } catch (e) {
      toast("移除失败：" + e.message, "err");
    }
  },
};
