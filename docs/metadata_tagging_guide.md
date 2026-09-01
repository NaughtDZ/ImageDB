# 元数据 / 正则打标（实验）使用指南

> 适用版本：ImageDB 1.0 · 功能「元数据/正则打标（实验）」
> 本功能**不需要 AI 模型**、不用下载模型、无需显卡。它靠你写的**正则规则**，直接从 **文件名** 或 **文件的附加元数据（EXIF/IPTC/XMP）** 里把标签抓出来。
> 想让 AI 帮你生成提取规则？见 [`docs/metadata_tagging_prompt.md`](docs/metadata_tagging_prompt.md)。

---

## 一、这个功能是干嘛的？

普通打标靠 cl-tagger / wd14 / LLM 等模型，要下载模型、占用 GPU/API。
而「元数据/正则打标」是一个**轻量规则引擎**：

- 对每张图，程序会读取：
  - **文件名**（含扩展名全名，以及去掉扩展名的主名）
  - **附加元数据**（EXIF / IPTC / XMP / 基础信息里的所有文本值）
- 再按你配置的**规则**逐条做**正则匹配**；命中就把「模板拼出的文本」当作标签写进库。
- 标签来源记为 <code>source=metadata</code>，可与其他来源区分、单独筛选/管理。

**好处**：零模型、零成本、毫秒级；特别适合文件名有规律（系列名/编号/作者）或元数据已带关键词的素材。

---

## 二、怎么找到它？

1. 顶栏 **⚙ 设置** → 点 **「打标」** 标签页。
2. 在「打标工具配置」里找到 <code>metadata</code>（显示名「元数据/正则打标（实验）」）。
3. 会看到一个 **「规则(JSON)」** 文本框，用来写规则。
4. 使用时：**选中单图/多图** → 顶栏 **🏷 打标**；或在 **目录上右键 → 整个目录打标**，在工具列表选 **「元数据/正则打标（实验）」**。

> 工具列表是程序**自动扫描插件目录**得到的，所以该工具会自动出现，无需额外开关。

---

## 三、规则字段（重点）

每条规则是一个 JSON 对象：

| 字段 | 必填 | 含义 | 取值/示例 |
|------|----|----|----|
| <code>enabled</code> | 否 | 是否启用 | <code>true</code> / <code>false</code>（默认 true） |
| <code>name</code> | 否 | 规则名（可用于标签模板） | <code>"系列号"</code> |
| <code>source</code> | 否 | 对哪类文本跑正则 | <code>"filename"</code> 文件名 / <code>"metadata"</code> 元数据 / <code>"all"</code> 两者，默认 filename |
| <code>pattern</code> | **是** | 正则表达式 | 见下方“反斜杠说明”；示例 <code>"^(.*?)[_-]\\d+$"</code> |
| <code>flags</code> | 否 | 正则选项 | 可组合 <code>i</code>(忽略大小写)、<code>m</code>(多行)、<code>s</code>(点匹配换行)，如 <code>"im"</code> |
| <code>tag</code> | 否 | 标签模板 | 支持 <code>{match}</code>、<code>$1</code>..<code>$9</code>、<code>{name}</code>，默认 <code>{match}</code> |
| <code>normalize</code> | 否 | 大小写归一 | <code>"lower"</code> / <code>"upper"</code> / 空 |
| <code>split</code> | 否 | 命中后把标签按该分隔符拆成多个 | 如 <code>"_"</code>、<code>","</code>（正则） |

**标签模板里能写：**
- <code>{match}</code>（整段匹配到的文本）
- <code>$1</code> .. <code>$9</code>（正则里第 1~9 个**捕获组**）
- <code>{name}</code>（这条规则的 name）

**关于反斜杠（重要，别踩坑）：**
- <code>pattern</code> 是 **JSON 字符串**。JSON 字符串里，一个“真正的反斜杠”要写成**两个反斜杠**。
- 例：正则里表示“数字”的 <code>\d</code>（一个反斜杠 + d），在 JSON 文本框里要输入 **<code>\\d</code>（两个反斜杠）**。同理 <code>\w</code>、<code>\s</code> 都这样。

---

## 四、可直接照抄的例子

把下面这段 JSON **数组** 粘进「规则(JSON)」文本框（文本框里放的是**数组**，不是带 "rules" 外壳的对象）：

    [
      {
        "enabled": true,
        "name": "从文件名提取系列名",
        "source": "filename",
        "pattern": "^(.*?)[_-]\\d+",
        "tag": "$1",
        "normalize": "lower"
      },
      {
        "enabled": true,
        "name": "元数据关键词直接当标签",
        "source": "metadata",
        "pattern": "(vtuber|nijisanji|hololive)",
        "tag": "$1"
      },
      {
        "enabled": true,
        "name": "文件名出现关键词就加标签",
        "source": "filename",
        "pattern": "(long hair|solo|nude)",
        "tag": "$1",
        "flags": "i"
      },
      {
        "enabled": true,
        "name": "[tag] 后的下划线标签批量导入",
        "source": "filename",
        "pattern": "\\[tag\\]_(.+?)(?:\\.[a-z0-9]+)?$",
        "tag": "$1",
        "split": "_",
        "normalize": "lower"
      }
    ]

> 上面用到 <code>\\d</code> 的地方就是“JSON 里写两个反斜杠”的写法，直接照抄即可。

---

## 五、逐条解析上面例子

