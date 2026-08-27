/* ============================================================
 * 画廊组件：媒体网格、选择（单击/Ctrl/Shift/框选）、分页浏览、缩略图内存缓存
 *
 * 缩略图缓存策略（解决高强度翻页卡顿）：
 *   - 内存缓存：Gallery._thumbCache (Map<mediaId, img>) 记录已成功加载的缩略图，
 *     翻回已访问页时直接复用缓存的 <img>，秒显不再重新网络请求；
 *     采用 LRU 策略，上限 _THUMB_CACHE_MAX 张（默认 400）防止内存无限增长。
 *   - 硬盘持久化：缩略图生成后仍持久化到 data/thumbs/（重启共用，生成过就保留）。
 *   - 预取：翻页后只预取"视口内 + 少量余量"的缩略图（而非整页 60 张），
 *     避免首访新页时瞬间生成大量缩略图导致卡顿。
 *   - 浏览状态：关闭/退出时保存当前目录、页码、滚动位置到 localStorage，
 *     下次启动自动恢复。
 *
 * 分页模式：
 *   - 每次只显示一页，翻页丢弃旧页内容省内存；
 *   - 每页数量可设置（工具栏输入框，记忆上次值）；
 *   - 翻页后滚动回顶部、清空选择。
 * ============================================================ */
