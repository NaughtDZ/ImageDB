# ImageDB 素材管理器

一个类似 Eagle 的本地素材（图片/视频）管理工具，基于 **Python (FastAPI) + SQLite + 纯 HTML/JS 前端**，
无需安装重型桌面框架，浏览器即用。

## 功能特性

- 📂 **目录库管理**：导入任意目录（递归扫描图片/视频路径入库），程序内树状图展示；
- 🏷 **插件化打标**：内置 cl-tagger / wd14 / 自定义 LLM 三种打标工具，支持 DirectML 加速，
  工具与程序本体完全解耦（新增/移除工具 = 增删插件文件）；
- 🔍 **多维搜索**：按文件名、目录名、标签（多标签 AND/OR）、类型筛选；
- 🧹 **缺失自动清理**：启动时、后台定时、点击目录时都会校验磁盘存在性，
  文件/目录被外部删除后数据库条目自动清理；
- 🎬 **视频处理**：按可配置的时间间隔抽帧打标（分类管理用），视频缩略图自动取帧；
- 👁 **内置查看器**：图片缩放/平移/全屏，视频播放/进度条/快进快退/倍速/音量，
  时间可控的幻灯片放映；
- 🌐 **代理 GUI**：设置页配置代理服务器（下载模型、更新依赖、调用 LLM 均走代理）。

## 快速开始

要求：Python 3.10+（建议 3.11/3.12）。

Windows 双击 `start.bat` 即可（自动创建虚拟环境并安装依赖）。手动方式：

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 启动
python main.py                    # 自动打开浏览器 http://127.0.0.1:8000
```

可选依赖（按需安装，程序会自动检测）：

```bash
# Windows 显卡 DirectML 加速（cl-tagger / wd14 推荐）
pip install onnxruntime-directml

# 纯 CPU 回退
pip install onnxruntime

# 视频抽帧 / 视频缩略图
pip install opencv-python-headless
```

> 提示：onnxruntime-directml 与 onnxruntime 二选一即可，两者都装可能冲突。
> 也可以直接在程序的「设置 → 依赖更新」页输入包名安装（走代理）。

## 使用流程

1. **导入目录**：点击顶栏「导入目录」（或左侧树右上角 ＋），输入目录绝对路径。
   程序会优先把其中所有图片/视频的**路径**写入数据库（不做缩略图、不打标）。
2. **打标**：在画廊中单选/多选媒体，或右键目录选择「整个目录打标」，
   在弹出的对话框中选择打标工具并开始。视频会按设置间隔自动抽帧打标。
   （也可在「✎ 标签」中手动添加/移除标签。）
3. **右侧标签栏**：点击任意缩略图（或框选/多选）后，最右侧会显示所选素材的标签并集；
   点击标签可按它筛选；✎ 重命名 / ✕ 删除为全局操作（应用到所有含该标签的素材）；
   顶部输入框添加的标签会应用到所有已选素材。
3. **查看**：双击媒体打开查看器（图片缩放/视频播放/幻灯片）。
4. **搜索**：顶栏支持文件名 / 目录名 / 标签（逗号分隔，默认取交集）。
5. **维护**：顶栏「校验缺失」手动清理已删除文件；后台也会定时自动校验。

## 打标工具配置

### cl-tagger（cella110n CLIP 打标）
- 适配 **`cella110n/cl_tagger`**（v1，公开）与 **`cella110n/cl_tagger_v2`**（v2，受限）两个仓库，规格一致：
- **下载受限模型（gated）**：设置页 → 模型下载 → 在「HF 访问令牌」输入你的 HuggingFace
  Access Token（`hf_xxxx`，在 https://huggingface.co/settings/tokens 创建，需已通过该模型的授权申请），
  保存后点击「开始下载」即可自动携带令牌访问受限仓库；
  - 输入 448x448，预处理为 pad 正方形 + RGB→BGR + 归一化 (x/255-0.5)/0.5；
  - 模型文件 `cl_tagger.onnx` + 标签映射 `cl_tagger_tag_mapping.json`
    （JSON 格式：`idx_to_tag` + `tag_to_category`，类别含 Rating/General/Character 等）；
  - 默认只输出 General/Character 类标签，Rating 只取最高分一项（可在配置开启 include_rating）。
- 设置页 → 模型下载 → 输入仓库**主页链接**（如 `https://huggingface.co/cella110n/cl_tagger`）、
  **文件树链接**（如 `https://huggingface.co/cella110n/cl_tagger/tree/main/cl_tagger_1_02`）或裸 repo_id
  均可自动下载并配置（程序会通过 HF API 获取仓库真实文件列表，自动筛选 .onnx 与标签映射文件）；
  也可以手动把模型文件放入 `data/models/cl_tagger/`。

