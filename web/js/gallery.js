/* ============================================================
 * 画廊组件：媒体网格、选择（单击/Ctrl/Shift/框选）、分页加载
 *
 * 选择交互：
 *   单击        选中单项（清空其他）
 *   Ctrl/⌘+单击 切换单项
 *   Shift+单击   区间选择（从锚点到当前项，可连续 Shift 扩展）
 *   空白处拖拽   框选（矩形范围内的所有卡片）
 *   空白处单击   清空选择
 * ============================================================ */
const Gallery = {
  /** 加载媒体列表（reset=true 时从第一页重新加载） */
  async load(reset) {
    const f = App.state.filters;
    const params = new URLSearchParams();
    if (App.state.currentFolderId) params.set("folder_id", App.state.currentFolderId);
    if (f.q) params.set("q", f.q);
    if (f.dir) params.set("dir_q", f.dir);
    if (f.tags) params.set("tags", f.tags);
    if (f.type) params.set("type", f.type);
    params.set("page", App.state.page);
    params.set("page_size", App.state.pageSize);
    params.set("sort", document.getElementById("sort-select").value);

    try {
      const data = await API.get("/api/media?" + params.toString());
      App.state.total = data.total;
      App.state.items = reset ? data.items : App.state.items.concat(data.items);
      if (reset) App.state.page = 1;
      this.render();
      this.updateLoadMore();
      this.updateResultInfo();
    } catch (e) {
      // 文件被外部删除时后端返回 410 并自动清理，这里刷新树
      if (e.status === 410) {
        toast("部分文件已被外部删除，记录已自动清理", "err");
        TreeView.refresh();
        this.load(reset);
      } else {
        toast("加载失败：" + e.message, "err");
      }
    }
  },

  /** 渲染网格 */
  render() {
    const box = document.getElementById("gallery");
    box.innerHTML = "";
    if (!App.state.items.length) {
      box.innerHTML = '<div class="empty">没有找到素材 —— 试试导入目录或调整筛选条件</div>';
      return;
    }
    for (const item of App.state.items) {
      box.appendChild(this.renderCard(item));
    }
    // 注册按需加载观察器（延迟到布局完成后，确保元素有正确位置）
    requestAnimationFrame(() => this.observeThumbs());
    // 兜底：500ms 后若仍无任何图片加载（观察器异常），直接加载可见区图片
    clearTimeout(this._observeFallback);
    this._observeFallback = setTimeout(() => {
      const thumbs = document.querySelectorAll(".thumb-img[data-src]");
      const anyLoaded = document.querySelector(".thumb-img[src]");
      if (thumbs.length && !anyLoaded) {
        // 观察器完全失效时，加载前 N 张（视口大致能容纳的数量）
        const visible = Math.ceil(window.innerHeight / 180) * 6 + 6;
        thumbs.forEach((im, i) => {
          if (i < visible && im.dataset.src) {
            im.src = im.dataset.src;
            delete im.dataset.src;
          }
        });
      }
    }, 1500);
  },

  /**
   * 按需渲染核心：用 IntersectionObserver 观察缩略图，
   * 只有进入视口（或接近视口）的图片才真正请求加载。
   * 滚出视口的图片自动移除 src 释放内存（可选，这里保留已加载的）。
   */
  observeThumbs() {
    const galleryEl = document.getElementById("gallery");
    if (!galleryEl || !("IntersectionObserver" in window)) {
      // 不支持 IntersectionObserver 的浏览器：直接加载全部（退化）
      document.querySelectorAll(".thumb-img[data-src]").forEach(im => {
        im.src = im.dataset.src;
        delete im.dataset.src;
      });
      return;
    }
    // 复用单个观察器实例（避免每次 render 重建）
    if (!this._thumbObserver) {
      this._thumbObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const im = entry.target;
          if (entry.isIntersecting && im.dataset.src) {
            // 进入视口：加载图片（用 requestAnimationFrame 错峰，避免同批瞬间全部请求）
            requestAnimationFrame(() => {
              if (im.dataset.src) {
                im.src = im.dataset.src;
                delete im.dataset.src;
              }
            });
            this._thumbObserver.unobserve(im);  // 加载后不再观察
          }
        }
      }, {
        // root 不指定（用浏览器视口），对滚动容器同样有效且更稳定
        rootMargin: "200px 0px",   // 提前 200px 预载，滚动顺滑
        threshold: 0.01,
      });
    }
    // 立即检查：已经可见的元素（可能在 viewport 内但回调延迟）直接加载
    for (const im of document.querySelectorAll(".thumb-img[data-src]")) {
      const r = im.getBoundingClientRect();
      const winH = window.innerHeight || 0;
      if (r.top < winH + 200 && r.bottom > -200) {
        // 元素已在视口附近：直接加载，不等观察器回调
        im.src = im.dataset.src;
        delete im.dataset.src;
      } else {
        this._thumbObserver.observe(im);
      }
    }
    // 观察所有未加载的缩略图
    document.querySelectorAll(".thumb-img[data-src]").forEach(im => {
      this._thumbObserver.observe(im);
    });
  },

  /** 渲染单个媒体卡片 */
  renderCard(item) {
    const card = document.createElement("div");
    card.className = "media-card" + (App.state.selected.has(item.id) ? " selected" : "");
    card.dataset.id = item.id;

    // 缩略图：按需渲染（data-src 存地址，IntersectionObserver 进入视口才真正加载）
    // 避免"加载更多"时一次性请求大量图片导致卡顿
    const thumbBox = document.createElement("div");
    thumbBox.className = "thumb-box";
    const img = document.createElement("img");
    img.alt = item.filename;
    img.className = "thumb-img";
    img.dataset.src = "/api/media/" + item.id + "/thumbnail";
    img.onerror = () => {
      // 缩略图失败时显示占位（文件可能已删除）
      img.src = "";
      img.style.background = "var(--bg-hover)";
      img.alt = "⚠";
    };
    thumbBox.appendChild(img);
    card.appendChild(thumbBox);

    // 类型徽标
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = item.type === "video" ? "🎬" : "🖼";
    card.appendChild(badge);

    // 视频时长
    if (item.type === "video" && item.duration) {
      const dur = document.createElement("span");
      dur.className = "dur";
      dur.textContent = formatDuration(item.duration);
      card.appendChild(dur);
    }

    // 文件名
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = item.filename;
    name.title = item.path;
    card.appendChild(name);

    // ---------- 单击选择：支持 Ctrl 切换 / Shift 区间 ----------
    card.onclick = (e) => {
      e.stopPropagation();
      if (e.shiftKey) {
        // Shift：区间选择（从锚点到当前项；无锚点则只选当前项）
        this.selectRange(item.id);
      } else if (e.ctrlKey || e.metaKey) {
        // Ctrl/⌘：切换单项，并更新锚点
        if (App.state.selected.has(item.id)) App.state.selected.delete(item.id);
        else App.state.selected.add(item.id);
        App.state.lastAnchor = item.id;
        card.classList.toggle("selected");
      } else {
        // 普通单击：清空后单选，更新锚点
        App.state.selected.clear();
        App.state.selected.add(item.id);
        App.state.lastAnchor = item.id;
        this.render();  // 刷新高亮
      }
      this.updateSelInfo();
      // 点击缩略图即刷新右侧标签侧边栏（并集去重展示）
      SidePanel.open();
      SidePanel.refresh();
    };

    // ---------- 双击打开查看器（传入完整列表，便于幻灯片/翻页） ----------
    card.ondblclick = (e) => {
      e.stopPropagation();
      // 定位当前项在列表中的下标，传入整个当前列表供浏览
      const idx = App.state.items.findIndex(i => i.id === item.id);
      Viewer.open(App.state.items, idx >= 0 ? idx : 0);
    };

    return card;
  },

  /** Shift 区间选择：从锚点到当前项之间的所有项 */
  selectRange(itemId) {
    const ids = App.state.items.map(i => i.id);
    const curIdx = ids.indexOf(itemId);
    if (curIdx < 0) return;
    // 锚点不存在时，把当前项当作锚点
    let anchorIdx = ids.indexOf(App.state.lastAnchor);
    if (anchorIdx < 0) anchorIdx = curIdx;
    const [a, b] = curIdx < anchorIdx ? [curIdx, anchorIdx] : [anchorIdx, curIdx];
    for (let i = a; i <= b; i++) {
      App.state.selected.add(ids[i]);
    }
    this.render();
    this.updateSelInfo();
  },

  /**
   * 缩略图大小滑块：调节 grid 列宽（CSS 变量 --thumb-size），并持久化到 localStorage。
   */
  initThumbSlider() {
    const slider = document.getElementById("thumb-size-slider");
    if (!slider || slider.dataset.inited) return;
    slider.dataset.inited = "1";
    const galleryEl = document.getElementById("gallery");

    // 读取上次保存的值
    const saved = localStorage.getItem("imagedb_thumb_size");
    if (saved) {
      const v = parseInt(saved, 10);
      if (v >= 100 && v <= 400) {
        slider.value = v;
        galleryEl.style.setProperty("--thumb-size", v + "px");
      }
    }

    // 拖动时实时生效
    slider.addEventListener("input", () => {
      const v = parseInt(slider.value, 10) || 160;
      galleryEl.style.setProperty("--thumb-size", v + "px");
      localStorage.setItem("imagedb_thumb_size", String(v));
    });
  },

  /**
   * 框选：在画廊空白处按下鼠标拖拽，框选矩形范围内的卡片。
   * 在 app 初始化时调用一次即可（#gallery 是常驻元素）。
   */
  initBoxSelect() {
    const gallery = document.getElementById("gallery");
    if (!gallery || gallery.dataset.boxSelect) return;  // 防止重复绑定
    gallery.dataset.boxSelect = "1";

    let box = null;      // 选框 DOM
    let startX = 0, startY = 0;

    gallery.addEventListener("mousedown", (e) => {
      // 点按在卡片上不启动框选（卡片有自己的选择逻辑）
      if (e.target.closest(".media-card")) return;
      if (e.button !== 0) return;  // 仅左键
      startX = e.clientX;
      startY = e.clientY;
      box = document.createElement("div");
      box.className = "select-box";
      document.body.appendChild(box);
      // 拖拽期间禁用文本选择，避免误选页面文字
      document.body.classList.add("selecting");
    });

    document.addEventListener("mousemove", (e) => {
      if (!box) return;
      const x = Math.min(e.clientX, startX);
      const y = Math.min(e.clientY, startY);
      const w = Math.abs(e.clientX - startX);
      const h = Math.abs(e.clientY - startY);
      box.style.left = x + "px";
      box.style.top = y + "px";
      box.style.width = w + "px";
      box.style.height = h + "px";
    });

    document.addEventListener("mouseup", (e) => {
      if (!box) return;
      const rect = box.getBoundingClientRect();
      const dist = Math.hypot(e.clientX - startX, e.clientY - startY);
      if (dist < 5) {
        // 空白处单击（几乎没有拖动）：清空选择
        App.state.selected.clear();
        App.state.lastAnchor = null;
      } else {
        // 框选：矩形与卡片相交则选中
        document.querySelectorAll("#gallery .media-card").forEach(card => {
          const cr = card.getBoundingClientRect();
          const hit = !(cr.right < rect.left || cr.left > rect.right ||
                        cr.bottom < rect.top || cr.top > rect.bottom);
          if (hit) {
            App.state.selected.add(parseInt(card.dataset.id, 10));
          }
        });
      }
      box.remove();
      box = null;
      document.body.classList.remove("selecting");
      this.render();
      this.updateSelInfo();
      // 框选后刷新右侧标签侧边栏
      SidePanel.open();
      SidePanel.refresh();
    });
  },

  /** 更新“加载更多”按钮显示 */
  updateLoadMore() {
    const btn = document.getElementById("btn-loadmore");
    btn.style.display = App.state.total > App.state.items.length ? "" : "none";
  },

  /** 更新结果统计 */
  updateResultInfo() {
    document.getElementById("result-info").textContent =
      "共 " + App.state.total + " 个，已加载 " + App.state.items.length;
  },

  /** 更新选择信息 */
  updateSelInfo() {
    document.getElementById("sel-info").textContent =
      "已选择 " + App.state.selected.size + " 项";
  },

  /** 清除选择 */
  clearSelection() {
    App.state.selected.clear();
    App.state.lastAnchor = null;
    this.updateSelInfo();
    this.render();
    SidePanel.refresh();
  },
};
