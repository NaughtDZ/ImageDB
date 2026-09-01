# AI 提示词 / Skill：为「元数据/正则打标」生成提取规则

> 用途：把这份内容丢给任何 AI（或自己对照理解），它就能把你描述的**文件命名规律 / 元数据规律**，转换成**可直接粘贴进 ImageDB「规则(JSON)」文本框**的规则数组。
> 适用：ImageDB「元数据/正则打标（实验）」，标签来源 <code>source=metadata</code>。

---

## 一、你的角色（给 AI 的指令）

用户会给你：**若干文件命名示例**（或元数据字段示例）+ **期望提取成的标签**。
请你只输出一个 **JSON 数组（rules）**，可直接粘贴。除一句必要说明外不要输出别的。

输出格式（只输出数组）：

    [
      { "enabled": true, "name": "...", "source": "...", "pattern": "...", "tag": "...", "flags": "...", "normalize": "...", "split": "..." }
    ]

---

## 二、规则 schema（每条规则一个对象）

- <code>enabled</code>：否，是否启用，<code>true</code>/<code>false</code>，默认 <code>true</code>。
- <code>name</code>：否，规则名（可用于标签模板 <code>{name}</code>）。
- <code>source</code>：否，对哪类文本跑正则。<code>"filename"</code> 文件名 / <code>"metadata"</code> 元数据 / <code>"all"</code> 两者；默认 <code>"filename"</code>。
- <code>pattern</code>：**是**，正则表达式（**注意 JSON 反斜杠转义，见下**）。
- <code>flags</code>：否，<code>i</code> 忽略大小写、<code>m</code> 多行、<code>s</code> 点匹配换行，可组合如 <code>"im"</code>。
- <code>tag</code>：否，标签模板。支持 <code>{match}</code>（整段匹配）、<code>$0</code>..<code>$9</code>（捕获组）、<code>{name}</code>（规则名）；默认 <code>{match}</code>。
- <code>normalize</code>：否，<code>"lower"</code> / <code>"upper"</code> / 空。
- <code>split</code>：否，把渲染结果**拆成多个标签**。一个正则分隔符，如 <code>"_"</code>、<code>","</code>、<code>"\\s+"</code>。

---

## 三、匹配逻辑（生成时必须遵守）

- <code>source=filename</code> → 程序对「完整文件名（含扩展名）」和「去扩展名的主名」两个文本做 <code>re.search</code>（任意位置命中即可）。
- <code>source=metadata</code> → 程序对文件 **EXIF/IPTC/XMP/基础信息**里的**所有字符串值**逐一 <code>re.search</code>。
- <code>source=all</code> → 以上都试。
- **一条规则**命中后，用 <code>tag</code> 模板渲染出**一个**字符串；若设了 <code>split</code>，再用它拆成**多个**标签。
- 标签**自动去重**；置信度固定 <code>1.0</code>；<code>normalize</code> 只管大小写。
- 所有标签写入 <code>source=metadata</code>。

---

## 四、反斜杠（极易踩坑，务必遵守）

<code>pattern</code> 是 **JSON 字符串**。正则里**一个反斜杠，在 JSON 里要写成两个**：

| 正则里（1 个反斜杠） | JSON 里要写（2 个反斜杠） |
|----|----|
| <code>[</code> 的字面量 <code>\[</code> | <code>\\[</code> |
| <code>[</code> 的结尾 <code>\]</code> | <code>\\]</code> |
| 匹配字面点 <code>\.</code> | <code>\\.</code> |
| 数字 <code>\d</code> | <code>\\d</code> |
| <code>\w</code> / <code>\s</code> | <code>\\w</code> / <code>\\s</code> |
| 左括号 <code>\(</code> | <code>\\(</code> |

> 也就是说：**你输出的 JSON 里，正则的反斜杠一定是成对的**（如 <code>\\[</code>）。

---

## 五、常见场景模板（直接复用）

### 场景 A：<code>[tag]</code> 之后、用下划线分隔的多个标签
示例文件名：<code>Recent Stuff_149092984_p21_[tag]_R-18_Koikatsu_..._AhaNubis.jpg</code>
规则：

    {
      "enabled": true,
      "name": "[tag] 后的下划线标签批量导入",
      "source": "filename",
      "pattern": "\\[tag\\]_(.+?)(?:\\.[a-z0-9]+)?$",
      "tag": "$1",
      "split": "_",
      "normalize": "lower"
    }

说明：定位到 <code>[tag]</code> 之后，把到扩展名之前的内容抓进 <code>$1</code>，再用 <code>split:"_"</code> 拆成多个标签。结果：<code>r-18</code>、<code>koikatsu</code>、<code>猫耳</code>……

### 场景 B：从文件名提取「系列 / 前缀」（去掉编号）
输入：<code>series_a_123_p0.jpg</code> → 想要 <code>series_a</code>
规则：

    {
      "enabled": true,
      "name": "从文件名提取系列名",
      "source": "filename",
      "pattern": "^(.*?)[_-]\\d+",
      "tag": "$1",
      "normalize": "lower"
    }

### 场景 C：文件名里出现某关键词就加标签
输入：<code>xxx_long hair_yyy.jpg</code> → 想要 <code>long hair</code>（忽略大小写）
规则：

    {
      "enabled": true,
      "name": "文件名关键词",
      "source": "filename",
      "pattern": "(long hair|solo|nude)",
      "tag": "$1",
      "flags": "i"
    }

### 场景 D：把元数据里的关键词全部当标签
规则：

    {
      "enabled": true,
      "name": "元数据关键词",
      "source": "metadata",
      "pattern": "(vtuber|nijisanji|hololive)",
      "tag": "$1"
    }

### 场景 E：多个分隔符混合（如 <code>[tag]</code> 后 空格/逗号/下划线）
规则：

    {
      "enabled": true,
      "name": "[tag] 后多分隔拆标签",
      "source": "filename",
      "pattern": "\\[tag\\]_(.+)$",
      "tag": "$1",
      "split": "[_,]"
    }

---

## 六、给用户的输入模板（复制这段给 AI）

> 请为以下文件命名规律生成「元数据/正则打标」的规则数组（source=filename 或 metadata；多用 split 支持多标签）：
> - 文件命名示例：<贴 3~5 个真实文件名>
> - 元数据（如有）：<如 IPTC 关键词、XMP subject>
> - 我想提取成标签：<说明，如“[tag] 后每个下划线段”、或“从开头提取系列名”>
> 只输出 JSON 数组，可附一句备注。

---

## 七、AI 生成后自查清单

- [ ] <code>pattern</code> 里的正则反斜杠，在 JSON 里是否都写成了**两个**（如 <code>\\[</code>）。
- [ ] 需要的标签是否都在；有没有误命中（用 <code>flags:"i"</code> / 更精确的 <code>pattern</code> 控制）。
- [ ] 多标签场景是否用了 <code>split</code>（否则一条规则只出一个标签）。
- [ ] <code>source</code> 选对：文件名规律用 <code>filename</code>；元数据规律用 <code>metadata</code>。
- [ ] 输出是否为**数组** <code>[ ... ]</code>（不是带 <code>rules</code> 外壳的对象）。

---

> 说明：规则保存在 设置页 → 打标 → <code>metadata</code> 的「规则(JSON)」文本框；保存后自动重载插件。彻底禁用可删 <code>app/tagging/plugins/metadata.py</code>。
