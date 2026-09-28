#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cite_check.py —— 交付物 ↔ 教材知识库 引用校验器

解决的问题
----------
我（模型）写的讲义/图表里，最危险的不是"没写全"，而是**写了教材根本没说的话**。
这类错误看起来都特别合理（"直径一般＞1cm"、"未累及基底膜"），人工逐条复核成本极高。

本工具把交付物里每一条「事实陈述」抽出来，回查知识库原文对应页，给出九档判定：

    原文       归一化后是教材该页的字面子串              → 通过
    重组       各信息元都在原文，只是排列不同            → 通过
    声明留白   交付物明说「教材未述」，找不到正是对的    → 通过
    归纳       声明为归纳，或位于说明区                  → 通过
    改写·接近  措辞略有出入，语义可能等价                → 需人工确认
    改写·部分  重合度偏低，可能是转述也可能是混写        → 需人工确认
    异地       教材里有，但不在标注的那一页              → 红线：修页码
    含未见片段 句子有原文作底，却夹了教材没有的话        → 红线：必须处理
    无出处     整句教材里找不到，且未声明为归纳          → 红线：必须处理

设计上的三个取舍
----------------
1. **页码归属用「行级」而非「格级」。** 交付物里只有皮损名称列标了 p.xx，
   但整行的成因/形态/定义/举例同属该页。所以按 row 取 pg，整行共用。
   子表格（斑疹分型、水疱分型）无自己的 pg，沿用最近前置 row 的 pg。

2. **只在字面层面判定，不做语义判断。** 本工具抓不到"语义没错但教材没写"的句子
   （例如凭常识补的限定语，只要用词常见，就可能在该页找到相似片段而判为「改写」）。
   这不是缺陷可以掩盖——报告末尾会明确写出来。

3. **区分「事实陈述」与「归纳性文字」。** 图里的层次描述、辨析、页脚说明本就允许
   是归纳。若不加区分，报告会被噪音淹没。但有个例外：**凡文本中自称"教材…"
   或标了 p.xx 的，无论什么位置，一律按严格标准校验** —— 声称了出处就必须对得上。

归一化只做"去空白、全角转半角、去装饰符"，不改任何字符，不折算同义词。

用法
----
    python cite_check.py 交付物.html                      # 自动定位 + 加载整库
    python cite_check.py 交付物.html --kb 知识库目录/原文   # 显式指定库
    python cite_check.py 交付物.html --chapter 04          # 只校验第 4 章
    python cite_check.py 交付物.html --json out.json

    # --kb 可省：脚本按便携包结构（tools/ 与 kb/ 同级）自动定位，解压到哪都能跑
    # --chapter 可省：留空则加载整库，交付物跨章时不必逐章切换
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import unicodedata
from html.parser import HTMLParser

# ---------------------------------------------------------------- 工具函数


def rel_or_abs(path: str, start: str) -> str:
    """报告里展示路径用：能算相对路径就算，跨盘符时退回绝对路径。

    交付物留在工程目录（C:）而知识库放在固定落点（D:）是常态，
    此时 os.path.relpath 在 Windows 上直接抛 ValueError，
    会让整个报告生成失败 —— 必须兜住。（2026-09-20 实测踩到）
    """
    try:
        return os.path.relpath(path, start)
    except ValueError:
        return os.path.abspath(path)


# ---------------------------------------------------------------- 常量

# 归一化时删除的字符：空白 + 各类装饰性分隔符 + 需要忽略的标点差异
DROP_CHARS = " \t\r\n\u3000\u200b"

# 判定阈值
COVER_STRONG = 0.85  # 覆盖率 ≥ 此值判「改写（接近原文）」
COVER_WEAK = 0.55    # 覆盖率 ≥ 此值判「改写（部分）」，低于则「无出处」

# 子句切分点（保留逗号、顿号 —— 它们是句子内部的正常成分）
SPLIT_RE = re.compile(r"[；。\n]|(?<=<br>)|<br\s*/?>")

# 「声称有出处」的标记：命中则强制走严格校验。
# 注意只认**肯定式**强声称。「教材所举疾病」「教材所述成因」这类是**栏目名**，
# 是在标注内容来源，不是在声称某句话出自教材；「教材未述」是在说明教材没有，
# 一并强制会把所有栏目名、留白声明和归纳说明都误报成无出处。
CLAIM_RE = re.compile(
    r"p\.\s*\d+"
    r"|教材(原文|明言|明确指出|明确说明)"
)

# 元信息区块：页头/页脚/节标题/分组标题/表头/计数。
# 这些位置的文字是**关于图的说明**，不是教材内容的陈述，
# 即便含「p.21–26」这类范围标注或「教材」字样，也不该按事实陈述去校验。
META_KINDS = {"foot", "hero", "schead", "ghead", "tablehd", "cnt", "subhead"}

# 这些位置承载具体内容，值得标上页码以便溯源。
# 页头/页脚/分组标题/辨析区不在其列 —— 要求「原发性皮损」这个标题标个页码没有意义。
PAGEABLE_KINDS = {"grid", "subgrid", "sxval", "ov", "name", "en", "note", "na"}

# 明确声明「教材没有此内容」的写法 —— 这类**找不到才是对的**
BLANK_RE = re.compile(r"^教材未述|^教材无|^未见教材")

# 图内导航标记：互标对照、引导读者看别处。它们服务于版面，不承载教材内容。
NAV_RE = re.compile(r"对照|参见|见表|见下|见上|同上")

# 「信息元重排」判定：把子句拆成信息元，若每个都能在原文找到，只是排列不同，
# 视为等价（如「花斑糠疹（糠秕状）、银屑病（蛎壳状）」对「可呈糠秕状（如花斑糠疹）…」）。
# `/` 也在切分之列 —— 它表示「换个叫法」，两侧本就该各自独立成立。
META_SPLIT_RE = re.compile(r"[、,，;；:：/／()（）\[\]【】\s]+")

# 内联页码：写在格子文字里的「（p.22）」，表示这段另有出处
INLINE_PG_RE = re.compile(r"p\.\s*(\d+)\s*(?:[\u2013\u2014\-\u2212]\s*(\d+))?")


