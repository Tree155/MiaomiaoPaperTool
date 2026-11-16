# 英文化工具 / English build generator

`源码/en/` 里的英文版源码不是手写的，是由 `源码/` 的中文源码**生成**的。

```
python tools/i18n/make_en.py            # 生成 + 校验
python tools/i18n/make_en.py --check    # 只校验，不写文件
```

要求 Python 3.11+，无第三方依赖。

## 它怎么做的

不是全文替换，而是**按词法位置**改字面量：

| 文件类型 | 做法 |
|---|---|
| `*.py` | `tokenize` 定位每个 `STRING` / `COMMENT` / `FSTRING_MIDDLE` 的精确区间，只改区间内的文本。代码、缩进、运算符一个字节不动。 |
| `ui.html` | 先按 `<script>` / `<style>` 切成段；段内识别字符串（`'` `"` `` ` ``）与注释，段外只认 HTML 文本节点和属性值。跨行的裸引号不会被误判成字符串。 |

替换时会自动处理引号转义（`'` 定界的串里把裸 `'` 转成 `\'`，反引号串里把 `\'` 还原），
所以不用手工维护转义。

## 校验（任一不过就报错退出）

1. 每个 `.py` 的 **AST 骨架与中文版完全一致**（抹掉字符串常量后比较）——保证没有把代码改坏
2. `py_compile` 通过
3. 除白名单与保护区的字面量外，不残留中文

任一条不通过，`make_en.py` 返回非 0，`源码/en/` 不会被当成可发布状态。

## 新增中文了怎么办

改了中文源码、加了新的中文文案后直接跑 `make_en.py`，它会列出**所有没有译文的条目**：

```
[!] 有 3 处中文没有对应译文：
  ui          '新的按钮文字'
  py_string   '新的日志: %s'
  py_fstring  '，失败 '
    把缺的条目补进 tools\i18n\dict.json 后重跑。
```

打开 `dict.json`，把这几条按格式补进对应分区，再跑一次即可。

## dict.json 的五个分区

| 分区 | key | value |
|---|---|---|
| `ui` | `ui.html` 里的文本 / 字符串内容 | 英文 |
| `ui_comments` | 整条注释（含 `//` `/* */` `<!-- -->` 标记） | 英文注释 |
| `py` | Python 字符串字面量的**内文**（不含引号和前缀） | 英文内文 |
| `py_comments` | 整条注释（含 `#`） | 英文注释 |
| `py_fstrings` | f-string 里被 `{表达式}` 切开后的单段文本 | 英文片段 |

注意 `ui` 与 `py` 分开：同一个中文词在界面里和在程序里译法可能不同（例如「文献库」在界面是
`Library`，在后端是目录名 `library`）。

`py_fstrings` 的译文必须**保留首尾空格**——这些片段会和相邻片段拼回一整句，空格错了就粘在一起。

## 不该翻的东西

三类，都在 `make_en.py` 里显式处理，**不要**给它们加字典：

| 类型 | 处理方式 | 例子 |
|---|---|---|
| 正则 / 外部数据匹配值 | `KEEP_AS_IS` 白名单，原样保留 | `区`（easyScholar 返回 `1区`）、`否` / `无`（top 字段返回值）|
| 中文查询表 | `PROTECT_VARS` 整块保护 | `ZH_TERMS`（中文词 → 英文，翻了用户输入中文就查不到）|
| 打包脚本 | `SKIP_FILES` 跳过 | `pack_release.py`（不随英文版发布）|

另外 `normalise()` 会把译文里的全角标点转成半角（`（`→`(`、`，`→`, `、`。`→`. `），
但会保留 `…` 和 `—`，这两个在英文排版里是正常字符。

## 英文版专有的界面微调

`UI_PATCHES` 与 `UI_CSS` 里放了几处只在英文版生效的改动：

- 中科院分区显示成 `Q1`–`Q4`，而不是 easyScholar 返回的 `1区`
- 搜索按钮 `min-width:96px`（`Search` 比「检索」宽，不加会被 flex 压扁、文字溢出）
- 年份输入框加宽到 88px
- 三个弹层略微加宽（英文比中文长约 1.6 倍）

