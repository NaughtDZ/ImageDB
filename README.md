# ImageDB 素材管理器

一个类似 Eagle 的本地素材（图片/视频）管理工具，基于 **Python (FastAPI) + SQLite + 原生 HTML/JS 前端**，
无需安装重型桌面框架，浏览器即用。所有用户数据仅保存在程序根目录的 SQLite 数据库中，绝不外泄。

## ✨ 功能特性

### 📂 目录库管理
- **导入目录**：递归扫描图片/视频**路径**入库（先入库后管理）；**后台任务 + 多线程统计 + 进度条**；
- **目录树**：启动时优先从数据库构建（不访问磁盘，速度快）；展开状态持久保留，点击子目录不折叠；
- **右键操作**：重新扫描 / 整个目录打标 / 添加图片到该目录 / 校验缺失 / **从库中移除（仅删记录，磁盘文件不动）**；
- **删除保护**：任何删除只动数据库记录，**磁盘文件永不删除**（缩略图缓存会自动清理）。

### 🏷 插件化打标（与程序本体解耦）
- 内置打标工具：**cl-tagger**（cella110n）/ **wd14** / **自定义 LLM** / **元数据·正则（实验）**；
- **新增/移除工具 = 增删插件文件**（`app/tagging/plugins/`），无需改主程序；
- **并行打标量**（设置→打标tab，默认4）：GPU → batch 批量推理、CPU → 线程池并行；
- **视频打标**：按可配置间隔抽帧 → 逐帧打标 → 投票聚合标签；
- 手动标签：单选/多选/框选后批量添加、移除、**全局重命名/删除**（应用到所有含该标签的素材）。

### 💾 附加数据侧边栏（基础信息 + EXIF / IPTC / XMP）
- 选中素材后，右侧「素材」栏在标签之外再分隔出一个**可拖拽调高**的「附加数据」分区；侧栏整体宽度也可拖拽调节（尺寸自动记忆）；
- **基础信息**：文件名、完整路径、类型、大小、分辨率、创建/修改时间；图片显示**格式编码**，视频显示**编码格式、平均码率、时长**；
- **附加元数据**：**EXIF**（设备/拍摄参数等）、**IPTC**（IIM：关键词/标题/作者/版权）、**XMP**（dc:subject/title/creator 等）；
- **按需只读读取，不写数据库**：点开即从文件读取，解析失败返回空，绝不抛错（与程序解耦）；多选时默认显示第一张，结果会话内内存缓存。

### 🗂 标签迁移（.imgtag 侧车）
- 每个目录一个 **`.imgtag`**（SQLite）保存该目录内媒体文件的标签（含 `source`/`confidence`），**随文件走**，换盘符/挪目录后无需 AI 重打标；
- **导出**：选中单图/多图或整棵目录树 → 把标签写入各目录的 `.imgtag`（不修改原图、不写主库）；导出完成后**列出写入失败的目录**（只读盘/权限等）；
- **导入**：读取目录树下的 `.imgtag`，按文件名匹配回主库并写回标签（`source=import`）；导入对话框可勾选**「覆盖旧导入标签」**（清掉旧的 `source=import`，不影响手动标签）；默认追加去重、不动手动标签；
- **迁移自检**：顶栏「🔍 迁移自检」核对目录树内 **主库媒体 vs 磁盘 vs `.imgtag`** 三方一致性（缺 `.imgtag` 的目录 / `.imgtag` 孤儿引用 / 未覆盖的媒体）；
- **兼容性**：`.imgtag` 是标准 SQLite，脱离软件也可用 Python 标准库 `sqlite3` 读取；
- **日常读写仍以 `data/imagedb.sqlite` 主库为准**；`.imgtag` 仅在显式导出/导入时使用；程序扫描目录会**自动跳过** `.imgtag`/`.txttag`。

### 🔍 多维搜索
- 按**文件名**、**目录名**、**标签**、**类型**筛选；
- **标签搜索支持空格/逗号/顿号分隔多个标签，默认取交集（AND）**；兼容含空格标签（如 `long hair`）；
- 多标签 OR 模式（`tag_any`）也保留。

