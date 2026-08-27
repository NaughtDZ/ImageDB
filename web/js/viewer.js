/* ============================================================
 * 查看器组件
 * 功能：
 *   图片：缩放（滚轮/按钮）、拖动平移、适应窗口、双击切换；
 *   视频：播放/暂停、进度条（原生 controls）、快进/快退 10 秒、
 *         倍速、音量、全屏；
 *   通用：上一张/下一张、键盘导航、幻灯片放映（时间可控）、
 *         全屏（F）、关闭（Esc）。
 * ============================================================ */
const Viewer = {
  list: [],            // 当前查看列表
  index: 0,            // 当前下标
  zoom: 1,             // 缩放倍率
  dragging: false,     // 拖拽平移标志
  panX: 0, panY: 0,    // 平移偏移
  slideshow: false,    // 幻灯片状态
  slideTimer: null,    // 幻灯片定时器

  /** 打开查看器 */
  open(items, index) {
    if (!items || !items.length) return;
    this._preloaded = new Set();   // 新会话重置预加载记录
    this.list = items;
    this.index = index;
    this.zoom = 1;
    this.panX = 0;
    this.panY = 0;
    this.stopSlideshow();
    document.getElementById("viewer").classList.remove("hidden");
    this.render();
  },

  /** 关闭查看器 */
  close() {
    this.stopSlideshow();
    this.pauseVideo();
    document.getElementById("viewer").classList.add("hidden");
    // 退出全屏
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  },

  /** 渲染当前项 */
  render() {
    const item = this.list[this.index];
    if (!item) return;
    const img = document.getElementById("viewer-img");
    const video = document.getElementById("viewer-video");
    const hint = document.getElementById("viewer-hint");

    // 标题与计数
    document.getElementById("viewer-title").textContent =
      "[" + (this.index + 1) + "/" + this.list.length + "] " + item.filename;
    document.getElementById("viewer-counter").textContent =
      formatSize(item.size) + (item.duration ? "  ·  " + formatDuration(item.duration) : "");

    // 标签
    this.renderTags(item);

    if (item.type === "video") {
      img.classList.add("hidden");
      video.classList.remove("hidden");
      document.getElementById("video-controls").classList.remove("hidden");
      hint.classList.add("hidden");
      // 更新视频源
      if (video.dataset.src !== item.id) {
        video.pause();
        video.src = "/api/media/" + item.id + "/file";
        video.dataset.src = item.id;
        video.load();
      }
      this.syncVideoControls();
      // 尝试自动播放（可能被浏览器阻止）
      video.play().catch(() => {});
    } else {
      video.pause();
      video.classList.add("hidden");
      document.getElementById("video-controls").classList.add("hidden");
      img.classList.remove("hidden");
      img.src = "/api/media/" + item.id + "/file";
      this.applyTransform();
      hint.classList.add("hidden");
      // 预加载相邻图片到浏览器缓存（切图时立即显示，无空白等待）
      this.preloadNearby();
    }
  },

  /**
   * 预加载：静默请求相邻几张图片的原图（用 Image 对象预热浏览器缓存）。
   * 只预加载图片（视频太大不预载），数量 = 前后各 1 张。
   */
  preloadNearby() {
    const preloadCount = 1;   // 前后各预载 1 张，避免过度占用带宽
    const targets = [];
    for (let d = 1; d <= preloadCount; d++) {
      const next = this.list[this.index + d];
      const prev = this.list[this.index - d];
      if (next && next.type === "image") targets.push(next);
      if (prev && prev.type === "image") targets.push(prev);
    }
    for (const item of targets) {
      const im = new Image();
      // 用查询参数绕开浏览器缓存去重（同一 id 不重复请求）
      if (!this._preloaded || !this._preloaded.has(item.id)) {
        im.src = "/api/media/" + item.id + "/file";
        if (!this._preloaded) this._preloaded = new Set();
        this._preloaded.add(item.id);
      }
    }
  },

  /** 渲染查看器底部标签 */
  renderTags(item) {
    const box = document.getElementById("viewer-tags");
    box.innerHTML = "";
    if (!item.tags || !item.tags.length) return;
    for (const t of item.tags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.innerHTML =
        escapeHtml(t.name) +
        ' <span class="src">' + escapeHtml(t.source) + " " +
        (t.source !== "manual" ? (t.confidence * 100).toFixed(0) + "%" : "") + "</span>";
      // 点击标签 → 按该标签筛选
      chip.title = "按此标签筛选";
      chip.onclick = () => {
        document.getElementById("search-tags").value = t.name;
        App.applyFilters();
        this.close();
      };
      box.appendChild(chip);
    }
  },

  // ---------------- 导航（循环浏览） ----------------
  next() {
    // 到末尾后回到开头（幻灯片可无限循环播放）
    if (this.index < this.list.length - 1) { this.index++; }
    else { this.index = 0; }
    this.render();
  },
  prev() {
    // 到开头后跳到末尾（循环）
    if (this.index > 0) { this.index--; }
    else { this.index = this.list.length - 1; }
    this.render();
  },

  // ---------------- 图片缩放与平移 ----------------
  applyTransform() {
    const img = document.getElementById("viewer-img");
    img.style.transform =
      "translate(" + this.panX + "px, " + this.panY + "px) scale(" + this.zoom + ")";
  },
  setZoom(delta, center) {
    const img = document.getElementById("viewer-img");
    const old = this.zoom;
    this.zoom = Math.min(8, Math.max(0.1, this.zoom * delta));
    // 以鼠标位置为缩放中心
    if (center && this.zoom !== old) {
      const rect = img.getBoundingClientRect();
      const ratio = this.zoom / old;
      this.panX = center.x - (center.x - this.panX) * ratio;
      this.panY = center.y - (center.y - this.panY) * ratio;
    }
    this.applyTransform();
  },
  zoomFit() {
    this.zoom = 1; this.panX = 0; this.panY = 0;
    this.applyTransform();
  },

  // ---------------- 视频控制 ----------------
  pauseVideo() {
    const video = document.getElementById("viewer-video");
    video.pause();
  },
  syncVideoControls() {
    const video = document.getElementById("viewer-video");
    document.getElementById("video-speed").value = String(video.playbackRate || 1);
    document.getElementById("video-volume").value = String(video.volume || 1);
  },
  seekBy(seconds) {
    const video = document.getElementById("viewer-video");
    if (video.duration) video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
  },

  // ---------------- 幻灯片 ----------------
  toggleSlideshow() {
    if (this.slideshow) { this.stopSlideshow(); return; }
    const interval = parseInt(document.getElementById("slide-interval").value, 10) || 5;
    this.slideshow = true;
    // 图片：定时切换；视频：播放完自动下一张（在 ended 事件处理）
    this.slideTimer = setInterval(() => {
      const item = this.list[this.index];
      const video = document.getElementById("viewer-video");
      if (item.type === "video") {
        // 视频正在播放则等待，暂停则推进
        if (!video.paused) return;
        this.next();
      } else {
        this.next();
      }
    }, interval * 1000);
    toast("幻灯片放映中（间隔 " + interval + " 秒）", "ok");
  },
  stopSlideshow() {
    this.slideshow = false;
    if (this.slideTimer) { clearInterval(this.slideTimer); this.slideTimer = null; }
  },

  // ---------------- 全屏 ----------------
  toggleFullscreen() {
    const modal = document.getElementById("viewer");
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      modal.requestFullscreen().catch(() => toast("全屏被浏览器拒绝", "err"));
    }
  },

  // ---------------- 事件绑定 ----------------
  initKeyboard() {
    document.addEventListener("keydown", (e) => {
      const viewer = document.getElementById("viewer");
      if (viewer.classList.contains("hidden")) return;
      // 输入框内不拦截
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;

      switch (e.key) {
        case "Escape": this.close(); break;
        case "ArrowLeft": this.prev(); break;
        case "ArrowRight": this.next(); break;
        case "+": case "=": this.setZoom(1.2); break;
        case "-": case "_": this.setZoom(0.8); break;
        case "0": this.zoomFit(); break;
        case "f": case "F": this.toggleFullscreen(); break;
        case " ": e.preventDefault(); this.togglePlay(); break;
        case "s": case "S": this.toggleSlideshow(); break;
      }
    });

    // 查看器按钮
    document.querySelectorAll("#viewer [data-vaction]").forEach(btn => {
      btn.onclick = (e) => {
        const action = btn.dataset.vaction;
        switch (action) {
          case "prev": this.prev(); break;
          case "next": this.next(); break;
          case "slideshow": this.toggleSlideshow(); break;
          case "zoom-in": this.setZoom(1.2); break;
          case "zoom-out": this.setZoom(0.8); break;
          case "zoom-fit": this.zoomFit(); break;
          case "fullscreen": this.toggleFullscreen(); break;
          case "close": this.close(); break;
          case "rewind": this.seekBy(-10); break;
          case "forward": this.seekBy(10); break;
          case "playpause": this.togglePlay(); break;
        }
      };
    });

    // 滚轮：普通滚动 = 上一张/下一张；Shift+滚动 = 缩放
    const img = document.getElementById("viewer-img");
    img.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (e.shiftKey) {
        // Shift + 滚轮：缩放（以鼠标位置为中心）
        const rect = img.getBoundingClientRect();
        this.setZoom(e.deltaY < 0 ? 1.15 : 0.87, {
          x: e.clientX - rect.left,
          y: e.clientY - rect.top,
        });
      } else {
        // 普通滚轮：切换图片（向上滚 = 上一张，向下滚 = 下一张）
        if (e.deltaY > 0) this.next();
        else this.prev();
      }
    }, { passive: false });

    // 图片拖拽平移
    img.addEventListener("mousedown", (e) => {
      this.dragging = true;
      img.classList.add("dragging");
      this._dragStart = { x: e.clientX - this.panX, y: e.clientY - this.panY };
    });
    document.addEventListener("mousemove", (e) => {
      if (!this.dragging) return;
      this.panX = e.clientX - this._dragStart.x;
      this.panY = e.clientY - this._dragStart.y;
      this.applyTransform();
    });
    document.addEventListener("mouseup", () => {
      if (this.dragging) { this.dragging = false; img.classList.remove("dragging"); }
    });

    // 双击图片：在适应窗口与 100% 之间切换
    img.addEventListener("dblclick", () => {
      if (this.zoom === 1 && this.panX === 0 && this.panY === 0) this.setZoom(2);
      else this.zoomFit();
    });

    // 视频：倍速 / 音量
    document.getElementById("video-speed").onchange = (e) => {
      document.getElementById("viewer-video").playbackRate = parseFloat(e.target.value);
    };
    document.getElementById("video-volume").onchange = (e) => {
      document.getElementById("viewer-video").volume = parseFloat(e.target.value);
    };

    // 视频播放结束：幻灯片模式自动下一张
    document.getElementById("viewer-video").addEventListener("ended", () => {
      if (this.slideshow) this.next();
    });
  },

  /** 播放/暂停当前视频 */
  togglePlay() {
    const item = this.list[this.index];
    if (!item || item.type !== "video") return;
    const video = document.getElementById("viewer-video");
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  },
};