这些用字符串锚点定位，锚点找不到会打印 `[warn]`，不会静默失效。

## 英文版删掉的功能（不是翻译）

有些东西对中文构建才有意义，英文版直接不给入口，用 `EN_STRIP_UI` 整块删掉：

| 删掉的 | 位置 |
|---|---|
| 标题下的中文译文行 | `zhCell()` 的调用点 |
| 摘要框里的 "Translate" 按钮与 MyMemory 提示 | `absBox()` 里的 `.absbar` 两行 |
| easyScholar 弹层的「显示中文译文」开关及它的 onchange | `#jmZh` |
| 导出 Excel 的 `Chinese title` 列 | `EXPORT_COLS` 首项 + 导出弹层文案 |

后端 `/api/zh` 与 `/api/zh_abs` 改成直接返回 `{"note":"disabled"}`（`PY_PATCHES`）。
`zh_to_en`（中文检索词转英文）**保留**——那是搜索功能，不是显示翻译。

被删掉入口后，`zhCell` / `zhStart` / `zhRepaint` / `loadAbsZh` / `showAbsZh` / `ABS_ZH` 成为死代码。
留着比删掉安全（删函数容易碰到引用），只要**不要**把 `render()` 里的 `zhStart(...)` 调用恢复回去。

## 几个不能翻的「数据词」

`KEEP_AS_IS` 与 `PROTECT_VARS` 就是为了别把它们译掉：

| 保留内容 | 为什么 |
|---|---|
| `区` / `否` / `无` | easyScholar 返回的中文值，翻了 `top` 判断和分区数字提取就失效 |
| `次数` / `限制` | easyScholar 配额耗尽的提示是中文，只留英文关键词永远匹配不到 |
| `ZH_TERMS` | 中文词 → 英文查询表，key 翻了用户输中文就查不到 |
| `CJK_STOP` | 中文停用字集合；`set(str)` 是逐字符切分，换成英文单词会让整句过滤失效 |
| `_F_CAS` / `_F_CAS_BASE` / `_F_TOP` | easyScholar 响应字段名，含中文候选 |
| `FIELD_ALIAS` | 检索前缀别名（含 `题名` `标题` 等中文别名） |

这些是「跟外部数据打交道的常量」，翻成英文等于把功能翻译没了。判断标准：这个字符串是不是用来和程序外部的东西比对？

## 发布前自检：`smoke_ui.py`

```bash
python tools/i18n/smoke_ui.py                          # 直接跑 源码/en/（复制到临时目录，不动仓库）
python tools/i18n/smoke_ui.py --exe path/to.exe        # 跑打包产物
python tools/i18n/smoke_ui.py --fixture results.json   # 灌真实检索结果，结果表才有内容
python tools/i18n/smoke_ui.py --json report.json       # 明细落盘
```

退出码 0 = PASS，1 = 有问题。

检查四项，任一不过就 FAIL：

| 检查 | 判据 | 为什么容易出问题 |
|---|---|---|
| **文本截断** | `scrollWidth > clientWidth + 1` | 英文比中文长约 1.6 倍，按钮、表头、下拉选项最容易挤爆 |
| **溢出视口** | 元素右边界 > 窗口宽 | 弹层、chip、长路径会跑出屏幕 |
| **中文残留** | 静态文案元素里还有 CJK 字符 | 漏翻、或数据词被误翻回去 |
| **JS 报错** | `window.onerror` / `unhandledrejection` | 删功能留下的悬空引用 |

覆盖 14 个场景：主界面、导出/引文/账户/存储/期刊指标/关于/语法帮助五个弹层、文献库页、Zotero、整理下载、库内搜索、笔记弹层、摘要展开。

**为什么不截图**：Chrome 157 起 `--headless --screenshot` 在本机静默退出（exit 0、无 stderr、不出图），Edge 同样。改用 pywebview（WebView2，与打包 exe 同内核）后 `evaluate_js` 直接量布局，比肉眼看图更准，而且 `hidden=True` 不弹窗。

**注意**：`--exe` 模式会让 exe 真跑起来（会闪一下窗口），并且用临时目录当数据目录，不会碰你自己的 `library`。