### 🧹 缺失自动清理
- 启动时、后台定时、点击目录时、打开文件时都会校验磁盘存在性；
- 文件/目录被外部删除后，数据库条目**自动即时清理**（缩略图缓存同步清除）。

### 🎬 视频处理
- 视频缩略图自动取帧；按可配置时间间隔抽帧打标；
- 查看器支持进度条拖动（HTTP Range）、快进快退 10 秒、倍速、音量。

### 👁 内置查看器
- 图片：缩放（滚轮/按钮）、平移、适应窗口、**滚轮切换图片**、Shift+滚轮缩放；
- 视频：播放/暂停、进度条、快进快退、倍速、音量、全屏；
- **时间可控幻灯片**（1~60 秒，循环播放，视频播完自动下一张）；
- 键盘导航：←→切换、F 全屏、S 幻灯片、空格播放、+/- 缩放、0 适应、Esc 关闭；
- **相邻预加载**：查看时提前加载下一张原图，切换零等待。

### 🖼 画廊与分页（核心优化）
- **分页翻页**（替代"加载更多"）：每次一页，翻页丢弃旧页内容省内存；每页数量可设（10~500，记忆）；
- **翻页导航**：首页/上一页/下一页/末页/页码跳转，显示"第 X / Y 页"；
- **缩略图三层缓存**：硬盘持久化（data/thumbs）+ 浏览器 HTTP 缓存 + **前端内存 LRU（400 张）**；
- **响应优先渲染**：翻页立即显示**文件名/徽标/时长**（卡片框架），缩略图异步懒加载 + 淡入；
- **翻页中断旧页加载**：用 AbortController 中断旧页缩略图请求，只加载目标页；
- **缩略图磁盘缓存上限**：默认 200MB，超限按 mtime LRU 清理最旧（0=不限制）；
- **浏览状态保存**：翻页后自动存当前目录/页码到 localStorage，下次启动可恢复；
- 多选：单击 / Ctrl+单击 / **Shift 区间** / **空白拖拽框选**；缩略图大小滑块（100~400px，记忆）。

### 🌐 网络与代理
- **代理 GUI**：设置页配置代理服务器（模型下载、pip 更新、LLM 调用均走代理），支持连通性测试；
- **HF Token**：配置 HuggingFace Access Token 下载受限（gated）模型；
- **模型下载**：支持仓库主页/文件树/具体文件/裸 repo_id 链接，HF API 自动获取文件列表；
- **一键安装 DirectML**：设置页自动卸载普通 onnxruntime 并安装 GPU 加速版。

## 🚀 快速开始

要求：Python 3.10+。

**Windows 双击 `start.bat`**（首次自动创建虚拟环境并安装依赖）。手动方式：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py                    # 自动打开浏览器 http://127.0.0.1:8000
```

> 启动时若端口被占用（如残留进程），**自动探测下一个可用端口**，不会崩溃。

### 可选依赖

```bash
# ⭐ Windows 显卡 GPU 加速（强烈推荐，速度提升数十倍）
#    设置页 → 依赖更新 → 一键安装 DirectML 更方便
pip install onnxruntime-directml

# 纯 CPU 回退（默认）
pip install onnxruntime