const Gallery = {
  // 内存缩略图缓存：mediaId -> <img>（已加载）
  _thumbCache: new Map(),
  _THUMB_CACHE_MAX: 400,   // 内存缓存上限（张），超过按 LRU 淘汰最旧的

  /** 缓存一张已加载的缩略图（LRU） */
  cacheThumb(id, imgEl) {
    // 已存在则先删（重新插到队尾表示最近使用）
    if (this._thumbCache.has(id)) {
      this._thumbCache.delete(id);
    }
    this._thumbCache.set(id, imgEl);
    // 超限淘汰最旧的（Map 迭代序 = 插入序，第一个最旧）
    while (this._thumbCache.size > this._THUMB_CACHE_MAX) {
      const oldestKey = this._thumbCache.keys().next().value;
      this._thumbCache.delete(oldestKey);
    }
  },

  /** 从内存缓存取缩略图（命中则返回 img 并标记最近使用） */
  getCachedThumb(id) {
    if (!this._thumbCache.has(id)) return null;
    const img = this._thumbCache.get(id);
    // 命中：移到队尾（LRU 更新）
    this._thumbCache.delete(id);
    this._thumbCache.set(id, img);
    return img;
  },

  /** 加载媒体列表：分页模式，翻页丢弃旧页内容省内存 */
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
      App.state.items = data.items;   // 翻页丢弃旧页，节约内存
      if (reset) App.state.page = 1;
      App.state.selected.clear();
      App.state.lastAnchor = null;
      this.render();
      this.prefetchVisible();   // 只预取视口附近的缩略图
      this.updatePager();
      this.updateResultInfo();
      this.updateSelInfo();
      this.scrollTop();
      this.saveBrowseState();   // 记录当前浏览状态
    } catch (e) {
      if (e.status === 410) {
        toast("部分文件已被外部删除，记录已自动清理", "err");
        TreeView.refresh();
        this.load(reset);
      } else {
        toast("加载失败：" + e.message, "err");
      }
    }
  },

  /** 滚动到画廊顶部（翻页/跳页时调用） */
  scrollTop() {
    const gallery = document.getElementById("gallery");
    if (gallery) gallery.scrollTop = 0;
  },

  /**
   * 只预取"视口内 + 少量余量"的缩略图（而非整页）。
   * 翻页后主要看视口内容，预取太多首访新页会导致瞬间生成大量缩略图卡顿。
   */
  prefetchVisible() {
    const items = App.state.items || [];
    if (!items.length) return;
    const galleryEl = document.getElementById("gallery");
    const winH = window.innerHeight || 0;
    // 预估视口能容纳的行数（每行约 6 列 + 余量）
    const visibleCount = Math.ceil((winH / 190) * 6) + 12;
    const urls = items.slice(0, visibleCount).map(i => "/api/media/" + i.id + "/thumbnail");
    let idx = 0;
    const batch = () => {
      for (let n = 0; n < 6 && idx < urls.length; n++, idx++) {
        const im = new Image();
        im.src = urls[idx];   // 触发后端生成并写硬盘缓存 + 浏览器缓存
      }
      if (idx < urls.length) setTimeout(batch, 50);
    };
    setTimeout(batch, 100);
  },

  /** 保存当前浏览状态到 localStorage（目录、页码、滚动位置） */
  saveBrowseState() {
    try {
      localStorage.setItem("imagedb_browse", JSON.stringify({
        folderId: App.state.currentFolderId,
        page: App.state.page,
        pageSize: App.state.pageSize,
        scrollTop: document.getElementById("gallery")?.scrollTop || 0,
        ts: Date.now(),
      }));
    } catch (e) { /* localStorage 满或不可用，忽略 */ }
  },

  /** 读取上次保存的浏览状态（用于启动恢复） */
  loadBrowseState() {
    try {
      const raw = localStorage.getItem("imagedb_browse");
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) { return null; }
  },

  /** 翻页到指定页码 */
  async changePage(page) {
    const totalPages = Math.max(1, Math.ceil(App.state.total / App.state.pageSize));
    if (page < 1) page = 1;
    if (page > totalPages) page = totalPages;
    if (page === App.state.page) return;
    App.state.page = page;
    await this.load(false);
  },

  /** 初始化分页导航：绑定按钮 + 每页数量输入 */
  initPager() {
    const btnFirst = document.getElementById("btn-page-first");
    const btnPrev = document.getElementById("btn-page-prev");
    const btnNext = document.getElementById("btn-page-next");
    const btnLast = document.getElementById("btn-page-last");
    const btnGoto = document.getElementById("btn-page-goto");
    const gotoInput = document.getElementById("pager-goto");
    const pageSizeInput = document.getElementById("page-size-input");

    if (btnFirst) btnFirst.onclick = () => this.changePage(1);
    if (btnPrev) btnPrev.onclick = () => this.changePage(App.state.page - 1);
    if (btnNext) btnNext.onclick = () => this.changePage(App.state.page + 1);
    if (btnLast) btnLast.onclick = () =>
      this.changePage(Math.max(1, Math.ceil(App.state.total / App.state.pageSize)));
    if (btnGoto && gotoInput) {
      btnGoto.onclick = () => this.changePage(parseInt(gotoInput.value, 10) || 1);
      gotoInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") this.changePage(parseInt(gotoInput.value, 10) || 1);
      });
    }
    if (pageSizeInput) {
      const saved = localStorage.getItem("imagedb_page_size");
      if (saved) {
        const v = parseInt(saved, 10);
        if (v >= 10 && v <= 500) pageSizeInput.value = v;
      }
      pageSizeInput.addEventListener("change", () => {
        let v = parseInt(pageSizeInput.value, 10) || 60;
        v = Math.max(10, Math.min(500, v));
        pageSizeInput.value = v;
        App.state.pageSize = v;
        localStorage.setItem("imagedb_page_size", String(v));
        App.state.page = 1;
        this.load(true);
      });
      const initV = parseInt(pageSizeInput.value, 10) || 60;
      if (App.state.pageSize !== initV) App.state.pageSize = initV;
    }
  },

  /** 更新分页导航状态 */
  updatePager() {
    const current = document.getElementById("pager-current");
    const total = document.getElementById("pager-total");
    const goto = document.getElementById("pager-goto");
    const totalPages = Math.max(1, Math.ceil(App.state.total / App.state.pageSize));
    if (current) current.textContent = App.state.page;
    if (total) total.textContent = totalPages;
    if (goto) goto.value = App.state.page;
    const btnFirst = document.getElementById("btn-page-first");
    const btnPrev = document.getElementById("btn-page-prev");
    const btnNext = document.getElementById("btn-page-next");
    const btnLast = document.getElementById("btn-page-last");
    if (btnFirst) btnFirst.disabled = App.state.page <= 1;
    if (btnPrev) btnPrev.disabled = App.state.page <= 1;
    if (btnNext) btnNext.disabled = App.state.page >= totalPages;
    if (btnLast) btnLast.disabled = App.state.page >= totalPages;
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
    requestAnimationFrame(() => this.observeThumbs());
    clearTimeout(this._observeFallback);
    this._observeFallback = setTimeout(() => {
      const thumbs = document.querySelectorAll(".thumb-img[data-src]");
      const anyLoaded = document.querySelector(".thumb-img[src]");
      if (thumbs.length && !anyLoaded) {
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

  /** 按需渲染：IntersectionObserver 只加载视口内缩略图（优先复用内存缓存） */
  observeThumbs() {
    const galleryEl = document.getElementById("gallery");
    if (!galleryEl || !("IntersectionObserver" in window)) {
      document.querySelectorAll(".thumb-img[data-src]").forEach(im => {
        im.src = im.dataset.src;
        delete im.dataset.src;
      });
      return;
    }
    if (!this._thumbObserver) {
      this._thumbObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const im = entry.target;
          if (entry.isIntersecting && im.dataset.src) {
            requestAnimationFrame(() => {
              if (im.dataset.src) {
                im.src = im.dataset.src;
                delete im.dataset.src;
              }
            });
            this._thumbObserver.unobserve(im);
          }
        }
      }, {
        rootMargin: "200px 0px",
        threshold: 0.01,
      });
    }
    for (const im of document.querySelectorAll(".thumb-img[data-src]")) {
      const r = im.getBoundingClientRect();
      const winH = window.innerHeight || 0;
      if (r.top < winH + 200 && r.bottom > -200) {
        im.src = im.dataset.src;
        delete im.dataset.src;
      } else {
        this._thumbObserver.observe(im);
      }
    }
    document.querySelectorAll(".thumb-img[data-src]").forEach(im => {
      this._thumbObserver.observe(im);
    });
  },

  /** 渲染单个媒体卡片（优先复用内存缓存的缩略图 img） */
  renderCard(item) {
    const card = document.createElement("div");
    card.className = "media-card" + (App.state.selected.has(item.id) ? " selected" : "");
    card.dataset.id = item.id;

    const thumbBox = document.createElement("div");
    thumbBox.className = "thumb-box";
    const img = document.createElement("img");
    img.alt = item.filename;
    img.className = "thumb-img";

    // 优先用内存缓存：已加载过的缩略图翻回时秒显，不再发网络请求
    const cached = this.getCachedThumb(item.id);
    if (cached && cached.complete && cached.naturalWidth > 0) {
      // 复用缓存 img 的 src（浏览器级内存缓存）
      img.src = cached.src;
      img.dataset.cached = "1";
    } else {
      img.dataset.src = "/api/media/" + item.id + "/thumbnail";
    }
    img.onload = () => {
      // 加载成功后写入内存缓存（供翻回时复用）
      this.cacheThumb(item.id, img);
    };
    img.onerror = () => {
      img.src = "";
      img.style.background = "var(--bg-hover)";
      img.alt = "⚠";
      delete img.dataset.src;
    };
    thumbBox.appendChild(img);
    card.appendChild(thumbBox);

    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = item.type === "video" ? "🎬" : "🖼";
    card.appendChild(badge);

    if (item.type === "video" && item.duration) {
      const dur = document.createElement("span");
      dur.className = "dur";
      dur.textContent = formatDuration(item.duration);
      card.appendChild(dur);
    }

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = item.filename;
    name.title = item.path;
    card.appendChild(name);

    card.onclick = (e) => {
      e.stopPropagation();
      if (e.shiftKey) {
        this.selectRange(item.id);
      } else if (e.ctrlKey || e.metaKey) {
        if (App.state.selected.has(item.id)) App.state.selected.delete(item.id);
        else App.state.selected.add(item.id);
        App.state.lastAnchor = item.id;
        card.classList.toggle("selected");
      } else {
        App.state.selected.clear();
        App.state.selected.add(item.id);
        App.state.lastAnchor = item.id;
        this.render();
      }
      this.updateSelInfo();
      SidePanel.open();
      SidePanel.refresh();
    };

    card.ondblclick = (e) => {
      e.stopPropagation();
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
    let anchorIdx = ids.indexOf(App.state.lastAnchor);
    if (anchorIdx < 0) anchorIdx = curIdx;
    const [a, b] = curIdx < anchorIdx ? [curIdx, anchorIdx] : [anchorIdx, curIdx];
    for (let i = a; i <= b; i++) {
      App.state.selected.add(ids[i]);
    }
    this.render();
    this.updateSelInfo();
  },

  /** 缩略图大小滑块：调节 grid 列宽（CSS 变量 --thumb-size），并持久化到 localStorage。 */
  initThumbSlider() {
    const slider = document.getElementById("thumb-size-slider");
    if (!slider || slider.dataset.inited) return;
    slider.dataset.inited = "1";
    const galleryEl = document.getElementById("gallery");
    const saved = localStorage.getItem("imagedb_thumb_size");
    if (saved) {
      const v = parseInt(saved, 10);
      if (v >= 100 && v <= 400) {
        slider.value = v;
        galleryEl.style.setProperty("--thumb-size", v + "px");
      }
    }
    slider.addEventListener("input", () => {
      const v = parseInt(slider.value, 10) || 160;
      galleryEl.style.setProperty("--thumb-size", v + "px");
      localStorage.setItem("imagedb_thumb_size", String(v));
    });
  },

  /** 框选：在画廊空白处按下鼠标拖拽，框选矩形范围内的卡片。 */
  initBoxSelect() {
    const gallery = document.getElementById("gallery");
    if (!gallery || gallery.dataset.boxSelect) return;
    gallery.dataset.boxSelect = "1";
    let box = null, startX = 0, startY = 0;
    gallery.addEventListener("mousedown", (e) => {
      if (e.target.closest(".media-card")) return;
      if (e.button !== 0) return;
      startX = e.clientX;
      startY = e.clientY;
      box = document.createElement("div");
      box.className = "select-box";
      document.body.appendChild(box);
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
    document.addEventListener("mouseup", () => {
      if (!box) return;
      const rect = box.getBoundingClientRect();
      if (Math.hypot(rect.width, rect.height) < 5) {
        App.state.selected.clear();
        App.state.lastAnchor = null;
      } else {
        document.querySelectorAll("#gallery .media-card").forEach(card => {
          const cr = card.getBoundingClientRect();
          const hit = !(cr.right < rect.left || cr.left > rect.right ||
                        cr.bottom < rect.top || cr.top > rect.bottom);
          if (hit) App.state.selected.add(parseInt(card.dataset.id, 10));
        });
      }
      box.remove();
      box = null;
      document.body.classList.remove("selecting");
      this.render();
      this.updateSelInfo();
      SidePanel.open();
      SidePanel.refresh();
    });
  },

  /** 更新结果统计（分页） */
  updateResultInfo() {
    document.getElementById("result-info").textContent =
      "共 " + App.state.total + " 个素材 · 第 " + App.state.page + " 页";
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