### 例1：从文件名提取「系列名」
文件 <code>series_a_123_p0.jpg</code>：
- <code>pattern</code> = <code>^(.*?)[_-]\\d+</code>：从开头抓，直到遇到「下划线/横杠 + 数字」；正则里就是“<code>\d</code> 表示数字”。
- <code>$1</code> 就是 <code>series_a</code>（第 1 个捕获组）
- 结果标签：<code>series_a</code>；<code>normalize:"lower"</code> 会转小写

### 例2：元数据关键词当标签
- <code>source:"metadata"</code>：读 EXIF/IPTC/XMP/基础信息里所有文本值
- <code>pattern:"(vtuber|nijisanji|hololive)"</code>：任一值出现其一即命中
- <code>$1</code> 取命中的词 → 标签 <code>vtuber</code> / <code>nijisanji</code> / <code>hololive</code>

### 例3：文件名出现关键词就加标签
- 用 <code>|</code>（或）列举关键词，命中即加
- <code>flags:"i"</code> 忽略大小写，<code>Long Hair</code> / <code>long hair</code> 都能命中

### 例4：把 <code>[tag]</code> 后面用下划线分隔的多个标签批量导入
文件名形如 <code>Recent Stuff_149092984_p21_[tag]_R-18_Koikatsu_きのこ_エジプト娘_猫耳_モンスター娘_巨乳_褐色ちゃん_蛾_アヌビス_AhaNubis.jpg</code>：
- <code>pattern</code>=<code>\\[tag\\]_(.+?)(?:\\.[a-z0-9]+)?$</code>：定位到 <code>[tag]</code> 之后，把到扩展名之前的内容整体抓进 <code>$1</code>；
- <code>split:"_"</code>：再把 <code>$1</code> 用下划线拆成一个个标签（如 <code>R-18</code>、<code>Koikatsu</code>、<code>猫耳</code>……）；
- <code>normalize:"lower"</code> 可选，把标签统一小写。
- 例：上面的文件名会得到 <code>r-18</code>、<code>koikatsu</code>、<code>猫耳</code>…（拆出多少个就多少个）。

---

## 六、怎么真正跑起来

1. **写规则**：设置 → 打标 → <code>metadata</code> → 在「规则(JSON)」文本框粘贴/编辑上面的数组。
2. **保存**：点对话框底部 **保存设置**，右下角提示「设置已保存（打标插件已重载）」即生效。
3. **选素材**：单选/多选图片，或右键目录 → 整个目录打标。
4. **选工具开始**：顶栏 **🏷 打标** → 工具选 **「元数据/正则打标（实验）」** → 范围选「选中项」或「当前目录」。
   - （可选）勾选 **覆盖该工具的旧标签**：先清掉该媒体旧的 <code>source=metadata</code> 标签再写；**不影响手动标签**。
   - 点 **开始打标**，进度走完即可。
5. **看结果**：打开某张图，右侧「素材」栏会显示标签，<code>metadata</code> 来源的就是本规则打出的。

> 小技巧：先在**几张**图试试，确认标签对，再整目录跑；默认“不覆盖”会跳过已打标媒体，重复跑不重复打。

---

## 七、规则命中逻辑（知道更稳）

- 对每条启用的规则：
  - <code>source=filename</code> → 对「文件名」「去扩展名主名」两个文本做 <code>re.search</code>（任意位置命中即可）
  - <code>source=metadata</code> → 对元数据里**所有字符串值**逐一 <code>re.search</code>
  - <code>source=all</code> → 以上都试
  - 某条规则一旦命中，就用 <code>tag</code> 模板渲染出**一个**标签（每条规则最多出一条，避免重复）
- 标签会**去重**；置信度统一记为 <code>1.0</code>。
- **无模型**，不需改模型配置、不需 GPU。

---

## 八、标签去哪了？怎么管理？

- 这批标签写进主库 <code>data/imagedb.sqlite</code>，来源是 <code>metadata</code>。
- 右侧「素材」栏能看到每个标签的来源；可用现有功能**批量重命名/删除**（全局生效），也可按标签/来源检索。
- 若做了 **导出标签（.imgtag）**，它们的 <code>source</code> 会一并写入，迁移导入时按来源恢复。

---

## 九、常见问题

**Q：规则保存后没反应？** A：确认点了「保存设置」（会重载插件），并检查 JSON 语法（缺逗号/引号会提示「已重置为空规则」）。

**Q：<code>\d</code> 在 JSON 文本框里怎么输入？** A：正则里它是一个反斜杠 <code>\d</code>；但 JSON 文本框里要打**两个**连续反斜杠 <code>\\d</code>。因为 <code>pattern</code> 是 JSON 字符串，JSON 会把 <code>\\</code> 还原成一个真正的反斜杠。同理 <code>\w</code>、<code>\s</code> 也都输入两个反斜杠。

**Q：为什么对视频不起作用？** A：实验版只处理图片；视频抽帧不跑本规则。

**Q：打出的标签不想要？** A：用标签管理：把 <code>source=metadata</code> 的标签全局删除/重命名；或把规则 <code>enabled</code> 设 <code>false</code>，或删除插件文件 <code>app/tagging/plugins/metadata.py</code>（工具+规则一起消失）。

**Q：正则不熟？** A：用在线正则工具（如 regex101）先把 <code>pattern</code> 调对；我们用的是 Python 正则语法。

---

## 十、怎么禁用？

- **临时**：把规则的 <code>enabled</code> 都设 <code>false</code> 并保存。
- **彻底**：删除 <code>app/tagging/plugins/metadata.py</code>，重启程序，工具消失；想用再放回。

---

> 提示：这是**实验性**功能，建议先在少量素材跑通再推广；标签随时可清理，不影响其他来源的标签。