# 视频抽帧 / 视频缩略图
pip install opencv-python-headless
```

> ⚠️ onnxruntime 与 onnxruntime-directml 二选一，装完 DirectML 需**重启程序**。

## 📖 使用流程

1. **导入目录**：顶栏「导入目录」（或左侧树 ＋）→ 输入目录绝对路径 → **进度条显示导入进度**；
2. **打标**：单选/多选/框选媒体，或右键目录「整个目录打标」→ 选工具开始（自动并行加速）；
3. **翻页浏览**：翻页立即看到文件名，缩略图陆续浮现；翻回已访问页秒显；
4. **查看**：双击缩略图打开查看器（滚轮切图、幻灯片、视频控制）；选中素材后右侧「素材」栏可看**标签**与**附加数据**（基础信息 + EXIF/IPTC/XMP）；
5. **搜索**：文件名 / 目录名 / 标签（空格分隔多标签 AND）；
6. **维护**：顶栏「校验缺失」手动清理；后台定时自动校验；
7. **管理**：右键目录删除记录、添加图片；选中媒体「🗑 删除选中」（仅数据库）。
8. **迁移兼容**：选中素材或目录 →「🏷 导出标签」写入各目录 `.imgtag`；迁移后回库目录 →「▼ 导入标签」读回（免 AI 重打标）。

## 🛠 打标工具配置

### cl-tagger（cella110n CLIP 打标）
- 适配 **`cella110n/cl_tagger`**（v1 公开）与 **`cella110n/cl_tagger_v2`**（v2 受限）；
- **规格**：输入固定 448×448（ViT-L/14），pad 正方形 + RGB→BGR + 归一化 (x/255-0.5)/0.5；
  `cl_tagger.onnx` + `cl_tagger_tag_mapping.json`（`idx_to_tag` + `tag_to_category`）；
- **下载 v2（gated）**：设置页 → 模型下载 → 填「HF 访问令牌」→ 输入仓库地址 → 开始下载；
- 输入主页/文件树/具体文件链接或裸 repo_id 均可；也可手动放入 `data/models/cl_tagger/`。

### wd14（WD14 Tagger）
- 需要 `model.onnx` + `selected_tags.csv`；推荐仓库 `SmilingWolf/wd-swinv2-tagger-v3`；
- 默认过滤画质分级标签（category 9）。

### 自定义 LLM
- 设置页配置 `base_url`（OpenAI 兼容接口）、`api_key`、`model`、`prompt`；走代理。

### 元数据/正则打标（实验性）
- 通过**正则规则**从文件名或附加元数据（EXIF/IPTC/XMP）提取标签；标签以 `source=metadata` 写入，可按来源管理；
- 规则字段：`enabled / name / source(filename|metadata|all) / pattern / flags(i,m,s) / tag(模板：{match}、$1..$9、{name}) / normalize(lower|upper)`；
- 配置：设置页 → 打标 → 「元数据/正则打标（实验）」的**规则 JSON** 编辑器；保存后插件自动重载；
- 支持单个/多选/整个目录打标（实验版聚焦图片，视频抽帧暂不适用）；禁用可删除 `app/tagging/plugins/metadata.py` 或清空规则。

## 📦 标签迁移（.imgtag 侧车）使用与兼容

换盘符/挪目录时，标签随文件夹一起走，回来免 AI 重打标。

### 迁移流程
1. **导出标签**：选中单图/多图（或右键目录「整个目录树」）→ 顶栏「🏷 导出标签」→ 把各媒体标签写入其所在目录的 `.imgtag`（每目录一个 sqlite）。完成后**列出写入失败的目录**（只读盘/权限等）。
2. **迁移自检**（可选）：顶栏「🔍 迁移自检」核对 **主库媒体 vs 磁盘 vs `.imgtag`** —— 缺 `.imgtag` 的目录 / `.imgtag` 孤儿引用 / 未覆盖的媒体；迁移前看一眼更安心。
3. **迁移**：整目录（连同 `.imgtag`）拷贝到新位置/新盘。
4. **导入标签**：目录重新入库后，选中该目录 → 顶栏「▼ 导入标签」→ 按文件名匹配回主库写回标签；导入对话框可勾选 **「覆盖旧导入标签」**（只动 `source=import`，不影响手动标签）。

### 说明
- 日常标签读写**始终以 `data/imagedb.sqlite` 主库为准**；`.imgtag` 只在上述显式 导出/导入 时读写。
- `.imgtag` 按**本目录内文件名**关联（不存 media_id），故换盘符/挪目录仍能对上。

### `.imgtag` 结构（标准 SQLite，可脱离软件读取）
```sql
CREATE TABLE IF NOT EXISTS tags (
  filename  TEXT PRIMARY KEY,      -- 本目录内的文件名
  tags_json TEXT NOT NULL          -- [{"name":..., "source":..., "confidence":...}]
);
```

### 用 Python 标准库读取示例（零依赖）
```python
import sqlite3, json
conn = sqlite3.connect(r"K:\path\to\folder\.imgtag")   # 换成你的目录
for filename, tags_json in conn.execute("SELECT filename, tags_json FROM tags"):
    tags = json.loads(tags_json)
    print(filename, [t["name"] for t in tags])
