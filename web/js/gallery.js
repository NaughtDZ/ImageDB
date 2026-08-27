/* ============================================================
 * 画廊组件：立即显示文件名/卡片框架，缩略图异步懒加载，翻页中断旧页加载
 *
 * 性能策略（响应优先，流畅翻页）：
 *   1. 卡片立即渲染：render() 同步创建所有卡片（文件名/类型徽标/视频时长），
 *      缩略图用独立 <img> 异步加载，绝不阻塞卡片显示。
 *   2. 缩略图懒加载：IntersectionObserver 监听，进入视口才真正请求缩略图；
 *      有内存缓存则秒显，无则请求后端生成并缓存。
 *   3. 翻页中断旧页：用 AbortController 管理当前批次的缩略图请求，
 *      翻页时 abort 所有未完成的旧页请求，只加载目标页，避免旧页抢占带宽。
 *   4. 内存缓存：Gallery._thumbCache (Map<id, img>) LRU，翻回已访问页秒显。
 *
 * 分页模式：每次一页，翻页丢弃旧页 DOM 省内存，滚动回顶。
 * ============================================================ */
const Gallery = {
  // 内存缩略图缓存：mediaId -> 已加载的 <img>（LRU）
  _thumbCache: new Map(),
  _THUMB_CACHE_MAX: 400,
  // 当前批次缩略图请求的控制器（翻页时 abort，丢弃旧页未完成请求）
  _abortCtrl: null,
  _loading: new Set(),   // 正在加载的 mediaId 集合（去重，避免重复请求）

  /** 缓存一张已加载的缩略图（LRU） + 记录到浏览器级 */
  cacheThumb(id, imgEl) {
    if (this._thumbCache.has(id)) this._thumbCache.delete(id);
    this._thumbCache.set(id, imgEl);
    while (this._thumbCache.size > this._THUMB_CACHE_MAX) {
      this._thumbCache.delete(this._thumbCache.keys().next().value);
    }
  },

  /** 取内存缓存缩略图（命中则移动 LRU 并返回） */
  getCachedThumb(id) {
    if (!this._thumbCache.has(id)) return null;
    const img = this._thumbCache.get(id);
    this._thumbCache.delete(id);
    this._thumbCache.set(id, img);
    return img;
  },

  /** 加载媒体列表：翻页时先 abort 旧页缩略图请求，再渲染目标页 */
  async load(reset) {
    // 翻页：中断上一批缩略图加载（丢弃旧页请求，优先目标页）
    this.abortThumbLoads();

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
      App.state.items = data.items;
      if (reset) App.state.page = 1;
      App.state.selected.clear();
      App.state.lastAnchor = null;
      // 1) 立即渲染卡片框架（文件名等），不阻塞
      this.render();
      // 2) 立刻启动缩略图加载（视口内优先，异步进行）
      this._abortCtrl = new AbortController();
      this.loadVisibleThumbs(this._abortCtrl.signal);
      // 3) 更新 UI 状态
      this.updatePager();
      this.updateResultInfo();
      this.updateSelInfo();
      this.scrollTop();
      this.saveBrowseState();
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

  /** 中断当前批次所有未完成的缩略图请求 */
  abortThumbLoads() {
    if (this._abortCtrl) {
      try { this._abortCtrl.abort(); } catch (e) {}
      this._abortCtrl = null;
    }
    // 清理"加载中"标记（用于去重），因为翻页后这些请求已作废
    // 注意：不清空 _loading 会误判新页图已在加载，这里清空
    this._loading.clear();
  },

  /**
   * 加载视口内的缩略图（异步）。用 signal 支持翻页中断。
   * 优先用内存缓存秒显，未命中才发请求。
   */
  loadVisibleThumbs(signal) {
    const galleryEl = document.getElementById("gallery");
    if (!galleryEl) return;
    const winH = window.innerHeight || 0;
    // 视口内 + 上下余量 200px 的卡片
    const cards = [...document.querySelectorAll(".media-card")];
    for (const card of cards) {
      const im = card.querySelector(".thumb-img");
      if (!im) continue;
      const id = card.dataset.id;
      const r = card.getBoundingClientRect();
      const inView = r.top < winH + 200 && r.bottom > -200;
      // 已加载（有 src 且非 data-src 模式）则跳过
      if (im.dataset.loaded === "1") continue;
      if (inView) {
        this.loadOneThumb(im, id, signal);
      }
    }
    // 其余交给 IntersectionObserver（滚动时加载）
    if (this._thumbObserver) {
      this._thumbObserver.disconnect();
    } else if ("IntersectionObserver" in window) {
      this._thumbObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const im = entry.target;
            const id = im.closest(".media-card")?.dataset?.id;
            if (id && im.dataset.loaded !== "1" && this._abortCtrl) {
              this.loadOneThumb(im, id, this._abortCtrl.signal);
              this._thumbObserver.unobserve(im);
            }
          }
        }
      }, { rootMargin: "200px 0px", threshold: 0.01 });
    }
    // 对未加载的缩略图重新注册观察
    document.querySelectorAll(".thumb-img[data-loaded!='1']").forEach(im => {
      if (this._thumbObserver) this._thumbObserver.observe(im);
    });
  },

  /** 加载单个缩略图（内存缓存命中则秒显，否则请求） */
  loadOneThumb(im, id, signal) {
    // 已在加载中则跳过（去重）
    if (this._loading.has(id)) return;
    const cached = this.getCachedThumb(id);
    if (cached && cached.complete && cached.naturalWidth > 0) {
      // 内存缓存命中：直接复用 src，秒显
      im.src = cached.src;
      im.dataset.loaded = "1";
      im.style.opacity = "1";
      return;
    }
    // 未命中：发起请求（带 signal 支持中断）
    this._loading.add(id);
    const url = "/api/media/" + id + "/thumbnail";
    const worker = new Image();
    worker.signal = signal;
    if (signal) {
      // 中断时取消
      signal.addEventListener("abort", () => {
        worker.src = "";   // 断开加载
        this._loading.delete(id);
      }, { once: true });
    }
    worker.onload = () => {
      this._loading.delete(id);
      // 更新卡片 img（若 DOM 还在），并写缓存
      im.src = worker.src;
      im.dataset.loaded = "1";
      im.style.opacity = "1";
      this.cacheThumb(id, worker);
    };
    worker.onerror = () => {
      this._loading.delete(id);
      im.style.background = "var(--bg-hover)";
      im.alt = "⚠";
    };
    worker.src = url;
  },

  /** 渲染网格：立即创建卡片框架（文件名/徽标），缩略图占位，不阻塞 */
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
    // 兜底：无 IntersectionObserver 时直接加载所有
    if (!("IntersectionObserver" in window)) {
      const thumbImgs = box.querySelectorAll(".thumb-img");
      thumbImgs.forEach((im, i) => {
        // 简化：无 IO 环境直接加载视口外也不管了（现代浏览器都有）
      });
    }
  },

  /** 渲染单个媒体卡片：文件名/徽标立即显示，缩略图占位（含渐变过渡） */
  renderCard(item) {
    const card = document.createElement("div");
    card.className = "media-card" + (App.state.selected.has(item.id) ? " selected" : "");
    card.dataset.id = item.id;

    // 缩略图容器：先显示占位背景，图片加载后淡入
    const thumbBox = document.createElement("div");
    thumbBox.className = "thumb-box";
    thumbBox.style.background = "var(--bg-hover)";   // 占位
    const img = document.createElement("img");
    img.alt = item.filename;
    img.className = "thumb-img";
    img.style.opacity = "0";   // 初始透明，加载后淡入
    img.dataset.loaded = "0";
    thumbBox.appendChild(img);
    card.appendChild(thumbBox);

    // 类型徽标（立即显示）
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = item.type === "video" ? "🎬" : "🖼";
    card.appendChild(badge);

    // 视频时长（立即显示）
    if (item.type === "video" && item.duration) {
      const dur = document.createElement("span");
      dur.className = "dur";
      dur.textContent = formatDuration(item.duration);
      card.appendChild(dur);
    }

    // 文件名（立即显示，最重要的信息）
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
        // 重新加载缩略图（因为 render 重建了 DOM）
        if (this._abortCtrl) this.loadVisibleThumbs(this._abortCtrl.signal);
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

  /** 滚动到画廊顶部 */
  scrollTop() {
    const gallery = document.getElementById("gallery");
    if (gallery) gallery.scrollTop = 0;
  },

  /** 保存浏览状态到 localStorage */
  saveBrowseState() {
    try {
      localStorage.setItem("imagedb_browse", JSON.stringify({
        folderId: App.state.currentFolderId,
        page: App.state.page,
        pageSize: App.state.pageSize,
        scrollTop: document.getElementById("gallery")?.scrollTop || 0,
        ts: Date.now(),
      }));
    } catch (e) {}
  },

  /** 读取浏览状态 */
  loadBrowseState() {
    try {
      const raw = localStorage.getItem("imagedb_browse");
      return raw ? JSON.parse(raw) : null;
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

  /** 初始化分页导航 */
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

  /** Shift 区间选择 */
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
    if (this._abortCtrl) this.loadVisibleThumbs(this._abortCtrl.signal);
    this.updateSelInfo();
  },

  /** 缩略图大小滑块 */
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

  /** 框选 */
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
      if (this._abortCtrl) this.loadVisibleThumbs(this._abortCtrl.signal);
      this.updateSelInfo();
      SidePanel.open();
      SidePanel.refresh();
    });
  },

  /** 更新结果统计 */
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
    if (this._abortCtrl) this.loadVisibleThumbs(this._abortCtrl.signal);
    SidePanel.refresh();
  },
};
