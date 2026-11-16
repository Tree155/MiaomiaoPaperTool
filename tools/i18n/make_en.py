#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_en.py -- 由中文源码生成英文版源码（源码/en/）。

    python tools/i18n/make_en.py            # 生成 + 校验
    python tools/i18n/make_en.py --check    # 只校验，不写文件

原理
----
不是简单全文替换，而是**按词法位置**替换：
  * Python：用 tokenize 定位每个 STRING / COMMENT / FSTRING_MIDDLE 的精确区间，
    只改这些区间里的字面量；代码、缩进、运算符一个字节都不动。
  * ui.html：先按 <script> / <style> 切成段，段内识别字符串（' " `）与注释，
    段外只认 HTML 文本节点和属性值。跨行的裸引号不会被当成字符串。

替换完成后会做三项校验，任一不通过就报错退出：
  1. Python：每个文件与中文版的 AST 骨架完全一致（字符串字面量内容除外）
  2. Python：py_compile 通过
  3. 两者：除白名单外不得残留中文字符

新增中文但字典里没有时，会在「未翻译」清单里列出来并失败——请补进 dict.json。
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import py_compile
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "源码")
DST = os.path.join(SRC, "en")
DICT = os.path.join(ROOT, "tools", "i18n", "dict.json")

CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_OR_WIDE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e]")


# --------------------------------------------------------------------------- 字典
def load_dict() -> dict:
    d = json.load(io.open(DICT, encoding="utf-8"))
    for k in ("ui", "ui_comments", "py", "py_comments", "py_fstrings"):
        d.setdefault(k, {})
    return d


def normalise(s: str) -> str:
    """英文译文的全角标点规范化（不动首尾空白，否则会破坏拼接）。"""
    s = re.sub(r"(?<=[\w\u4e00-\u9fff\u3000-\u303f\uff01-\uff65])\uff08", " (", s)
    s = re.sub(r"\u3002(?=\\[rn]|\s|$)", ".", s)
    for a, b in (
        ("\u3000", " "), ("\uff0c", ", "), ("\u3001", ", "),
        ("\uff1b", "; "), ("\uff1a", ": "), ("\uff01", "! "), ("\uff1f", "? "),
        ("\u3010", "["), ("\u3011", "]"),
    ):
        s = s.replace(a, b)
    s = s.replace("\uff08", "(").replace("\uff09", ")")
    s = re.sub(r"([,:;!?])(?:[ ]{2,})", r"\1 ", s)
    return s


# --------------------------------------------------------------------------- ui.html
def _segments(text: str):
    """按 <script>/<style> 切段：(start, end, kind)。"""
    segs, pos = [], 0
    for m in re.finditer(r"<(script|style)\b[^>]*>", text, re.I):
        if m.start() < pos:
            continue
        segs.append((pos, m.start(), "html"))
        tag = m.group(1).lower()
        end = text.lower().find("</%s>" % tag, m.end())
        end = len(text) if end < 0 else end
        segs.append((m.end(), end, tag))
        pos = end
    segs.append((pos, len(text), "html"))
    return segs