conn.close()
```

## 🔌 新增/移除打标工具

程序启动时自动扫描 `app/tagging/plugins/`：

- **新增**：复制插件文件，改类名与 `PLUGIN_CLASS` 导出，实现 `tag_image()`；
- **移除**：删除插件文件即可。

插件基类：`app/tagging/base.py`（支持 batch 批量推理 + JSON/CSV/纯文本多格式标签）。

## 📁 项目结构

```
ImageDB/
├── main.py                  # 入口：初始化数据库/端口探测/启动服务
├── requirements.txt         # 依赖
├── start.bat                # Windows 一键启动（纯 ASCII）
├── data/                    # 运行时产生（已被 .gitignore 排除，不入库）
│   ├── imagedb.sqlite       # SQLite 数据库（一切操作记录）
│   ├── thumbs/              # 缩略图缓存（可设上限，LRU 清理）
│   ├── frames/              # 视频抽帧临时图
│   └── models/              # 下载的模型
├── app/
│   ├── config.py            # 配置管理（代理/打标/并行量/缓存上限，存 settings 表）
│   ├── database.py          # SQLite 层（唯一直接访问数据库的模块）
│   ├── library.py           # 目录库：导入/进度/扫描/校验/目录树/删除
│   ├── media.py             # 缩略图/视频抽帧/缓存清理/时长探测
│   ├── metadata.py          # 按需读取附加数据(EXIF/IPTC/XMP)与文件基础信息（只读，不写库）
│   ├── imagetag.py          # .imgtag 侧车：标签导出/导入迁移（每目录一个 sqlite，显式使用）
│   ├── tagging/             # 打标子系统
│   │   ├── base.py          # 插件基类 + 通用 ONNX 批量推理
│   │   ├── manager.py       # 插件管理器 + 并行任务调度（GPU batch/CPU 线程池）
│   │   └── plugins/         # 内置插件（cl_tagger / wd14 / llm / metadata）
│   ├── downloader.py        # 模型下载/URL 解析/代理测试/DirectML 安装
│   └── server.py            # FastAPI 服务层（REST API + 静态资源）
└── web/                     # 前端（纯 HTML/CSS/JS，无构建步骤）
    ├── index.html
    ├── css/style.css
    ├── favicon.svg          # 二次元风图标（大动漫眼+相册书架）
    └── js/                  # api / app / tree / gallery / viewer / tagger / tags / panel / metadata_panel / imagetag / settings
```

## 🗄 数据库与缓存说明

- 所有操作记录在 `data/imagedb.sqlite`（WAL 模式）：
  `folders` / `media_items` / `tags` / `media_tags` / `tag_jobs` / `settings`；
- **缩略图三层缓存**：硬盘文件（data/thumbs，可设上限 LRU 清理）+ 浏览器 HTTP 缓存 + 前端内存 LRU（400 张）；
- **删除/失效自动清理缩略图**：删除媒体、目录、缺失校验时都会同步删除对应缩略图文件，避免幽灵缓存堆积。

> 若数据库在网络驱动器上 WAL 较慢，可在 `app/database.py` 把 `journal_mode=WAL` 改为 `DELETE`。

## ❓ 常见问题

- **打标缺 onnxruntime**：装 `onnxruntime-directml`（显卡）或 `onnxruntime`（CPU）；
- **5090 打标慢**：默认 CPU！设置页 → 依赖更新 → 一键安装 DirectML 后重启；
- **打标报 257 by 1025**：cl_tagger 固定 448 输入，插件已强制，勿改 input_size；
- **翻页显示 1/1**：检查浏览器控制台报错（曾有 `data-loaded!=` 非法选择器，已修复）；
- **启动端口被占用**：程序自动探测备用端口；也可手动 `python main.py --port 9000`；
- **模型下载慢/失败**：设置页配置代理并测试；v2 受限模型需先填 HF Token。

## 📜 许可

仅供个人本地使用。
