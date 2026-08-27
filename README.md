# ImageDB 素材管理器

一个类似 Eagle 的本地素材（图片/视频）管理工具，基于 **Python (FastAPI) + SQLite + 原生 HTML/JS 前端**，
无需安装重型桌面框架，浏览器即用。所有用户数据仅保存在程序根目录的 SQLite 数据库中，绝不外泄。

## ✨ 功能特性

### 📂 目录库管理
- **导入目录**：递归扫描图片/视频**路径**入库（不做缩略图、不打标，先入库后管理）；
- **目录树**：启动时优先从数据库构建树状图（不访问磁盘，速度快）；
- **右键操作**：重新扫描 / 整个目录打标 / **添加图片到该目录** / 校验缺失 / **从库中移除（仅删数据库记录，磁盘文件不动）**；
- **删除保护**：任何删除操作都只动数据库记录，**磁盘文件永远不会被删除**。

### 🏷 插件化打标（与程序本体完全解耦）
- 内置三种打标工具：**cl-tagger**（cella110n）/ **wd14** / **自定义 LLM**；
- **新增/移除工具 = 增删插件文件**（`app/tagging/plugins/` 目录），无需改主程序；
- **并行打标**：设置页可调「并行打标量」（默认 4）；
  - GPU（DirectML）→ **batch 批量推理**（多图一次推理，GPU 并行度拉满）；
  - CPU → **线程池并行**（多核同时推理）；
- **视频打标**：按可配置间隔（秒）自动抽帧 → 逐帧打标 → 投票聚合标签；
- 手动标签：单选/多选/框选后批量添加、移除、**全局重命名/删除**（应用到所有含该标签的素材）。

### 🔍 多维搜索
- 按**文件名**、**目录名**、**标签**、**类型**（图片/视频）筛选；
- **标签搜索支持空格/逗号/顿号分隔多个标签，默认取交集（AND）**；
- 兼容含空格标签（如 `long hair`），多词标签可整体搜索；
- 多标签 OR 模式（`tag_any`）也保留。

### 🧹 缺失自动清理
- 启动时、后台定时（可设间隔）、点击目录时、打开文件时都会校验磁盘存在性；
- 文件/目录被外部删除后，数据库条目**自动即时清理**（缩略图同步清除）。

### 🎬 视频处理
- 视频缩略图自动取帧（可设置取帧位置）；
- 按可配置时间间隔抽帧打标，作为分类管理用途；
- 查看器支持进度条拖动（HTTP Range）、快进快退 10 秒、倍速、音量。

### 👁 内置查看器
- 图片：缩放（滚轮/按钮）、平移拖动、适应窗口、**滚轮切换图片**、Shift+滚轮缩放；
- 视频：播放/暂停、进度条、快进快退、倍速、音量、全屏；
- **时间可控幻灯片**（1~60 秒间隔，循环播放，视频播完自动下一张）；
- 键盘导航：←→切换、F 全屏、S 幻灯片、空格播放、+/- 缩放、0 适应、Esc 关闭；
- **相邻预加载**：查看时提前加载下一张原图，切换零等待。

### 🖼 画廊与交互
- 多选：单击 / Ctrl+单击 / **Shift 区间选择** / **空白拖拽框选**；
- **缩略图大小滑块**（100~400px，实时调节，记忆上次设置）；
- **按需渲染**：IntersectionObserver 只加载视口内的缩略图，大图库不卡顿；
- **右侧标签栏**：点击缩略图即显示标签并集，点击标签筛选、✎ 重命名、✕ 删除、输入框添加（应用到所有选中项）。

### 🌐 网络与代理
- **代理 GUI**：设置页配置代理服务器（模型下载、pip 更新、LLM 调用均走代理），支持连通性测试；
- **HF Token**：配置 HuggingFace Access Token 下载受限（gated）模型；
- **模型下载**：支持仓库主页链接 / 文件树链接 / 具体文件链接 / 裸 repo_id，通过 HF API 自动获取文件列表；
- **一键安装 DirectML**：设置页可自动卸载普通 onnxruntime 并安装 GPU 加速版。

## 🚀 快速开始

要求：Python 3.10+。

**Windows 双击 `start.bat`**（首次自动创建虚拟环境并安装依赖）。手动方式：

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 启动
python main.py                    # 自动打开浏览器 http://127.0.0.1:8000
```

### 可选依赖（按需安装）

```bash
# ⭐ Windows 显卡 GPU 加速（强烈推荐，速度提升数十倍）
#    设置页 → 依赖更新 → 一键安装 DirectML 更方便
pip install onnxruntime-directml   # 自动替换普通 onnxruntime

# 纯 CPU 回退（默认已装）
pip install onnxruntime