def scan_ui(text: str):
    """返回 (tokens, comments)。token=(start, end, kind, body, lead, tail)"""
    tokens, comments = [], []

    def scan_code(lo, hi, allow_line_cmt):
        i = lo
        while i < hi:
            if text.startswith("/*", i):
                j = text.find("*/", i + 2, hi)
                j = hi if j < 0 else j + 2
                comments.append((i, j))
                i = j
                continue
            if allow_line_cmt and text.startswith("//", i):
                j = text.find("\n", i, hi)
                j = hi if j < 0 else j
                comments.append((i, j))
                i = j
                continue
            c = text[i]
            if c in "\"'":
                q, j, ok = c, i + 1, False
                while j < hi:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == q:
                        ok = True
                        break
                    if text[j] == "\n":
                        break
                    j += 1
                if ok:
                    tokens.append((i, j + 1, "q"))
                    i = j + 1
                    continue
                i += 1
                continue
            if c == "`":
                j, depth, ok = i + 1, 0, False
                while j < hi:
                    ch = text[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "$" and j + 1 < hi and text[j + 1] == "{":
                        depth += 1
                        j += 2
                        continue
                    if ch == "}" and depth > 0:
                        depth -= 1
                        j += 1
                        continue
                    if ch == "`" and depth == 0:
                        ok = True
                        break
                    j += 1
                if ok:
                    tokens.append((i, j + 1, "tpl"))
                    i = j + 1
                    continue
                i += 1
                continue
            i += 1

    for lo, hi, kind in _segments(text):
        if kind == "html":
            i = lo
            while i < hi:
                if text.startswith("<!--", i):
                    j = text.find("-->", i + 4, hi)
                    j = hi if j < 0 else j + 3
                    comments.append((i, j))
                    i = j
                    continue
                c = text[i]
                if c in "\"'":
                    q, j, ok = c, i + 1, False
                    while j < hi:
                        if text[j] == q:
                            ok = True
                            break
                        if text[j] == "\n":
                            break
                        j += 1
                    if ok:
                        tokens.append((i, j + 1, "q"))
                        i = j + 1
                        continue
                    i += 1
                    continue
                if c == ">":
                    j = i + 1
                    while j < hi and text[j] != "<":
                        j += 1
                    if text[i + 1:j]:
                        tokens.append((i + 1, j, "text"))
                    i = j
                    continue
                i += 1
        else:
            scan_code(lo, hi, kind == "script")

    out = []
    for a, b, kind in tokens:
        raw = text[a:b]
        body = raw[1:-1] if kind in ("q", "tpl") else raw
        s = body.strip()
        if not s or not CJK_OR_WIDE.search(s):
            continue
        lead = body[: len(body) - len(body.lstrip())]
        tail = body[len(body.rstrip()):]
        out.append((a, b, kind, s, lead, tail))
    return out, comments


def rebuild_ui(text: str, d: dict, missing: list) -> str:
    tokens, comments = scan_ui(text)
    jobs = []
    for a, b, kind, body, lead, tail in tokens:
        if body in KEEP_AS_IS:
            continue
        en = d["ui"].get(body)
        if en is None:
            auto = auto_punct(body)
            if auto is None:
                missing.append(("ui", body))
                continue
            en = auto
        en = normalise(en)
        if kind in ("q", "tpl"):
            q = text[a]
            if q == "'":
                en = re.sub(r"(?<!\\)'", r"\\'", en)
            else:
                en = en.replace("\\'", "'")
            if q == '"':
                en = re.sub(r'(?<!\\)"', r'\\"', en)
            new = q + lead.replace("\u3000", " ") + en + tail.replace("\u3000", " ") + q
        else:
            new = lead.replace("\u3000", " ") + en + tail.replace("\u3000", " ")
        jobs.append((a, b, new))

    for a, b in comments:
        raw = text[a:b]
        en = d["ui_comments"].get(raw)
        if en is None:
            auto = auto_punct(raw)
            if auto is None:
                if not CJK.search(raw):
                    continue          # 纯 ASCII 注释，原样保留
                missing.append(("ui_comment", raw))
                continue
            en = auto
        jobs.append((a, b, en))

    jobs.sort(key=lambda j: j[0], reverse=True)
    out, prev = text, len(text) + 1
    for a, b, new in jobs:
        assert b <= prev, "ui: overlapping replacement"
        out = out[:a] + new + out[b:]
        prev = a
    return out.replace("\u3000", " ")


# 英文版专用补丁：界面上的功能性微调（不影响中文版）
UI_PATCHES = [
    # 中科院分区：英文界面显示 Q1..Q4，而不是 easyScholar 返回的 "1区"
    ("const z = info.cas.replace('\u533a', '');",
     "const z = info.cas.replace('\u533a', ''); const q = /^[1-4]$/.test(z) ? 'Q' + z : info.cas;", 2),
    ("+ `${esc(info.cas)}${info.top ? ' Top' : ''}</span>`;",
     "+ `${esc(q)}${info.top ? ' Top' : ''}</span>`;", 2),
    # 导出的 Excel 也要跟界面一致用 Q1..Q4（原来直接写后端给的 "1区"）
    ("      cas: m.cas ? (m.cas + (m.top ? ' Top' : '')) : '',",
     "      cas: (() => { const z = (m.cas || '').replace('\u533a', '');\n"
     "        return z ? (/^[1-4]$/.test(z) ? 'Q' + z : m.cas) + (m.top ? ' Top' : '') : ''; })(),", 1),
    # 页面语言声明（英文界面不该声明成中文，影响字体回退与断行）
    ('<html lang="zh-CN">', '<html lang="en">', 1),
    # 导出按钮 tooltip：中文标题列已移除，不能再承诺
    ('title="Export results to Excel / CSV with translated titles, IF, CAS quartile and DOI">Export table</button>',
     'title="Export results to Excel / CSV with IF, CAS quartile and DOI">Export table</button>', 1),
    ("'Translated titles and journal metrics are cached locally — instant.'",
     "'Journal metrics are cached locally — instant.'", 1),
    # 关于面板：MyMemory 已不再使用
    ("      Title translations come from MyMemory; IF / CAS quartiles come from easyScholar (your own free key required).",
     "      IF / CAS quartiles come from easyScholar (your own free key required).", 1),
    # easyScholar 弹层：翻译说明整段去掉
    ('      Without it you can still search and download; the IF / quartile columns simply show "—".<br>\n'
     '      Translations come from the free MyMemory API, which has a daily character cap. Once it is used up nothing more is translated that day (it resumes the next day and previously translated items are cached).',
     '      Without it you can still search and download; the IF / quartile columns simply show "—".', 1),
    # Zotero 弹层里的 "library" 指 Zotero 库，与程序的 library/ 目录同名会误导
    ("Skipped ${ZOTDONE.skipped} already in the library", "Skipped ${ZOTDONE.skipped} already in Zotero", 1),
    ("(skipping ${ZOTPLAN.skipped} already in the library)", "(skipping ${ZOTPLAN.skipped} already in Zotero)", 1),
    ("Items already in the library are skipped by DOI", "Items already in Zotero are skipped by DOI", 1),
    ("${out.skipped} already in the library skipped", "${out.skipped} already in Zotero skipped", 1),
    # Zotero 按钮的 tooltip 同样是「Zotero 库」不是程序目录
    ("Local Zotero integration: see what is already in the library and generate an import file with collections",
     "Local Zotero integration: see what is already in Zotero and generate an import file with collections", 1),
    # 实际目录是小写 library
    ("Files are saved in the app folder's <code>Library/</code>.", "Files are saved in the app folder's <code>library/</code>.", 1),
    # 「精确短语」后面接一句说明，英文需要冒号才读得通
    ("<b>Exact phrase</b> use English double quotes:", "<b>Exact phrase:</b> use English double quotes:", 1),
    # MeSH 主题词标签：英文单词之间 3px 太挤，容易读成一串
    ("border-radius:3px;padding:0 5px;margin:0 3px 0 0}", "border-radius:3px;padding:0 5px;margin:0 4px 2px 0}", 1),
]

# 这些 token 原样保留（正则表达式、程序内用于解析的中文等）
KEEP_AS_IS = {
    "\u533a",                                   # 前端: info.cas.replace('区','') 的匹配字符
    "\u5426", "\u65e0",                          # easyScholar 的 top 字段返回中文「否 / 无」，必须原样匹配
    "\u6b21\u6570", "\u9650\u5236",                  # easyScholar 配额耗尽的提示是中文（"调用次数已达上限"）
    "([1-4])\\s*\u533a",                        # enrich.py: 从 "1区" 里取数字
    "(?<=[.!?;])\\s+|(?<=\u3002)\\s*",         # enrich.py: 中文分句用的正则
}
# 这些文件英文版有**有意的功能删减**（去掉中文翻译接口），AST 必然与中文版不同；
# 对它们改用「函数/类名集合必须一致」的弱校验，防止误删代码。
ALLOW_AST_DIFF = {"paperfetch_ui.py"}
SKIP_FILES = {"pack_release.py"}          # 打包脚本不随英文版发布，不翻译
# 这些变量存的是「中文词 -> 英文」查询表，key 必须保持中文，否则用户输入中文就查不到
PROTECT_VARS = {
    "ZH_TERMS",                            # 中文词 -> 英文查询表，key 必须是中文
    "CJK_STOP",                            # 中文停用字集合（set(str) 逐字符切分，翻了就失效）
    "_F_CAS", "_F_CAS_BASE", "_F_TOP",     # easyScholar 返回的字段名，含中文候选
    "FIELD_ALIAS",                         # 检索前缀别名，含中文别名
}


def protected_spans(text: str):
    """返回 [(起始行, 结束行)]：这些行内的字面量一律不动。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if names & PROTECT_VARS:
                spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans
REGEX_LIKE = re.compile(r"\(\?<|\\\\s|\\\\d|\[\^")


def needs_dict(s: str) -> bool:
    """含中文 → 必须查字典；只含全角标点 → 自动规范化；正则式 → 不动。"""
    return bool(CJK.search(s))


def auto_punct(s: str):
    """不含中文但含全角标点时，返回规范化结果；否则 None（不动）。"""
    if CJK.search(s) or REGEX_LIKE.search(s):
        return None
    n = normalise(s)
    return None if n == s else n


# 英文版默认值的改动（不是翻译，是产品决策）
PY_PATCHES = {
    "paperfetch.py": [
        ('"show_zh_title": True', '"show_zh_title": False'),
        # 生成的待下载清单页面也声明成英文
        ('<html lang=\\"zh-CN\\">', '<html lang=\\"en\\">'),
    ],
    "enrich.py": [],
    # 英文版不提供中文翻译：去掉 Excel 的「Chinese title」列，两个翻译接口直接返回 disabled
    "paperfetch_ui.py": [
        ('    ("Chinese title", "zh_title", 46),\n', ''),
        # 中文标题列删掉后，唯一标题列不必再标 (English)
        ('("Title (English)", "title", 62),', '("Title", "title", 62),'),
        ('''        elif self.path == "/api/zh":
            titles = [str(x) for x in (body.get("titles") or [])][:80]
            try:
                m, note = en.translate_titles(titles, CFG.get("email") or "", cache())
                self._json({"map": m, "note": note})
            except Exception as e:
                self._json({"map": {}, "note": f"{type(e).__name__}"}, 500)''',
         '''        elif self.path == "/api/zh":
            # English build: Chinese title translation is not offered.
            self._json({"map": {}, "note": "disabled"})'''),
        ('''        elif self.path == "/api/zh_abs":
            text = (body.get("text") or "")[:4000]
            try:
                zh, note = en.translate_abstract(text, CFG.get("email") or "", cache())
                self._json({"zh": zh, "note": note})
            except Exception as e:
                self._json({"zh": "", "note": f"{type(e).__name__}"}, 500)''',
         '''        elif self.path == "/api/zh_abs":
            # English build: abstract translation is not offered.
            self._json({"zh": "", "note": "disabled"})'''),
    ],
}

# 英文版要**整块删掉**的界面元素（不是翻译）：所有中译入口。
# 锚点是英文版生成后的文本；改了中文源码导致锚点失配时会打印 [warn]，不会静默失效。
EN_STRIP_UI = [
    ('''    <label class="fmt-sw" style="margin-top:9px">
      <input type="checkbox" id="jmZh" ${JM.zh ? 'checked' : ''}> Show a translated title under the original (X-MOL style)
    </label>
''', ""),
    ('''  $('#jmZh').onchange = async ev => {
    JM.zh = ev.target.checked;
    await jpost('/api/easyscholar', {action: 'toggle_zh', on: JM.zh});
    if(!JM.zh){
      document.querySelectorAll('.zh.on').forEach(el => { el.classList.remove('on'); });
    } else {
      zhRepaint('#tblBox', rows);   // Paste the translations we already have first, then fill in the missing ones
      if(rows.length) zhStart('#tblBox', rows);
    }
  };
''', ""),
    ('''       + `<div class="absbar"><button type="button" class="abstr">Translate</button>`
       + `<span class="abstip">Machine translation · uses your daily free MyMemory quota</span></div>`
''', ""),
    ("${zhCell(r)}", ""),
    ("  zhStart('#tblBox', rows, RENDER_SEQ);\n", ""),
    ("  zhStart('#listBox', list);\n", ""),
    ('''  if(!JM.zh) miss.push('Chinese title translation is off, so the Chinese title column will be empty');
  else if(nZh) miss.push(`${nZh} titles not translated yet`);
''', ""),
    ("  const nZh = list.filter(r => !ZH_MAP[r.title]).length;\n", ""),
    ("const willFetch = (JM.zh && nZh) || (JM.ok && nJm);", "const willFetch = (JM.ok && nJm);"),
    ("  try{ if(JM.zh) await zhStart('#tblBox', list); }catch(e){}", ""),
    ("Export table · with translated titles / IF / CAS quartile", "Export table · with IF / CAS quartile"),
]


UI_CSS = """
  /* ---- English UI tweaks (English strings are ~1.6x wider than Chinese) ---- */
  #btnSearch{flex:0 0 auto;min-width:96px}
  .yr{width:88px}
  #citePop{width:440px}
  #orgPop{width:480px}
  .acctpop{min-width:360px}
"""


# --------------------------------------------------------------------------- python
PAT_STR = re.compile(r'^([A-Za-z]*)("""|\'\'\'|"|\')([\s\S]*)\2$', re.S)
FSTART = getattr(tokenize, "FSTRING_START", -1)
FMID = getattr(tokenize, "FSTRING_MIDDLE", -2)
FEND_NAME = "FSTRING_END"


