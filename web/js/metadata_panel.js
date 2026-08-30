/* ============================================================
 * 附加数据侧边栏（EXIF / IPTC / XMP）
 *
 * 功能：
 *   1. 选中素材后，按需读取并显示该素材的 EXIF/IPTC/XMP 附加数据；
 *   2. 多选时默认显示第一张（在状态行注明）；读取结果做会话内内存缓存；
 *   3. 附加数据分区高度、侧边栏宽度均可拖拽调节（尺寸存 localStorage）。
 * ============================================================ */
const MetadataPanel = {
  _cache: new Map(),     // media_id -> {exif, iptc, xmp}

  /** 刷新：取选中集合第一张的附加数据并渲染 */
  async refresh(sel) {
    const status = document.getElementById("meta-status");
    const groups = document.getElementById("meta-groups");
    if (!sel || sel.length === 0) {
      groups.innerHTML = "";
      status.textContent = "选择素材后显示 EXIF / IPTC / XMP";
      status.classList.remove("hidden");
      return;
    }
    const id = sel[0];
    if (sel.length > 1) {
      status.textContent = "已选 " + sel.length + " 个，显示第一张的附加数据";
      status.classList.remove("hidden");
    } else {
      status.classList.add("hidden");
    }
    if (this._cache.has(id)) { this.render(this._cache.get(id)); return; }
    groups.innerHTML = '<div class="hint">读取中…</div>';
    try {
      const data = await API.get("/api/media/" + id + "/metadata");
      this._cache.set(id, data);
      this.render(data);
    } catch (e) {
      groups.innerHTML = "";
      status.textContent = "读取失败：" + (e.message || "未知错误");
      status.classList.remove("hidden");
    }
  },

  /** 基础信息行：映射中文标签 + 格式化值 */
  basicRows(b) {
    const rows = [];
    const fmtTime = (ep) => { if (!ep) return ""; const d = new Date(ep * 1000); return d.toLocaleString(); };
    const fmtSize = (n) => { if (!n && n !== 0) return ""; const u = ["B","KB","MB","GB"]; let i=0, v=n; while(v>=1024 && i<u.length-1){v/=1024;i++;} return v.toFixed(v>=100?0:1) + " " + u[i]; };
    const fmtDur = (n) => { if (!n) return ""; let s=Math.round(n); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60; const mm=String(m).padStart(2,"0"), sss=String(ss).padStart(2,"0"); return (h ? h+":"+mm+":"+sss : mm+":"+sss) + " (" + n + "s)"; };
    const fmtBit = (n) => { if (!n) return ""; return (n/1000000).toFixed(2) + " Mbps"; };
    if (b.filename) rows.push(["文件名", b.filename]);
    if (b.path) rows.push(["完整路径", b.path]);
    if (b.type) rows.push(["类型", b.type === "video" ? "视频" : "图片"]);
    if (b.format) rows.push(["格式", b.format]);
    if (b.codec) rows.push(["编码", b.codec]);
    if (b.width && b.height) rows.push(["分辨率", b.width + " × " + b.height]);
    if (b.duration) rows.push(["时长", fmtDur(b.duration)]);
    if (b.bitrate) rows.push(["码率", fmtBit(b.bitrate)]);
    if (b.size) rows.push(["大小", fmtSize(b.size)]);
    if (b.created) rows.push(["创建时间", fmtTime(b.created)]);
    if (b.modified) rows.push(["修改时间", fmtTime(b.modified)]);
    return rows;
  },

  /** 渲染 基础信息 + EXIF / IPTC / XMP 分组 */
  render(data) {
    const groups = document.getElementById("meta-groups");
    groups.innerHTML = "";

    // 1) 基础信息
    const basic = data.basic || {};
    if (Object.keys(basic).length) {
      const br = this.basicRows(basic);
      const sec = document.createElement("div");
      sec.className = "meta-group";
      let html = '<div class="g-head">基础信息</div>';
      for (const [k, v] of br) {
        if (v === null || v === undefined || v === "") continue;
        html += '<div class="g-row"><span class="g-key">' + escapeHtml(k) + "</span>" +
                '<span class="g-val">' + escapeHtml(String(v)) + "</span></div>";
      }
      sec.innerHTML = html;
      groups.appendChild(sec);
    }

    // 2) EXIF / IPTC / XMP
    const sections = [
      { key: "exif", label: "EXIF" },
      { key: "iptc", label: "IPTC" },
      { key: "xmp", label: "XMP" },
    ];
    let any = false;
    for (const s of sections) {
      const obj = data[s.key] || {};
      const entries = Object.entries(obj);
      if (!entries.length) continue;
      any = true;
      let html = '<div class="g-head">' + s.label + "</div>";
      for (const [k, v] of entries) {
        const vals = Array.isArray(v) ? v : [v];
        for (const vv of vals) {
          html += '<div class="g-row"><span class="g-key">' + escapeHtml(k) + "</span>" +
                  '<span class="g-val">' + escapeHtml(String(vv)) + "</span></div>";
        }
      }
      const sec = document.createElement("div");
      sec.className = "meta-group";
      sec.innerHTML = html;
      groups.appendChild(sec);
    }
    if (!any && !Object.keys(basic).length) {
      groups.innerHTML = '<div class="g-empty">该素材无附加数据</div>';
    }
  },

  /** 清空缓存（重扫/数据变化后调用） */
  clearCache() { this._cache.clear(); },

  /** 初始化：拖拽条 + 恢复上次尺寸 */
  init() {
    const rx = document.getElementById("panel-resizer-x");
    const sp = document.getElementById("side-panel");
    if (rx && sp) {
      let sx = 0, sw = 0, dx = false;
      rx.addEventListener("mousedown", (e) => {
        if (!SidePanel.isOpen()) return;
        dx = true; sx = e.clientX; sw = sp.offsetWidth;
        rx.classList.add("active"); sp.style.transition = "none";
        document.body.style.userSelect = "none";
        const mv = (ev) => {
          if (!dx) return;
          let w = sw + (sx - ev.clientX);   // 拖向左边 = 加宽
          w = Math.max(220, Math.min(600, w));
          sp.style.width = w + "px"; sp.style.minWidth = "220px";
        };
        const up = () => {
          dx = false; rx.classList.remove("active"); sp.style.transition = "";
          document.body.style.userSelect = "";
          localStorage.setItem("imagedb.panelWidth", String(sp.offsetWidth));
          document.removeEventListener("mousemove", mv);
          document.removeEventListener("mouseup", up);
        };
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
      });
    }
    const ry = document.getElementById("panel-resizer-y");
    const meta = document.getElementById("panel-meta-sec");
    if (ry && meta) {
      let sy = 0, sh = 0, dy = false;
      ry.addEventListener("mousedown", (e) => {
        dy = true; sy = e.clientY; sh = meta.offsetHeight;
        ry.classList.add("active"); document.body.style.userSelect = "none";
        const mv = (ev) => {
          if (!dy) return;
          let h = sh + (sy - ev.clientY);   // 拖向上边 = 加高
          h = Math.max(100, Math.min(600, h));
          meta.style.height = h + "px";
          localStorage.setItem("imagedb.metaHeight", String(h));
        };
        const up = () => {
          dy = false; ry.classList.remove("active");
          document.body.style.userSelect = "";
          document.removeEventListener("mousemove", mv);
          document.removeEventListener("mouseup", up);
        };
        document.addEventListener("mousemove", mv);
        document.addEventListener("mouseup", up);
      });
    }
    // 恢复上次附加数据高度（宽度由 SidePanel.open() 应用）
    const savedH = parseInt(localStorage.getItem("imagedb.metaHeight") || "0", 10);
    if (savedH > 0 && meta) { meta.style.height = savedH + "px"; }
  },
};