# 视频抽帧 / 视频缩略图（需要视频功能时）
pip install opencv-python-headless
```

> ⚠️ onnxruntime 与 onnxruntime-directml 二选一，不能同时安装。
> 装完 DirectML 后需**重启程序**，打标自动走 GPU。

## 📖 使用流程

1. **导入目录**：顶栏「导入目录」（或左侧树 ＋）→ 输入目录绝对路径 → 路径入库；
2. **打标**：单选/多选/框选媒体，或右键目录「整个目录打标」→ 选工具开始。
   视频自动按间隔抽帧打标；也可在右侧栏/「✎ 标签」手动添加；
3. **查看**：双击缩略图打开查看器（滚轮切图、幻灯片、视频控制）；
4. **搜索**：文件名 / 目录名 / 标签（空格分隔多标签 AND）；
5. **维护**：顶栏「校验缺失」手动清理；后台定时自动校验；
6. **管理**：右键目录可删除记录、添加图片；选中媒体可「🗑 删除选中」（仅数据库）。

## 🛠 打标工具配置

### cl-tagger（cella110n CLIP 打标）
- 适配 **`cella110n/cl_tagger`**（v1 公开）与 **`cella110n/cl_tagger_v2`**（v2 受限）两个仓库；
- **规格**：输入固定 448×448（ViT-L/14），pad 正方形 + RGB→BGR + 归一化 (x/255-0.5)/0.5；
  模型文件 `cl_tagger.onnx` + 标签映射 `cl_tagger_tag_mapping.json`
  （`idx_to_tag` + `tag_to_category`，类别含 Rating/General/Character 等）；
- **下载 v2（gated）**：设置页 → 模型下载 → 填「HF 访问令牌」（https://huggingface.co/settings/tokens，
  需已通过授权）→ 输入仓库地址 → 开始下载；
- 输入仓库主页/文件树/具体文件链接或裸 repo_id 均可，程序自动解析并下载；
- 也可手动把模型文件放入 `data/models/cl_tagger/`。

### wd14（WD14 Tagger）
- 需要 `model.onnx` + `selected_tags.csv`；
- 推荐仓库 `SmilingWolf/wd-swinv2-tagger-v3`（设置页一键下载）；
- 默认过滤画质分级标签（category 9），可在配置开启 include_rating。

### 自定义 LLM
- 设置页配置 `base_url`（OpenAI 兼容接口，如本地 vLLM）、`api_key`、`model`、`prompt`；
- 支持任意视觉模型，走代理。

## 🔌 新增/移除打标工具

程序启动时自动扫描 `app/tagging/plugins/`：

- **新增**：复制插件文件，改类名与 `PLUGIN_CLASS` 导出，实现 `tag_image()`；
- **移除**：删除插件文件即可。

插件基类：`app/tagging/base.py`（`TaggerPlugin` 抽象类 + `OnnxTaggerPlugin` 通用 ONNX 实现，
支持 batch 批量推理与多格式标签文件：JSON 映射 / CSV / 纯文本）。

## 📁 项目结构

```
ImageDB/
├── main.py                  # 入口：初始化数据库/配置/启动服务
├── requirements.txt         # 依赖
├── start.bat                # Windows 一键启动（纯 ASCII，避免编码问题）
├── data/                    # 运行时生成（已被 .gitignore 排除，不入库）
│   ├── imagedb.sqlite       # SQLite 数据库（一切操作记录）
│   ├── thumbs/              # 缩略图缓存
│   ├── frames/              # 视频抽帧临时图
│   └── models/              # 下载的模型
├── app/
│   ├── config.py            # 配置管理（代理/打标参数/并行量，存于 settings 表）
│   ├── database.py          # SQLite 层（唯一直接访问数据库的模块）
│   ├── library.py           # 目录库：导入/扫描/缺失校验/目录树/删除
│   ├── media.py             # 缩略图/视频抽帧/时长探测
│   ├── tagging/             # 打标子系统
│   │   ├── base.py          # 插件基类 + 通用 ONNX 批量推理
│   │   ├── manager.py       # 插件管理器 + 并行任务调度（GPU batch / CPU 线程池）
│   │   └── plugins/         # 内置插件（cl_tagger / wd14 / llm）
│   ├── downloader.py        # 模型下载/URL 解析/代理测试/DirectML 安装
│   └── server.py            # FastAPI 服务层（32 个 REST API + 静态资源）
└── web/                     # 前端（纯 HTML/CSS/JS，无构建步骤）
    ├── index.html
    ├── css/style.css
    └── js/                  # api / app / tree / gallery / viewer / tagger / tags / panel / settings
```

## 🗄 数据库说明

所有操作记录都在 `data/imagedb.sqlite`（WAL 模式）：

- `folders`：目录树（path 唯一，parent_id 构成树，is_root 标记导入根目录）；
- `media_items`：媒体文件路径与元信息（type 区分 image/video，含缩略图路径）；
- `tags` / `media_tags`：标签与媒体-标签关联（含置信度、来源 manual/cl_tagger/wd14/llm）；
- `tag_jobs`：打标任务进度；
- `settings`：应用配置（代理、打标参数、并行量等）。

> 若数据库位于**网络驱动器**上 WAL 较慢，可在 `app/database.py` 把 `journal_mode=WAL` 改为 `DELETE`。

## ❓ 常见问题

- **打标提示缺少 onnxruntime**：装 `onnxruntime-directml`（显卡）或 `onnxruntime`（CPU）；
- **5090 打标慢**：默认是 CPU 推理！设置页 → 依赖更新 → 一键安装 DirectML 后重启；
- **视频无缩略图/无法抽帧**：装 `opencv-python-headless`；
- **模型下载慢/失败**：设置页配置代理并测试后重试；v2 受限模型需先填 HF Token；
- **打标报 257 by 1025**：cl_tagger 固定 448 输入，插件已强制，勿手动改 input_size；
- **启动端口被占用**：`python main.py --port 9000`；
- **浏览器没自动打开**：手动访问 http://127.0.0.1:8000。

## 📜 许可

仅供个人本地使用。