def rebuild_py(text: str, d: dict, missing: list):
    """返回新的源码文本。"""
    lines = text.split("\n")
    edits = []
    fq = None
    spans = protected_spans(text)

    def protected(ln):
        for a, b in spans:
            if a <= ln <= b:
                return True
        return False

    def push(r0, c0, r1, c1, new):
        edits.append(((r0, c0), (r1, c1), new))

    for t in tokenize.tokenize(io.BytesIO(text.encode("utf-8")).readline):
        if protected(t.start[0]):
            continue
        if t.type == FSTART:
            s0 = t.string
            fq = '"""' if s0.endswith('"""') else ("'''" if s0.endswith("'''") else s0[-1])
            continue
        if fq and tokenize.tok_name[t.type] == FEND_NAME:
            fq = None
            continue

        if t.type == tokenize.COMMENT:
            if t.string in KEEP_AS_IS:
                continue
            if not CJK_OR_WIDE.search(t.string):
                continue
            en = d["py_comments"].get(t.string)
            if en is None:
                auto = auto_punct(t.string)
                if auto is None:
                    if not CJK.search(t.string):
                        continue
                    missing.append(("py_comment", t.string))
                    continue
                en = auto
            push(t.start[0], t.start[1], t.end[0], t.end[1], normalise(en))
            continue

        if t.type == FMID:
            if t.string in KEEP_AS_IS:
                continue
            if not CJK_OR_WIDE.search(t.string):
                continue
            en = d["py_fstrings"].get(t.string)
            if en is None:
                auto = auto_punct(t.string)
                if auto is None:
                    if not CJK.search(t.string):
                        continue
                    missing.append(("py_fstring", t.string))
                    continue
                en = auto
            en = normalise(en)
            if fq == '"':
                en = re.sub(r'(?<!\\)"', r'\\"', en)
            elif fq == "'":
                en = re.sub(r"(?<!\\)'", r"\\'", en)
            push(t.start[0], t.start[1], t.end[0], t.end[1], en)
            continue

        if t.type != tokenize.STRING or not CJK_OR_WIDE.search(t.string):
            continue
        m = PAT_STR.match(t.string)
        if not m:
            missing.append(("py_string_badfmt", t.string))
            continue
        prefix, quote, body = m.group(1), m.group(2), m.group(3)
        if body in KEEP_AS_IS:
            continue
        en = d["py"].get(body)
        if en is None:
            auto = auto_punct(body)
            if auto is None:
                if not CJK.search(body):
                    continue          # 正则式 / 纯全角标点但无需改动
                missing.append(("py_string", body))
                continue
            en = auto
        en = normalise(en)
        if quote == "'":
            en = re.sub(r"(?<!\\)'", r"\\'", en)
        elif quote == '"':
            en = en.replace("\\'", "'")
            en = re.sub(r'(?<!\\)"', r'\\"', en)
        else:
            en = en.replace("\\'", "'").replace(quote, "\\" + quote[0] * 3)
        push(t.start[0], t.start[1], t.end[0], t.end[1], prefix + quote + en + quote)

    edits.sort(key=lambda e: e[0], reverse=True)
    for (r0, c0), (r1, c1), new in edits:
        i0, i1 = r0 - 1, r1 - 1
        if i0 == i1:
            lines[i0] = lines[i0][:c0] + new + lines[i0][c1:]
        else:
            lines[i0:i1 + 1] = [lines[i0][:c0] + new + lines[i1][c1:]]
    out = "\n".join(lines)
    # 「」→ 中文直角引号在英文里改用弯引号（不会与 f-string 定界符冲突）
    out = out.replace("\u300c", "\u201c").replace("\u300d", "\u201d")
    # 译文里的 \" 反斜杠+空格 是无效转义
    out = out.replace("library\\ ", "library ")
    return out