def min_hit_for(n_len: int) -> int:
    """最短命中长度随子句长度自适应。

    长句要求 8 字连续命中才可信；表格格里常见的短标签（如「压之变白」）只有 4 字，
    若一律要求 8 字会把正确的重排写法全判成无出处。
    """
    if n_len <= 6:
        return 3
    if n_len <= 12:
        return 4
    if n_len <= 24:
        return 6
    return 8


# 长句内「夹带」片段的告警下限（字）。低于此长度的零碎差异视为虚词/标点，不报。
#
# 这个参数解决的是本工具最凶险的一类漏报：**真话里夹假话**。
# 例如「为实质性、深在性、可触诊的皮损，直径一般大于 1cm」——
# 前半句是教材原文（14 字连续命中），整体覆盖率被拉到 72%，若只看覆盖率
# 就会判成"改写·接近"而放过；可「直径一般大于 1cm」是教材根本没有的限定。
UNCOVER_MIN = 6



# ---------------------------------------------------------------- 归一化


def norm(s: str) -> str:
    """规范化：全角转半角、去空白、去装饰符。

    只动"形"，不动"实" —— 不折算同义词、不做繁简转换，
    因为我们校验的是"教材是否这样说过"，措辞本身就是被检验对象。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for ch in DROP_CHARS:
        s = s.replace(ch, "")
    # 剔除内联引证页码（「（p.21）」「(p.24–26)」）—— 它是标注，不是内容
    s = re.sub(r"[（(]p\.\s*\d+(?:\s*[\u2013\u2014\-\u2212]\s*\d+)?[)）]", "", s)
    # 中文引号/括号统一（教材与交付物可能不同）
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2018", "'").replace("\u2019", "'")
    # 去掉纯装饰符
    s = re.sub(r"[·•→←↔⇒⇐]|—{2,}", "", s)
    return s


def parse_pages(label: str):
    """'p.24–25' -> [24, 25]"""
    if not label:
        return []
    m = re.match(r"\s*p\.\s*(\d+)\s*(?:[\u2013\u2014\-\u2212]\s*(\d+))?", label)
    if not m:
        return []
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return list(range(a, b + 1))


# ---------------------------------------------------------------- 知识库


def load_kb(paths: str | list[str]):
    """按 <!-- pdf=N book=M --> 锚点把原文切成 {书内页码: 正文}

    `paths` 可以是单个 .md，也可以是多个 .md —— 多章合并进同一份页码表，
    这样交付物跨章时不必逐章切换校验，也不会把「别章的内容」误判成「无出处」。
    """
    if isinstance(paths, str):
        paths = [paths]
    pages: dict[int, str] = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        parts = re.split(r"<!--\s*pdf=(\d+)\s+book=(\d+|none)\s*-->", raw)
        for i in range(1, len(parts), 3):
            book, body = parts[i + 1], parts[i + 2]
            if book == "none":
                continue
            bp = int(book)
            pages[bp] = pages.get(bp, "") + "\n" + body
    return {k: v for k, v in pages.items() if v.strip()}


# ---------------------------------------------------------------- HTML → DOM

VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "source", "area"}


class El:
    __slots__ = ("tag", "cls", "attrs", "kids", "parent", "line")

    def __init__(self, tag, cls, attrs, parent, line):
        self.tag = tag
        self.cls = cls
        self.attrs = attrs
        self.kids = []          # El 或 str（文本节点）
        self.parent = parent
        self.line = line


class Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("root", set(), {}, None, 0)
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = set((d.get("class") or "").split())
        el = El(tag, cls, d, self.stack[-1], self.getpos()[0])
        self.stack[-1].kids.append(el)
        if tag not in VOID_TAGS:
            self.stack.append(el)
        else:
            el.kids.append("\u00a0" if tag == "br" else "")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].kids.append(data)


def text_of(el: El) -> str:
    """递归取元素纯文本；<br> 折算为换行符，便于子句切分"""
    buf = []
    for k in el.kids:
        if isinstance(k, str):
            buf.append(k)
        elif k.tag == "br":
            buf.append("\n")
        else:
            buf.append(text_of(k))
    return "".join(buf)


def has_cls(el: El, *names) -> bool:
    return bool(el.cls & set(names))


def descendants(el: El):
    for k in el.kids:
        if isinstance(k, El):
            yield k
            yield from descendants(k)


def direct_text_children(el: El):
    """直接子元素中的「文本承载元素」：div / span"""
    return [k for k in el.kids if isinstance(k, El) and k.tag in ("div", "span", "li", "em")]


def find_pg(el: El):
    """从后代中找 span.pg，返回其文本（找不到返回 None）"""
    for d in descendants(el):
        if d.tag == "span" and "pg" in d.cls:
            return text_of(d).strip()
    return None


def find_page_hint(el: El, max_up: int = 3):
    """找块的页码归属：先看块内 span.pg，再向上若干层找 span.pg 或文案里的 p.N。

    有些区块把页码写在标题里（如「症状总述 教材 p.21」）而不是 span.pg，
    若不认，这些格子就会被当成"未标页码"。
    向上查找层数刻意限制在 3 层 —— 再往上就是整张表，会抓到无关条目的页码。
    """
    pg = find_pg(el)
    if pg:
        return pg
    node = el
    for _ in range(max_up):
        node = node.parent
        if node is None:
            return None
        pg = find_pg(node)
        if pg:
            return pg
        m = INLINE_PG_RE.search(text_of(node))
        if m:
            return "p." + m.group(1)
    return None


def text_without_pg(el: El) -> str:
    """取元素文本，但剔除 span.pg 里的页码。

    名称列形如 `<div class=name><b>风团</b><span>wheal</span><span class=pg>p.22</span></div>`，
    若整块取文本会变成「风团whealp.22」—— 页码混进正文，必然判无出处。
    """
    return split_special(el, {"pg"})[0]


def split_special(el: El, special: set):
    """把元素的文本拆成 (主体, {特殊class: [文本…]})。

    为什么要拆：格子正文与内嵌注解（`span.note`）来源不同 —— 正文宣称出自教材，
    注解往往是「教材未设直径限定」这类我的判断。混在一起校验，两者会互相污染：
    正文因为多了注解而不匹配，注解因为混了正文而被当成正文。
    """
    main, found = [], {}

    def rec(e):
        for k in e.kids:
            if isinstance(k, str):
                main.append(k)
            elif k.tag == "br":
                main.append("\n")
            elif k.cls & special:
                for c in sorted(k.cls & special):
                    found.setdefault(c, []).append(text_of(k))
            else:
                rec(k)

    rec(el)
    return "".join(main), found


def extract_latin_names(el: El):
    """从名称列里抽出教材给的英文名（形如 `macule, patch`）。

    这是历史踩过的坑：教材只给「症状（symptom）」一个英文名，
    瘙痒/疼痛/感觉异常都没标。我当年自作主张补了 pruritus/pain/paresthesia。
    所以英文名必须独立校验 —— 教材该条目没标就是不标。
    """
    out = []
    for d in descendants(el):
        if d.tag != "span" or "pg" in d.cls:
            continue
        t = text_of(d).strip()
        if t and re.fullmatch(r"[A-Za-z][A-Za-z,\s\-()]{2,60}", t):
            out.append(t)
    return out


def text_cn_only(el: El) -> str:
    """名称列的**中文名**部分：剔除页码与英文名。

    名称列是「斑疹、斑片」+「macule, patch」+「p.21」的拼接体，
    整串拿去比对必然不中（教材原文写作「1.斑疹（macule）和斑片（patch）」）。
    中文名与英文名各校验各的，才判得准。
    """
    buf = []

    def rec(e):
        for k in e.kids:
            if isinstance(k, str):
                buf.append(k)
            elif k.tag == "br":
                buf.append("\n")
            elif k.cls & {"pg"}:
                continue
            elif re.fullmatch(r"[A-Za-z][A-Za-z,\s\-()]{2,60}", text_of(k).strip() or ""):
                continue
            else:
                rec(k)

    rec(el)
    return "".join(buf).strip()



# ---------------------------------------------------------------- 单元抽取

# 单元：一条可独立校验的事实陈述
class Unit:
    __slots__ = ("kind", "page_label", "text", "line", "soft", "note", "inline_pages")

    def __init__(self, kind, page_label, text, line, soft=False, note=""):
        self.kind = kind               # grid / subgrid / sxval / ov / disc / layer ...
        self.page_label = page_label   # 标注的 p.xx（可能为 None）
        self.text = text
        self.line = line               # 源 HTML 行号，便于定位
        self.soft = soft               # True = 归纳性文字，无出处不算错
        self.note = note
        # 写在格子文字里的内联页码，如「…称丘脓疱疹（p.22）」。
        # 这类文字游走在页边界（图注、跨页续写），必须把它自己声明的页也算进去，
        # 否则会被判成"页码标错" —— 而它其实标得很准。
        self.inline_pages = []
        for m in INLINE_PG_RE.finditer(text):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            self.inline_pages.extend(range(a, b + 1))

    @property
    def pages(self):
        return parse_pages(self.page_label or "")

    @property
    def scope_pages(self):
        return sorted(set(self.pages) | set(self.inline_pages))


# 各类块的抽取规则：kind -> (是否软性, 说明)
KIND_INFO = {
    "grid":    (False, "主表格正文格"),
    "name":    (False, "皮损/症状名称列"),
    "en":      (False, "英文名（教材该条目是否标了英文）"),
    "note":    (True,  "内嵌注解（多为我的判断，非教材原话）"),
    "na":      (True,  "「教材未述」留白标记"),
    "subgrid": (False, "子表格格（分型表）"),
    "sxhead":  (False, "症状卡标题"),
    "sxval":   (False, "症状卡内容"),
    "ov":      (False, "症状总览项"),
    "gnote":   (True,  "分组说明"),
    "ghead":   (True,  "分组标题/描述"),
    "schead":  (True,  "节标题/描述"),
    "hero":    (True,  "页头"),
    "layer":   (True,  "层次带描述"),
    "subhead": (True,  "子表标题"),
    "tablehd": (True,  "表头（自拟栏目名，非教材陈述）"),
    "disc":    (True,  "辨析条目"),
    "foot":    (True,  "页脚说明"),
    "cnt":     (True,  "计数标签"),
}


def extract_units(html_text: str):
    b = Builder()
    b.feed(html_text)
    units: list[Unit] = []
    state = {"last_page": None}

    def add(kind, page, el, note="", text_override=None):
        txt = text_override if text_override is not None else text_without_pg(el).strip()
        txt = txt.strip()
        if not txt:
            return
        soft = KIND_INFO.get(kind, (True, ""))[0]
        # 元信息区块（页头页脚标题等）恒为软性：它们说的是"这张图怎么用"，
        # 不是"教材说了什么"，即便出现「教材」二字也不该按事实陈述校验。
        if kind in META_KINDS:
            soft = True
        elif CLAIM_RE.search(txt):
            soft = False
        units.append(Unit(kind, page, txt, el.line, soft, note))

    def add_split(kind, page, el):
        """正文与内嵌的 note / 留白标记，拆成各自独立的单元分别校验"""
        main, found = split_special(el, {"pg", "note", "na"})
        add(kind, page, el, text_override=main)
        for t in found.get("note", []):
            add("note", page, el, text_override=t)
        for t in found.get("na", []):
            add("na", page, el, text_override=t)

    def walk(el: El):
        cls = el.cls

        # ---- 主表格行 ----
        if "row" in cls:
            pg = find_pg(el)
            if pg:
                state["last_page"] = pg
            for k in direct_text_children(el):
                if "cell" in k.cls:
                    add_split("grid", state["last_page"], k)
                elif "name" in k.cls:
                    add("name", state["last_page"], k, text_override=text_cn_only(k))
                    for en in extract_latin_names(k):
                        add("en", state["last_page"], k, text_override=en)
            return

        # ---- 子表格（分型表）----
        if "sub" in cls:
            for d in descendants(el):
                if "sub-grid" in d.cls:
                    for cell in direct_text_children(d):
                        c = cell.cls
                        if "sh" in c:
                            add("tablehd", state["last_page"], cell)
                        else:
                            add("subgrid", state["last_page"], cell)
                    break
            for d in descendants(el):
                if "sub-t" in d.cls:
                    add("subhead", state["last_page"], d)
            return

        # ---- 症状卡 ----
        if "sx" in cls and "sx-wrap" not in cls:
            pg = find_pg(el)
            if pg:
                state["last_page"] = pg
            for d in descendants(el):
                if "sxh" in d.cls:
                    add("sxhead", state["last_page"], d)
                elif "v" in d.cls:
                    add("sxval", state["last_page"], d)
            return

        # ---- 症状总览 ----
        # 它的页码写在卡片标题里（「症状总述 教材 p.21」），且位于全文最前，
        # 没有"最近前置行"可用，所以要向上找。
        if "ov-grid" in cls:
            pg = state["last_page"] or find_page_hint(el, 3)
            for k in direct_text_children(el):
                for s in direct_text_children(k):
                    if s.tag == "span":
                        add("ov", pg, s)
            return

        # ---- 辨析 ----
        # 辨析区是跨条目的汇总，其内容来自多页，沿用"最近一行"的页码必然出错。
        # 故 page 置空，让它在全章里检索。
        if "disc-grid" in cls:
            for k in direct_text_children(el):
                for s in direct_text_children(k):
                    if s.tag == "span":
                        add("disc", None, s)
            return

        # ---- 页脚 ----
        if "foot" in cls and el.tag == "div":
            for d in descendants(el):
                if d.tag == "li":
                    add("foot", None, d)
                elif "end" in d.cls:
                    add("foot", None, d)
            return

        # ---- 分组说明 / 分组头 ----
        if "gnote" in cls:
            add("gnote", None, el)
            return
        if "ghead" in cls or "schead" in cls:
            for d in descendants(el):
                if d.tag == "span":
                    add("ghead" if "ghead" in cls else "schead", None, d)
            return

        # ---- 层次带 ----
        if "layer" in cls:
            for k in direct_text_children(el):
                if "d" in k.cls:
                    add("layer", state["last_page"], k)
                elif "cnt2" in k.cls:
                    add("cnt", None, k)
            return

        for k in el.kids:
            if isinstance(k, El):
                walk(k)

    walk(b.root)
    return units


def guard_units(units, total_clauses=None):
    """零单元 / 零子句守卫 —— 抽不到东西时必须停下，不能输出「通过」。

    「查过都没问题」和「根本没查」是两件事，在报告上不能长得一样。
    校验器按 class 抽单元（不是通用 HTML 文本提取器）：交付物若是普通
    HTML（<h1>/<p>/<div> 不带约定 class），会抽出 0 个单元。此时若照常出报告，
    结论会是「未检出教材未见的内容」、退出码仍是 0 —— 一个会被信以为真的假绿。
    （2026-09-20 实测：同一句编造，写进普通 HTML 报绿，写进约定结构判「无出处」。）

    `total_clauses` 传 None 表示"这一步还没统计"，只查单元数。
    """
    if not units:
        raise SystemExit(
            "未能从交付物中抽取任何内容单元 —— HTML 结构不符合校验器约定。\n"
            "  校验器只识别带特定 class 的结构：\n"
            '    <div class="row"> 下面放 <div class="cell">（正文格）'
            '或 <div class="name">（名称列）\n'
            '    页码写成 <span class="pg">p.21</span>\n'
            "  普通 HTML（不带这些 class）会被抽成 0 单元。\n"
            "  结构约定与最小示例见包内 README 的「交付物结构约定」一节。")
    if total_clauses is not None and total_clauses == 0:
        raise SystemExit(
            f"抽到 {len(units)} 个单元，却没有一条可校验的子句（都短于 4 字）。\n"
            "  多半是单元里只有标题、栏目名这类极短文本 —— 确认正文格里确实写了内容。")


# ---------------------------------------------------------------- 校验


def matching_ratio(a: str, b: str):
    """a 有多少比例的字符能按顺序在 b 中找到（用最长匹配合并计算）"""
    if not a:
        return 0.0, 0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = 0
    longest = 0
    for blk in sm.get_matching_blocks():
        matched += blk.size
        longest = max(longest, blk.size)
    return matched / len(a), longest


def unmatched_runs(a: str, b: str, win_min: int = 4, max_win: int = 44):
    """把 a 切成「有原文支撑 / 无支撑」两种片段，返回长度 ≥ UNCOVER_MIN 的无支撑段。

    这里刻意**不用** difflib 的匹配块。difflib 允许字符在 b 里任意位置、乱序匹配，
    于是假话里的单字只要在页面别处出现过，就被算作"已覆盖" ——
    整段假话会被切碎成若干短空隙，每一段都不足告警下限，从而整体逃过检查。
    实测：`实质性、深在性、可触诊的皮损，直径一般大于1cm，触之较硬` 就是这样漏掉的。
    改用滑动窗口做**最长连续命中**：只有连续出现在原文里的片段才算有支撑。
    """
    if not b:
        return []
    n = len(a)
    covered = bytearray(n)
    i = 0
    while i < n:
        best = 0
        for L in range(min(max_win, n - i), win_min - 1, -1):
            if a[i:i + L] in b:
                best = L
                break
        if best:
            covered[i:i + best] = b"\x01" * best
            i += best
        else:
            i += 1
    segs, start = [], None
    for idx in range(n):
        if not covered[idx]:
            if start is None:
                start = idx
        elif start is not None:
            if idx - start >= UNCOVER_MIN:
                segs.append(a[start:idx])
            start = None
    if start is not None and n - start >= UNCOVER_MIN:
        segs.append(a[start:])
    return segs


def ctx_of(hay: str, needle: str, span: int = 34) -> str:
    """在 hay 中定位 needle 的上下文片段（原文里是硬换行，这里压平展示）"""
    i = hay.find(needle)
    if i < 0:
        sm = difflib.SequenceMatcher(None, needle, hay, autojunk=False)
        m = sm.find_longest_match(0, len(needle), 0, len(hay))
        if m.size < 4:
            return ""
        i = m.b
        frag = hay[max(0, i - span): i + m.size + span]
    else:
        frag = hay[max(0, i - span): i + len(needle) + span]
    return re.sub(r"\s+", "", frag)


def split_clauses(text: str):
    """把单元文本切成子句。保留逗号 —— 逗号两侧是同一句话的组成，拆开反而失真。"""
    t = re.sub(r"<br\s*/?>", "\n", text)
    raw = re.split(r"[；。\n]+", t)
    out = []
    for c in raw:
        c = c.strip(" \u3000·•-—")
        if not c:
            continue
        # 过短的片段（如"等""见表下"）不单独校验，噪音太大
        if len(norm(c)) < 4:
            continue
        out.append(c)
    return out


# 「引导语：」前缀的两种写法：冒号式 与 破折号式。
# 交付物里常用「教材明言：「……」」「症状与体征的分界（教材原文）：……」引出引用。
# 引导语是作者加的，不属于被引内容，拿去比对必然在教材里找不到 —— 必须先剥掉。
LEAD_PATTERNS = (
    re.compile(r'^[^，。；、]{1,18}[：:]["\u201c]?'),
    re.compile(r'^[^，。；、"\u201c]{1,18}(?:——|—)["\u201c]?'),
)


def strip_lead(clause: str) -> str:
    """剥掉引述引导语，只留被引用的内容。剥不动就原样返回。"""
    for pat in LEAD_PATTERNS:
        m = pat.match(clause)
        if m and len(clause) - m.end() >= 8:
            return clause[m.end():].strip(' "\u201c\u201d\u2018\u2019\'')
    return clause


def meta_pieces(clause: str):
    """拆信息元，用于「重排」判定。返回长度 ≥2 的片段。"""
    out = []
    for p in META_SPLIT_RE.split(clause):
        p = p.strip(" \u3000·•-—")
        if len(norm(p)) >= 2:
            out.append(p)
    return out


def check(units, pages):
    """返回 (rows, stats)。rows 为逐子句的判定记录。

    判定优先级刻意这样排：
      声明留白 → 标注页精确命中 → 标注页信息元重排 → 全库精确命中（判异地）
      → 信息元重排 → 近似改写 → 归纳 → 无出处
    前几级都是"能证明有原文支撑"，越靠前证据越硬。
    「标注页」两级排在全库之前：语料若是整库，别章的巧合字面串不该抢走
    本页已有支撑的内容。只有本页完全支撑不了，才去全库找并判「异地」。
    """
    clean = {p: norm(t) for p, t in pages.items()}
    rows = []
    total_clauses = 0

    for u in units:
        clauses = split_clauses(u.text)
        if not clauses:
            continue
        upages = u.scope_pages
        labeled = bool(upages)
        scope = upages if upages else sorted(clean.keys())
        for cl in clauses:
            # 校验的是「被引用的内容」，引导语（「教材明言：」之类）先剥掉
            n = norm(strip_lead(cl))
            if len(n) < 4:
                n = norm(cl)
            if len(n) < 4:
                continue
            total_clauses += 1
            need = min_hit_for(len(n))
            rec = {
                "line": u.line, "kind": u.kind, "soft": u.soft,
                "page_label": u.page_label or "", "clause": cl,
                "verdict": "", "found_page": None, "flag": "",
                "cover": 0.0, "longest": 0, "evidence": "", "uncovered": [],
                "need": need,
            }

            # ---- 0) 声明「教材没有」----
            if BLANK_RE.search(cl.strip()):
                rec["verdict"] = "声明留白"
                rows.append(rec)
                continue

            # ---- 0.5) 图内导航标记（如「↔ 与溃疡对照」）----
            if len(n) <= 12 and NAV_RE.search(cl):
                rec["verdict"] = "归纳"
                rec["flag"] = "导航标记"
                rows.append(rec)
                continue

            # ---- 1) 精确命中标注页 ----
            hit = next((p for p in upages if p in clean and n in clean[p]), None)
            if hit is not None:
                rec.update(verdict="原文", found_page=hit, cover=1.0,
                           longest=len(n), evidence=ctx_of(clean[hit], n))
                rows.append(rec)
                continue

            pieces = meta_pieces(cl)

            # ---- 1.5) 标注页内的信息元重排 ----
            # 必须排在「全库精确命中」之前。语料是整库时，标注页上一句被约定性
            # 改写过的内容（如按书写约定把教材的「和」写成顿号），可能恰好在别章
            # 撞上字面相同的串；若不先在本页做重排，就会被误判成「页码错」。
            # 本页能重排成功，说明内容确实出自本页，不该再往别处找。
            if labeled and len(pieces) >= 2:
                for p in upages:
                    if p not in clean:
                        continue
                    if all(norm(pc) in clean[p] for pc in pieces):
                        rec.update(verdict="重组", found_page=p,
                                   cover=len("".join(pieces)) / len(n),
                                   longest=max(len(norm(x)) for x in pieces),
                                   evidence=ctx_of(clean[p], norm(pieces[0])))
                        break
                if rec["verdict"]:
                    rows.append(rec)
                    continue

            # ---- 2) 全库精确命中：区分三种情况 ----
            found = next((p for p, t in clean.items() if n in t), None)
            if found is not None:
                rec.update(found_page=found, cover=1.0, longest=len(n),
                           evidence=ctx_of(clean[found], n))
                if not labeled:
                    # 这个位置没标页码 —— 不是错误，但值得补上
                    rec["verdict"] = "原文"
                    rec["flag"] = "未标页" if u.kind in PAGEABLE_KINDS else ""
                elif u.soft:
                    # 元信息/归纳区的文字恰好与原文重合，属正常
                    rec["verdict"] = "原文"
                    rec["flag"] = "旁注"
                else:
                    # 标了页，但教材不在那一页 —— 真页码错
                    rec["verdict"] = "异地"
                rows.append(rec)
                continue

            # ---- 3) 信息元重排：逐段都能在同一页找到，只是排列方式不同 ----
            if len(pieces) >= 2:
                for p in scope:
                    if p not in clean:
                        continue
                    if all(norm(pc) in clean[p] for pc in pieces):
                        rec.update(verdict="重组", found_page=p,
                                   cover=len("".join(pieces)) / len(n),
                                   longest=max(len(norm(x)) for x in pieces),
                                   evidence=ctx_of(clean[p], norm(pieces[0])))
                        break
                if rec["verdict"]:
                    rows.append(rec)
                    continue

            # ---- 4) 近似匹配（标注页优先，再扫全库取更优者）----
            cov, lg, p, ev = 0.0, 0, None, ""
            for q in scope:
                if q not in clean:
                    continue
                c2, l2 = matching_ratio(n, clean[q])
                if (c2, l2) > (cov, lg):
                    cov, lg, p = c2, l2, q
            for q, txt in clean.items():
                if q == p:
                    continue
                c2, l2 = matching_ratio(n, txt)
                if (c2, l2) > (cov, lg):
                    cov, lg, p = c2, l2, q
            if p is not None:
                ev = ctx_of(clean[p], n)
            unc = unmatched_runs(n, clean[p]) if p is not None else []

            rec.update(cover=round(cov, 3), longest=lg, found_page=p,
                       evidence=ev, uncovered=unc)
            # 归纳性文字（层次描述、辨析、页脚说明、表头）本就允许非原文表述，
            # 走到这一步只说明措辞不同，不该计为问题。
            # 注意：文本里自称「教材明言/教材原文」的，在 add() 里已被转成硬性，
            # 不会从这里漏过去。
            if u.soft:
                rec["verdict"] = "归纳"
            # 再看有没有"夹带的未见片段"，最后才看整体相似度。
            # 顺序至关重要：夹带检测必须压过覆盖率，否则真话越长的句子越容易蒙混过关。
            elif lg >= need and cov >= COVER_WEAK:
                if unc:
                    rec["verdict"] = "含未见片段"
                elif cov >= COVER_STRONG:
                    rec["verdict"] = "改写·接近"
                else:
                    rec["verdict"] = "改写·部分"
            else:
                rec["verdict"] = "无出处"
            rows.append(rec)

    order = ["无出处", "含未见片段", "异地", "改写·部分", "改写·接近",
             "重组", "原文", "声明留白", "归纳"]
    stats = {k: 0 for k in order}
    for r in rows:
        stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
    stats["_子句总数"] = total_clauses
    return rows, stats


# ---------------------------------------------------------------- 报告

VERDICT_ORDER = ["无出处", "含未见片段", "异地", "改写·部分", "改写·接近",
                 "重组", "原文", "声明留白", "归纳"]
VERDICT_LABEL = {
    "原文": "✓ 原文（字面命中教材）",
    "重组": "◇ 重组（信息元均在原文，仅排列不同）",
    "声明留白": "◇ 声明留白（教材确无此项）",
    "异地": "⚠ 异地（教材有，但页码不符）",
    "改写·接近": "⚠ 改写·接近（措辞略异，语义可能等价）",
    "改写·部分": "⚠ 改写·部分（重合度偏低）",
    "含未见片段": "✗ 含未见片段（真话里夹了教材没有的话）",
    "无出处": "✗ 无出处（教材未见，且未声明为归纳）",
    "归纳": "◇ 归纳（允许非原文）",
}
VERDICT_DESC = {
    "原文": "字面命中教材该页，可放心引用",
    "重组": "各信息元都能在原文找到，只是排列方式不同",
    "声明留白": "交付物明说「教材未述」，找不到正是对的",
    "异地": "教材确实这么说，但页码标错了",
    "改写·接近": "大部分重合，需确认改写是否可接受",
    "改写·部分": "重合度低，可能是转述或归纳混写",
    "含未见片段": "**句中有连续片段在教材找不到，须逐条核对**",
    "无出处": "**整句教材未见，必须处理**",
    "归纳": "声明为归纳（或位于说明区），无出处属预期",
}


def build_report(units, rows, stats, args):
    L = []
    A = L.append
    A("# 引用校验报告 · 交付物 vs 教材原文")
    A("")
    A(f"- **交付物**：`{os.path.basename(args.html)}`")
    A(f"- **事实来源**：`{rel_or_abs(args.kb, os.path.dirname(args.html))}`")
    A(f"- **单元数**：{len(units)}　｜　**已校验子句数**：{stats.get('_子句总数', 0)}")
    A("")
    A("## 一、总览")
    A("")
    A("| 判定 | 子句数 | 占比 | 说明 |")
    A("|---|---:|---:|---|")
    total = max(stats.get("_子句总数", 0), 1)
    for v in VERDICT_ORDER:
        c = stats.get(v, 0)
        if not c:
            continue
        A(f"| {VERDICT_LABEL[v]} | {c} | {c / total * 100:.0f}% | {VERDICT_DESC[v]} |")
    A("")
    red = stats.get("无出处", 0) + stats.get("含未见片段", 0)
    if red:
        A(f"> **结论：{red} 条子句存在教材未见的内容 —— 这些是需要修改的。**")
        A(">")
        A(f"> 其中「无出处」{stats.get('无出处', 0)} 条（整句找不到）、"
          f"「含未见片段」{stats.get('含未见片段', 0)} 条（句子有原文做底、但夹带了教材没有的话）。")
    else:
        A("> **结论：没有发现「教材未见」的内容。**")
    A("")

    # 未标页的位置：不是错误，但补上页码能提升可追溯性
    unlabeled = [r for r in rows if r.get("flag") == "未标页"]
    if unlabeled:
        bypage = {}
        for r in unlabeled:
            bypage.setdefault(r["found_page"], []).append(r)
        A(f"### 建议补标页码的位置（{len(unlabeled)} 条，非错误）")
        A("")
        A("这些格子在图中没有标页码，但教材里确实有对应原文 —— 补上即可完整溯源。")
        A("")
        A("| 教材页 | HTML 行 | 内容示例 |")
        A("|---|---|---|")
        for p in sorted(bypage):
            rs = bypage[p]
            lines = "、".join(f"L{r['line']}" for r in rs[:6]) + ("…" if len(rs) > 6 else "")
            A(f"| p.{p} | {lines} | {rs[0]['clause'][:36]} |")
        A("")

    # 按严重度分组明细
    COLLAPSE = {"原文", "重组", "声明留白", "归纳"}
    for v in VERDICT_ORDER:
        items = [r for r in rows if r["verdict"] == v]
        if not items:
            continue
        if v in COLLAPSE and not args.verbose:
            A(f"## {VERDICT_LABEL[v]} —— 共 {len(items)} 条（加 `--verbose` 展开）")
            A("")
            continue
        A(f"## {VERDICT_LABEL[v]} —— 共 {len(items)} 条")
        A("")
        for r in items:
            A(f"**HTML 第 {r['line']} 行** ｜ 归类 `{r['kind']}` ｜ 标注页 `{r['page_label'] or '（未标）'}`")
            A("")
            A(f"> {r['clause']}")
            A("")
            if v == "含未见片段":
                A(f"- 覆盖率 {r['cover']:.0%}，最长重合 {r['longest']} 字")
                for seg in r.get("uncovered", []):
                    A(f"- ⚠ **句中教材未见的部分**：`{seg}`")
                if r["evidence"]:
                    A(f"- 教材相邻原文：`{r['evidence']}`")
            elif v == "无出处":
                A(f"- 覆盖率 {r['cover']:.0%}，最长重合 {r['longest']} 字"
                  f"（判定需 ≥{r['need']} 字）")
                if r["evidence"]:
                    A(f"- 教材中最接近的片段：`{r['evidence']}`")
            elif v == "异地":
                A(f"- 教材实际位于 **p.{r['found_page']}**（标注为 {r['page_label']}）")
                A(f"- 原文：`{r['evidence']}`")
            elif v.startswith("改写"):
                A(f"- 覆盖率 {r['cover']:.0%}，最长重合 {r['longest']} 字"
                  f"（判定需 ≥{r['need']} 字），参照页 p.{r['found_page']}")
                if r["evidence"]:
                    A(f"- 教材相邻原文：`{r['evidence']}`")
            elif v == "重组":
                A(f"- 参照页 p.{r['found_page']}")
            A("")

    A("## 二、本工具的能力边界（必读）")
    A("")
    A("1. **能抓「真话里夹假话」——这是它最有价值的一档。** `含未见片段` 用滑动窗口做"
      "**最长连续命中**，把句中没有原文支撑的连续片段单独拎出来。")
    A("   为什么必须这样：一句话只要有一部分能在教材里对上，整体覆盖率就会很高 ——"
      "例：「为实质性、深在性、可触诊的皮损，**直径一般大于 1cm**」，前半句是教材原文"
      "（14 字连续命中），覆盖率被拉到 72%，只看覆盖率就放过了。**真话越长，掩护越强。**")
    A("2. **但仍抓不到语义层面的编造。** 若整句用词都能在教材里拼出来、只是意思不同，"
      "会落到「改写」而非「无出处」。**它验证的是「交付物有没有偏离原文」，"
      "不是「知识本身是否正确」。**")
    A("3. **不折算同义词。** 教材说「毛细血管扩张」、交付物写「血管扩张」，"
      "会被判为「改写·部分」，需要人工判断是否等价。这是刻意的："
      "同义词折算表一旦写进工具，就成了新的、不可见的编辑权。")
    A("4. **检出能力有自检兜底。** `cite_check_selftest.py` 往交付物植入 3 项已知错误"
      "（编造内容 / 编造英文名 / 页码错），要求全部检出；漏报即判定工具退化。")
    A("5. **页码归属是行级的。** 同一行内若有个别文字实际来自相邻页，会记为「异地」。")
    A("6. **表格被压平。** 知识库里的表格是逐行文本，"
      "分型表的原始版面关系（哪一格对哪一行）无法自动还原，只能按文字判存否。")
    A("7. **只认约定好的 HTML 结构。** 校验器按 class 抽取单元"
      "（`row`/`cell`/`name`/`pg`、`sx`/`sxh`/`v`、`ov-grid`、`disc-grid`、"
      "`layer`、`foot`、`gnote`、`ghead`/`schead`），不是通用 HTML 文本提取器。"
      "**抽不到单元时它直接报错退出，不会给出「通过」的报告** —— "
      "「查过都没问题」和「根本没查」是两件事，不能长得一样。结构约定见 `README.md`。")
    A("")
    A("---")
    A("")
    A(f"*由 `tools/cite_check.py` 自动生成。近似匹配阈值：最长重合需达子句长度的自适应下限"
      f"（3–8 字），覆盖率 ≥{COVER_STRONG:.0%} 判「接近」、≥{COVER_WEAK:.0%} 判「部分」。*")
    return "\n".join(L)


# ---------------------------------------------------------------- 知识库定位


def kb_candidates(tools_dir: str) -> list[str]:
    """按便携包结构列出候选知识库「原文」目录。

    便携包的目录结构是固定的，`tools/` 与 `kb/` 是**兄弟目录**：

        <包根>/tools/cite_check.py
        <包根>/kb/<库名>/原文/

    所以脚本可以从自身位置推出库的位置，不需要任何绝对路径 ——
    包解压到哪个盘、哪个目录都成立。这是「解压即用、零配置」的依据。

    刻意**不**内置任何默认绝对路径：默认值一旦写成某个固定磁盘路径，
    换机器就失效；按自身位置推断则永远跟着包走。
    """
    root = os.path.dirname(os.path.abspath(tools_dir))
    kbd = os.path.join(root, "kb")
    if not os.path.isdir(kbd):
        return []
    out = []
    for name in sorted(os.listdir(kbd)):
        d = os.path.join(kbd, name, "原文")
        if os.path.isdir(d):
            out.append(d)
    return out


def resolve_kb(args_kb: str, quiet: bool) -> str:
    """确定知识库路径：显式 `--kb` 优先，否则按包结构自动定位。

    自动定位只在**恰好一个**候选时才自作主张；0 个或多个都明确报错并说明
    该传什么 —— 静默猜一个错的库，比报错危险得多。
    """
    if args_kb:
        return args_kb

    here = os.path.dirname(os.path.abspath(__file__))
    cands = kb_candidates(here)
    if len(cands) == 1:
        if not quiet:
            print(f"（未传 --kb，按包结构自动定位到：{cands[0]}）")
        return cands[0]
    if not cands:
        raise SystemExit(
            "未传 --kb，且未能按包结构自动定位到知识库。\n"
            "  自动定位规则：脚本所在 tools/ 的**同级** kb/<库名>/原文/\n"
            f"  已查找：{os.path.join(os.path.dirname(here), 'kb')}\n"
            "  两种解决办法：\n"
            "    1) 把知识库放到包的 kb/ 下（推荐，之后就不必再传 --kb）；\n"
            "    2) 显式指定：--kb <知识库原文目录 或 某一章的 .md>")
    lines = "\n".join(f"    - {c}" for c in cands)
    raise SystemExit(
        f"未传 --kb，而包内 kb/ 下有 {len(cands)} 个知识库，无法确定用哪个：\n"
        f"{lines}\n"
        "  请用 --kb 指定其中之一。")


def resolve_kb_files(kb: str, chapter: str = "", quiet: bool = False):
    """定位知识库并决定要加载哪些文件，返回 (文件列表, 库标签)。

    三种情形：
      1. `--kb` 指到某一章的 .md      → 只加载它
      2. `--kb` 指到「原文」目录 + 指定 --chapter → 只加载该章
      3. `--kb` 指到「原文」目录、未指定章 → 加载整库全部 NN-*.md

    情形 3 是默认：**宁可多加载，也不要静默只查错了的那一章**。
    漏传章号时把整库当语料，代价是慢一点，好处是别章的内容不会被误判成「无出处」。
    """
    kbpath = resolve_kb(kb, quiet)
    if not os.path.isdir(kbpath):
        if not os.path.isfile(kbpath):
            raise SystemExit(f"知识库文件不存在：{kbpath}")
        return [kbpath], kbpath

    avail = sorted({f[:2] for f in os.listdir(kbpath)
                    if f.endswith(".md") and f[:2].isdigit()})
    avail_s = "、".join(avail) if avail else "（无 NN-*.md）"
    if chapter:
        cands = [os.path.join(kbpath, f) for f in sorted(os.listdir(kbpath))
                 if f.startswith(chapter) and f.endswith(".md")]
        if not cands:
            raise SystemExit(
                f"在 {kbpath} 中未找到以 {chapter} 开头的 .md\n"
                f"  该库可用章号：{avail_s}\n"
                "  去掉 --chapter 则加载整库。")
        return cands[:1], kbpath

    files = [os.path.join(kbpath, f) for f in sorted(os.listdir(kbpath))
             if re.match(r"^\d\d-", f) and f.endswith(".md")]
    if not files:
        raise SystemExit(
            f"{kbpath} 下没有 NN-*.md（该库可用章号：{avail_s}），无法校验。")
    return files, kbpath


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="交付物 ↔ 教材知识库 引用校验")
    ap.add_argument("html", help="交付物 HTML")
    ap.add_argument("--kb", default="",
                    help="知识库「原文」目录，或直接指定某一章的 .md。"
                         "留空则按包结构自动定位（脚本所在 tools/ 同级 kb/<库名>/原文/）")
    ap.add_argument("--chapter", default="",
                    help="只校验某一章（章号前缀，如 04）；留空则加载整库")
    ap.add_argument("--out", default="", help="报告输出路径（默认写到交付物同目录）")
    ap.add_argument("--json", dest="json_out", default="", help="另存机器可读的 JSON")
    ap.add_argument("--verbose", action="store_true", help="展开「原文」级明细")
    ap.add_argument("--quiet", action="store_true", help="不打印控制台摘要")
    args = ap.parse_args()

    if not os.path.isfile(args.html):
        raise SystemExit(f"交付物不存在：{args.html}")

    # 定位知识库：显式 --kb 优先，否则按便携包结构自动定位
    files, kb_label = resolve_kb_files(args.kb, args.chapter, args.quiet)
    if not args.kb:
        args.kb = kb_label        # 自动定位时，报告里的「事实来源」也指向实际用到的库

    pages = load_kb(files)
    if not pages:
        raise SystemExit("未能从知识库中解析出任何页锚点，请检查 <!-- pdf=N book=M --> 格式")

    html_text = open(args.html, encoding="utf-8").read()
    units = extract_units(html_text)
    guard_units(units)              # 抽不到单元格就先停下，别进 check 出假绿报告

    rows, stats = check(units, pages)
    guard_units(units, stats.get("_子句总数", 0))

    span = f"p.{min(pages)}–{max(pages)}"
    nclause_real = stats.get("_子句总数", 0)
    nclause = max(nclause_real, 1)      # 仅用于算比例，避免除零；显示用真实值
    if not args.quiet:
        is_dir = os.path.isdir(kb_label)
        kb_name = (os.path.basename(os.path.dirname(kb_label)) if is_dir
                   else os.path.basename(kb_label))
        print(f"知识库：{kb_name}（{span}，{len(pages)} 页"
              + (f"，{len(files)} 个文件" if is_dir else "") + "）")
        print(f"交付物：{os.path.basename(args.html)}　单元 {len(units)} 个，"
              f"子句 {nclause_real} 条")
        print("-" * 60)
        for v in VERDICT_ORDER:
            c = stats.get(v, 0)
            if not c:
                continue
            bar = "█" * max(1, int(c / nclause * 34))
            print(f"  {VERDICT_LABEL[v]:<36}{c:>5}  {bar}")
        print("-" * 60)
        red = (stats.get("无出处", 0) + stats.get("含未见片段", 0)
               + stats.get("异地", 0))
        if red:
            print(f"  ⚠ 需处理 {red} 条"
                  f"（无出处 {stats.get('无出处', 0)}"
                  f" + 含未见片段 {stats.get('含未见片段', 0)}"
                  f" + 页码错 {stats.get('异地', 0)}）")
        else:
            print("  ✓ 未检出「教材未见」的内容")
        w = stats.get("改写·接近", 0) + stats.get("改写·部分", 0)
        if w:
            print(f"  · 待人工确认 {w} 条（改写类）")

    report = build_report(units, rows, stats, args)
    if args.out:
        out = args.out
    else:
        stem = os.path.splitext(os.path.basename(args.html))[0]
        out = os.path.join(os.path.dirname(os.path.abspath(args.html)),
                           stem + "-引用校验报告.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    if not args.quiet:
        print(f"\n报告已写入：{out}")

    if args.json_out:
        payload = {
            "html": os.path.abspath(args.html),
            "kb": os.path.abspath(kbpath),
            "page_span": span,
            "stats": stats,
            "rows": rows,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        if not args.quiet:
            print(f"JSON 已写入：{args.json_out}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