### wd14（WD14 Tagger）
- 需要模型目录：`model.onnx` + `selected_tags.csv`。
- 推荐仓库：`SmilingWolf/wd-swinv2-tagger-v3`（设置页一键下载）。
- 默认过滤画质分级标签（category 9），可在配置中开启 include_rating。

### 自定义 LLM
- 设置页配置 `base_url`（OpenAI 兼容接口，如本地 vLLM）、`api_key`、`model`、`prompt`。
- 支持任意支持视觉输入的模型，走代理。

## 新增/移除打标工具（插件机制）

程序启动时自动扫描 `app/tagging/plugins/` 目录：

- **新增**：复制任意插件文件，修改类名与 `PLUGIN_CLASS` 导出，实现 `tag_image()` 即可；
- **移除**：删除插件文件，或在设置页把该工具的 model_dir 置空。

插件基类：`app/tagging/base.py`（`TaggerPlugin` 抽象类 + `OnnxTaggerPlugin` 通用 ONNX 实现）。

## 项目结构

```
ImageDB/
├── main.py                  # 入口：初始化数据库/配置/启动服务
├── requirements.txt         # 依赖
├── start.bat                # Windows 一键启动
├── data/                    # 运行时生成（数据库/缩略图/抽帧/模型）
│   ├── imagedb.sqlite       # SQLite 数据库（一切操作记录）
│   ├── thumbs/              # 缩略图缓存
│   ├── frames/              # 视频抽帧临时图
│   └── models/              # 下载的模型
├── app/
│   ├── config.py            # 配置管理（代理/打标参数，存于 settings 表）
│   ├── database.py          # SQLite 层（唯一直接访问数据库的模块）
│   ├── library.py           # 目录库：导入/扫描/缺失校验/目录树
│   ├── media.py             # 缩略图/视频抽帧/时长探测
│   ├── tagging/             # 打标子系统
│   │   ├── base.py          # 插件基类 + 通用 ONNX 实现
│   │   ├── manager.py       # 插件管理器 + 打标任务调度
│   │   └── plugins/         # 内置插件（cl_tagger / wd14 / llm）
│   ├── downloader.py        # 模型下载/代理测试/pip 更新
│   └── server.py            # FastAPI 服务层（REST API + 静态资源）
└── web/                     # 前端（纯 HTML/CSS/JS，无构建步骤）
    ├── index.html
    ├── css/style.css
    └── js/                  # api / app / tree / gallery / viewer / tagger / tags / settings
```

## 数据库说明

所有操作记录都在 `data/imagedb.sqlite` 中：

- `folders`：目录树（path 唯一，parent_id 构成树，is_root 标记导入根目录）；
- `media_items`：媒体文件路径与元信息（type 区分 image/video）；
- `tags` / `media_tags`：标签与媒体-标签关联（含置信度、来源）；
- `tag_jobs`：打标任务进度；
- `settings`：应用配置（代理、打标参数等）。

数据库使用 WAL 模式。注意：**若数据库位于网络驱动器上，WAL 可能较慢或偶发锁冲突**，
此时可在 `app/database.py` 的 `_connect()` 中把 `journal_mode=WAL` 改为 `DELETE`。

## 常见问题

- **打标提示“缺少运行库 onnxruntime”**：安装 `onnxruntime-directml`（Windows 显卡）或 `onnxruntime`。
- **视频无缩略图/无法抽帧**：安装 `opencv-python-headless`。
- **模型下载慢/失败**：在设置页配置代理并测试，然后重试。
- **启动端口被占用**：`python main.py --port 9000`。
- **浏览器没自动打开**：手动访问 http://127.0.0.1:8000。

## 许可

仅供个人本地使用。