PY_PROTECT = ["(?<=\u3002)\\s*"]      # 正则里的中文句号，必须保留


def normalise_py_text(s: str) -> str:
    hold = []
    for i, k in enumerate(PY_PROTECT):
        tok = "@@KEEP%d@@" % i
        if k in s:
            s = s.replace(k, tok)
            hold.append((tok, k))
    s = normalise(s)
    for tok, k in hold:
        s = s.replace(tok, k)
    return s


# --------------------------------------------------------------------------- 校验
def skel(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ("Str",)
    if isinstance(node, ast.JoinedStr):
        return ("FStr", [skel(v) for v in node.values])
    if isinstance(node, ast.FormattedValue):
        return ("Fmt", skel(node.value), skel(node.format_spec) if node.format_spec else None)
    if isinstance(node, ast.Constant):
        return ("Const", type(node.value).__name__)
    if isinstance(node, ast.Name):
        return ("Name", node.id)
    if isinstance(node, ast.Attribute):
        return ("Attr", skel(node.value), node.attr)
    if isinstance(node, ast.alias):
        return ("alias", node.name, node.asname)
    if isinstance(node, ast.expr_context):
        return ("ctx", type(node).__name__)
    fields = []
    for f in node._fields:
        v = getattr(node, f, None)
        if isinstance(v, list):
            fields.append((f, [skel(x) if isinstance(x, ast.AST) else x for x in v]))
        elif isinstance(v, ast.AST):
            fields.append((f, skel(v)))
        else:
            fields.append((f, v))
    return (type(node).__name__, fields)


def def_names(tree) -> set:
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def count_residual(out_text: str) -> int:
    """统计「不该有中文的地方」的中文字符数。

    允许两类例外：
      * protected_spans() 覆盖的行（中文术语表等数据表）
      * KEEP_AS_IS 里的字面量（正则、easyScholar 返回值）
    """
    spans = protected_spans(out_text)
    n = 0
    keep = sorted(KEEP_AS_IS, key=len, reverse=True)
    for i, line in enumerate(out_text.split("\n"), 1):
        if any(a <= i <= b for a, b in spans):
            continue
        t = line
        for k in keep:
            t = t.replace(k, "")
        n += len(CJK.findall(t))
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    if not os.path.isdir(SRC):
        print("找不到源码目录：%s" % SRC)
        return 2
    d = load_dict()
    os.makedirs(DST, exist_ok=True)

    missing, results = [], []

    # ---- ui.html ----
    up = os.path.join(SRC, "ui.html")
    ui_src = io.open(up, encoding="utf-8").read()
    ui_en = rebuild_ui(ui_src, d, missing)
    for old, new, cnt in UI_PATCHES:
        got = ui_en.count(old)
        if got != cnt:
            print("[warn] ui patch anchor found %d times (expected %d): %r" % (got, cnt, old[:50]))
        ui_en = ui_en.replace(old, new)
    if "</style>" in ui_en:
        ui_en = ui_en.replace("</style>", UI_CSS + "</style>", 1)
    ui_en = ui_en.replace("'\u3000\u26a0 '", "' \u26a0 '")
    for a, b in EN_STRIP_UI:
        if a not in ui_en:
            print("[warn] ui.html: 删减锚点没找到 %r" % a[:60])
        ui_en = ui_en.replace(a, b)
    results.append(("ui.html", ui_src, ui_en, "ui"))

    # ---- python ----
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py") or fn in SKIP_FILES:
            continue
        p = os.path.join(SRC, fn)
        # newline="" 保留原始 CRLF —— tokenize 拿到的多行字符串里带着 \r\n，
        # 字典的 key 也是按原始字节提取的，两边必须一致。
        text = io.open(p, encoding="utf-8", newline="").read()
        out = rebuild_py(text, d, missing)
        out = normalise_py_text(out)
        crlf = "\r\n" in out
        for a, b in PY_PATCHES.get(fn, []):
            aa, bb = (a.replace("\n", "\r\n"), b.replace("\n", "\r\n")) if crlf else (a, b)
            if aa not in out:
                print("[warn] %s: 补丁锚点没找到 %r" % (fn, a[:40]))
            out = out.replace(aa, bb)
        results.append((fn, text, out, "py"))

    # ---- 未翻译 ----
    if missing:
        seen = set()
        print("\n[!] 有 %d 处中文没有对应译文：" % len(missing))
        for kind, s in missing:
            k = (kind, s)
            if k in seen:
                continue
            seen.add(k)
            print("  %-16s %r" % (kind, s[:90]))
        print("\n    把缺的条目补进 %s 后重跑。" % os.path.relpath(DICT, ROOT))
        return 1

    # ---- 写文件 / 校验 ----
    ok = True
    for fn, src, out, kind in results:
        if not args.check:
            io.open(os.path.join(DST, fn), "w", encoding="utf-8", newline="").write(out)
        if kind == "py":
            try:
                a, b = ast.parse(src), ast.parse(out)
            except SyntaxError as e:
                print("[FAIL] %s 生成后语法错误：%s" % (fn, e))
                ok = False
                continue
            if repr(skel(a)) != repr(skel(b)):
                if fn in ALLOW_AST_DIFF:
                    if def_names(a) != def_names(b):
                        print("[FAIL] %s 的函数/类集合变了：%s" %
                              (fn, sorted(def_names(a) ^ def_names(b))))
                        ok = False
                        file_ok = False
                        continue
                    print("      %s 结构与中文版不同（英文版去掉了中文翻译接口，属有意删减）" % fn)
                else:
                    print("[FAIL] %s 的 AST 与中文版不一致（说明替换动到了代码结构）" % fn)
                    ok = False
                    file_ok = False
                    continue
            tmp = os.path.join(DST, "." + fn + ".chk")
            io.open(tmp, "w", encoding="utf-8", newline="").write(out)
            try:
                py_compile.compile(tmp, doraise=True, cfile=tmp + "c")
            except Exception as e:
                print("[FAIL] %s 编译失败：%s" % (fn, e))
                ok = False
            finally:
                for t in (tmp, tmp + "c"):
                    if os.path.exists(t):
                        os.remove(t)
        file_ok = True
        residual = count_residual(out)
        if residual:
            print("[FAIL] %s 仍残留 %d 个中文字符（既不在字典里，也不在白名单/保护区内）" % (fn, residual))
            file_ok = False
            ok = False
        print("%-6s %-16s %7d -> %7d chars" % ("[ok]" if file_ok else "[bad]", fn, len(src), len(out)))

    if os.path.isdir(DST):
        for fn in ("app.ico",):
            src_ico = os.path.join(SRC, fn)
            if os.path.isfile(src_ico) and not args.check:
                import shutil
                shutil.copy2(src_ico, os.path.join(DST, fn))

    print("\n%s" % ("生成完成：源码/en/" if ok else "存在失败项，见上。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
