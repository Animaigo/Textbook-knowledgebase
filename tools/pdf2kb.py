# -*- coding: utf-8 -*-
"""
教材 PDF → 知识库（Markdown）通用管线。

两个子命令
----------
  probe   <pdf>                      只诊断，不写任何知识库文件
  extract <pdf> --out <dir> [选项]   抽取为逐章 Markdown（每页带页码锚点）

为什么要有这个工具
------------------
我们整套交付物靠「印刷页码」溯源（图里每格标 p.21 / p.24 这种）。所以三件事必须自动做对：

1. **先判文字层。** 有文字层 → pymupdf 直接取字，零 OCR 误差；无文字层 → 立刻报出来，
   别硬凑（扫描件的术语误识率是灾难级的，必须换 OCR 工具再人工校对）。
2. **算出「物理页 → 印刷页」偏移。** PDF 的第 1 页往往是封面，书内 p.21 可能在 PDF 第 42 页。
   偏移搞错，全书的来源标注就全废了 —— 比不标还糟。这里从页眉/页脚自动推断。
3. **扫描抽字缺陷。** PDF 文字层常见连字断裂（`f issure`、`lichenif ication`）和
   Unicode 连字字符（ﬁ ﬂ）。前者默认只报不改，需要人工确认后走白名单修正。

切章策略（v2 更新）
-------------------
**书签的层级因书而异，不能写死"取一级目录"。** 实测《皮肤性病学》第 10 版：
一级目录 = 封面页 / 书名页 / …… / 第一篇 / 第二篇 / 推荐阅读 / 索引 / 封底页（17 条），
章在**二级**（29 条）。若按一级切，会切出一堆"封面页.md"这种垃圾文件。

现在自动挑层级：统计哪一级里 `第X章` 条目最多（≥3 条）就用那一级当章。
篇（`第X篇`）只作为分组信息写进各章页眉；篇扉页本身不并入任何章，在报告里列出。

切章策略（v3 新增：这本书根本没有书签怎么办）
--------------------------------------------
**有相当一部分 PDF 的内置书签是空的**（实测《精神病学》第 9 版：0 条）。
此时按书签切章无从谈起，退回「每 25 页机械切块」，那样的文件对溯源没有用。

这类书的**印刷目录页通常是完整的**，版面上「第X章 / 章名 / 起始页码」三行一组。
人工核对后写成**章节清单**，用 `--chapters` 传给本脚本：

    # 注释行以 # 开头；每行 = 起始书内页 + 空白 + 标题
    1	第一章 绪论
    10	第二章 精神障碍的症状学
    ...
    330	@后置材料（推荐阅读与索引）      ← @ 开头表示独立片段，文件名前缀 99

切章结果与有书签的书完全一致，`_toc.md` 也照常生成。
清单进知识库目录留档（如 `_章节目录.txt`），不是一次性临时文件 —— 重跑要能复现。

连字修正（v2 新增）
-------------------
`--ligature-fixes <文件>` 采用**白名单**方式：文件里逐行写 `坏 -> 好`，只做字面替换。

为什么不用正则自动合并：`f\\s+[il]...` 这个模式在 `of interest`、`if left` 这类**真词界**
上同样成立，自动合并会把它们粘成 `ofinterest`。白名单虽然笨，但 100% 可控可审计。

实测过的坑（别踩）
------------------
- **`--strip-running-head` 会吃掉章标题。** 章的起始页常用「第四章 皮肤病和性病的临床表现」占住
  页眉位置，与后续页的重复页眉字面完全相同。已加保险：文档首页与各章起始页不剥。但仍建议
  默认不开这个开关 —— 保留页眉对检索无害，剥错却会丢正文。
- **页码映射错一次，全库报废。** 交付物靠书内页码溯源，偏移算错等于所有标注都指向错页。
  务必看 probe 输出的置信度；低于 ~90% 就别急着抽取，先人工确认几页。
- **前言/目录页没有印刷页码**，`guess_book_page` 会推出负数或乱数。这些页在锚点里写 `book=none`。

设计原则
--------
- probe 与 extract 分离：先看清楚再动手，避免污染知识库
- 章节切分优先用 PDF 内置书签；没有书签就**只提建议，不擅自切**
- "改了可能改错"的一律只报不改；要改必须走白名单且逐条记账
- 每页写入 `<!-- pdf=N book=M -->` 锚点，方便 grep 定位与逐条溯源

用法示例
--------
    python pdf2kb.py probe "皮肤性病学.pdf"
    python pdf2kb.py extract "皮肤性病学.pdf" --out ".workbuddy/kb/皮肤性病学-第10版/原文" \
        --tidy --ligature-fixes "_连字白名单.txt"
"""
import argparse
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

try:
    import pymupdf
except ImportError:  # 老版本包名
    import fitz as pymupdf


# ---------------------------------------------------------------- 工具函数

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 可安全归一化的 Unicode 连字字符
LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl",
    "\u00ad": "",          # 软连字符：排版用，正文不该有
    "\ufeff": "",          # BOM
    "\u00a0": " ",         # 不换行空格
    "\u3000": " ",         # 全角空格（正文里通常是排版残留）
}

CHAPTER_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零〇\d]+)\s*([章篇部])\s*(.*)$")
CH_CHAPTER_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零〇\d]+)\s*章\s*(.*)$")
CH_PART_RE = re.compile(r"^\s*第\s*([一二三四五六七八九十百零〇\d]+)\s*[篇部]\s*(.*)$")

# 连字断裂检测：`f`（可带前缀字母）+ 空白 + i/l 开头的词
#   词首型 `f issure` 与词内型 `lichenif ication` 都会被这条命中。
#   ⚠ 它对 `of interest` 这类真词界同样成立 → 只报不改，改要走白名单。
LIGA_RE = re.compile(r"([A-Za-z]*f)\s+([il][a-z]{2,})\b")


def cn2int(s):
    """中文数字 → 整数（支持 四 / 十 / 十二 / 二十一 / 二〇二一 / 阿拉伯数字）。"""
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = CN_DIGIT.get(head, 1) if head else 1
        ones = CN_DIGIT.get(tail, 0) if tail else 0
        if head and head not in CN_DIGIT:
            return None
        return tens * 10 + ones
    if s and all(c in CN_DIGIT for c in s):
        return int("".join(str(CN_DIGIT[c]) for c in s))
    return None


def sanitize_filename(name, maxlen=60):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return name[:maxlen] or "未命名"


# 实测：这个 PDF 的**每条**大纲标题尾部都挂着一个 \x00（控制字符），
# 不清掉会直接导致 open() 抛 ValueError: embedded null character。
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_title(s):
    """书签标题清洗：剥掉控制字符与首尾空白。"""
    return CTRL_RE.sub("", s or "").strip()


def read_toc(doc):
    """读取并清洗书签目录，返回 [(level, title, pdf_page)]。"""
    return [(lvl, clean_title(t), p) for lvl, t, p in doc.get_toc(simple=True)]


def normalize_text(s):
    """归一化连字字符，返回 (新文本, 替换计数)。"""
    n = 0
    for bad, good in LIGATURES.items():
        c = s.count(bad)
        if c:
            s = s.replace(bad, good)
            n += c
    return s, n


def fmt_shift(shift):
    """把偏移写成人话：-21 → 「物理页 − 21」。"""
    if shift is None:
        return "未知"
    if shift >= 0:
        return f"物理页 + {shift}"
    return f"物理页 − {abs(shift)}"


def book_label(phys, shift):
    """该物理页对应的书内页码；前言页无印刷页码时返回 None。"""
    bp = phys + (shift or 0)
    return bp if bp >= 1 else None


def load_ligature_fixes(path):
    """读白名单：每行 `坏 -> 好`，`#` 开头为注释。返回 [(坏, 好)]，长串优先。"""
    fixes = []
    if not path:
        return fixes
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            for sep in ("->", "=>", "\t", "→"):
                if sep in ln:
                    bad, _, good = ln.partition(sep)
                    bad, good = bad.strip().strip("`"), good.strip().strip("`")
                    if bad and good:
                        fixes.append((bad, good))
                    break
    fixes.sort(key=lambda x: -len(x[0]))
    return fixes


def guess_book_page(blocks, page_h):
    """从页眉/页脚区域猜这一页印的是第几页。返回候选整数或 None。

    只取位于页面上下边缘、且内容几乎纯数字的块，避免把正文里的数字当页码。
    """
    cands = []
    for b in blocks:
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not text or not text.strip():
            continue
        in_bottom = y1 > page_h * 0.90
        in_top = y0 < page_h * 0.08
        if not (in_bottom or in_top):
            continue
        t = text.strip()
        if re.fullmatch(r"[\-–—\s]*\d{1,4}[\-–—\s]*", t):
            cands.append(int(re.sub(r"\D", "", t)))
        # 页眉常见的「第四章 皮肤病 …… 21」这种：取行尾数字
        elif len(t) <= 60 and re.search(r"\d{1,4}\s*$", t) and len(re.findall(r"\d", t)) <= 4:
            m = re.search(r"(\d{1,4})\s*$", t)
            if m:
                cands.append(int(m.group(1)))
    if not cands:
        return None
    # 页脚（靠下）的更可信：调用处按 y 已排序，这里取最后一个出现在底部的
    return cands[-1]


def infer_page_offset(page_numbers, total):
    """推断 shift：书内页码 = 物理页 + shift。

    page_numbers: {物理页(1-based): 书内页码}，允许缺失
    返回 (shift, 置信度, 命中数, 被采纳的物理页集合)
    """
    votes = Counter()
    for phys, book in page_numbers.items():
        votes[book - phys] += 1
    if not votes:
        return None, 0.0, 0, set()
    shift, hits = votes.most_common(1)[0]
    agree = {p for p, b in page_numbers.items() if b - p == shift}
    return shift, hits / max(len(page_numbers), 1), hits, agree


def detect_running_heads(page_lines, page_count, min_ratio=0.5, min_len=4):
    """跨页重复出现的页眉/页脚行。返回 {归一化键: 出现页数}。"""
    freq = defaultdict(set)
    for phys, lines in page_lines.items():
        seen = set()
        for ln in lines:
            key = re.sub(r"\d+", "#", ln.strip())
            key = re.sub(r"\s+", "", key)
            if len(key) >= min_len and key not in seen:
                seen.add(key)
                freq[key].add(phys)
    return {k: len(v) for k, v in freq.items() if len(v) >= max(2, page_count * min_ratio)}


def collect_ligatures(text):
    """返回 Counter：连字断裂候选 `f xxx` → 出现次数。

    键里的空白一律归一化为单个空格 —— LIGA_RE 的 `\\s` 会跨行匹配，
    不归一化的话键里带换行，报告表格会被撑断。
    """
    c = Counter()
    for m in LIGA_RE.finditer(text):
        c[re.sub(r"\s+", " ", m.group(0))] += 1
    return c


# ---------------------------------------------------------------- probe

def probe(pdf_path, show_lines=0, chapters_file=""):
    doc = pymupdf.open(pdf_path)
    total = len(doc)
    print(f"文件：{os.path.basename(pdf_path)}")
    print(f"页数：{total}")
    if doc.is_encrypted:
        print("⚠ 文档已加密（可能需口令）")
    if total:
        r = doc[0].rect
        print(f"页面尺寸：{r.width:.0f}×{r.height:.0f} pt"
              f"（{r.width / 72 * 25.4:.0f}×{r.height / 72 * 25.4:.0f} mm）")

    toc = read_toc(doc)
    lvls = Counter(l for l, _, _ in toc)
    print(f"内置书签目录：{len(toc)} 条"
          + (f"（层级分布 {'/'.join(f'{k}级×{v}' for k, v in sorted(lvls.items()))}）"
             if toc else "  ⚠ 无目录，章节需另定"
             + ("；已提供章节清单" if chapters_file else "")))

    page_char = {}
    page_lines = {}
    low_text_pages = []
    for i, page in enumerate(doc):
        txt = page.get_text("text", sort=True)
        txt, _ = normalize_text(txt)
        page_char[i + 1] = len(txt.strip())
        page_lines[i + 1] = [l for l in txt.splitlines() if l.strip()]
        if page_char[i + 1] < 80:
            low_text_pages.append(i + 1)

    total_chars = sum(page_char.values())
    avg = total_chars / total if total else 0
    print(f"文字层总字符数：{total_chars:,}（平均 {avg:.0f} 字符/页）")
    print(f"判定：{'含有效文字层，可直接抽取原文（零 OCR 误差）' if avg > 200 else '⚠ 文字层薄弱，很可能是扫描件，需先走 OCR'}")

    # 页码映射
    pn = {}
    for i, page in enumerate(doc):
        g = guess_book_page(page.get_text("blocks"), page.rect.height)
        if g is not None:
            pn[i + 1] = g
    shift, conf, hits, agree = infer_page_offset(pn, total)
    if shift is None:
        print("页码映射：⚠ 未能在页眉/页脚识别出页码（可能是扫描件或页码在图片里）")
    else:
        print(f"页码映射：书内页码 = {fmt_shift(shift)}"
              f"（PDF 物理页 1 ↔ 书内 p.{'（无页码）' if 1 + shift < 1 else 1 + shift}）"
              f"　｜　置信度 {conf * 100:.0f}%（{hits}/{len(pn)} 页）")
        bad = sorted(set(pn) - agree)
        if bad:
            print(f"          不一致的页：{bad[:20]}{' …' if len(bad) > 20 else ''}")

    if low_text_pages:
        print(f"低文字页（<80 字符，多半是整页插图）：{low_text_pages[:30]}"
              f"{' …' if len(low_text_pages) > 30 else ''}")

    # 跨页重复行（页眉页脚）
    runs = detect_running_heads(page_lines, total)
    if runs:
        print(f"疑似页眉/页脚重复行：{len(runs)} 种，例如 "
              + "、".join(f"「{k}」×{v}页" for k, v in list(runs.items())[:3]))

    # 抽字缺陷
    full = "\n".join("\n".join(v) for v in page_lines.values())
    lig = collect_ligatures(full)
    print(f"连字断裂可疑（如 `f issure`）：{len(lig)} 种 / {sum(lig.values())} 处")
    if lig:
        print("          示例：" + "、".join(f"`{k}`×{v}" for k, v in lig.most_common(6)))

    # 切章预览（优先级与 extract 一致：人工清单 > 书签 > 机械切块）
    if chapters_file:
        clist = read_chapter_list(chapters_file)
        chapters, tails = build_chapters_from_list(clist, shift, total)
        print(f"\n切章方案：按章节清单 `{os.path.basename(chapters_file)}`"
              f"（{len(chapters)} 章"
              + (f" + {len(tails)} 个独立片段" if tails else "") + "）")
        for c in chapters[:6]:
            print(f"  {c['num'] if c['num'] else '-':>3}. {c['label']}"
                  f"　pdf {c['start']}–{c['end']}"
                  f"　书内 p.{c['start'] + (shift or 0)}–{c['end'] + (shift or 0)}")
        if len(chapters) > 6:
            print(f"  …… 其余 {len(chapters) - 6} 章见 _toc.md")
    else:
        chapters, ch_level = build_chapters(toc, total)
        if chapters:
            print(f"\n切章方案：按书签第 {ch_level} 级（共 {len(chapters)} 章）")
            for c in chapters[:6]:
                print(f"  {c['num']:>3}. {c['label']}　pdf {c['start']}–{c['end']}"
                      f"　书内 p.{c['start'] + (shift or 0)}–{c['end'] + (shift or 0)}"
                      f"　[{c['part']}]")
            if len(chapters) > 6:
                print(f"  …… 其余 {len(chapters) - 6} 章见 _toc.md")
        elif toc:
            print("\n切章方案：⚠ 未识别出章级目录，将退回按 25 页机械切块")
        else:
            print("\n切章方案：⚠ 该 PDF 没有内置书签。")
            print("  若它有完整的印刷目录页：照《新书入库核对清单》把「章起始页 + 章名」"
                  "抄成清单，用 --chapters 传入，即可按章切。")
            print("  否则只能按每 25 页机械切块，章节边界需人工确认。")

    if toc:
        print("\n目录（前 20 条）：")
        for lvl, title, pg in toc[:20]:
            print(f"  {'  ' * (lvl - 1)}{title}  → pdf p.{pg}")

    if show_lines and low_text_pages:
        print(f"\n低文字页逐行内容（前 {show_lines} 页示例如下）：")
        for p in low_text_pages[:show_lines]:
            print(f"  --- pdf p.{p} ---")
            for ln in page_lines[p]:
                print("   ", ln[:100])

    doc.close()
    return {"total": total, "avg_chars": avg, "shift": shift,
            "confidence": conf, "toc": toc, "low_text_pages": low_text_pages}


# ---------------------------------------------------------------- 切章

def pick_chapter_level(toc, min_hits=3):
    """选哪一级当"章"。优先选 `第X章` 条目最多的一级；不足 min_hits 返回 None。"""
    counts = Counter()
    for lvl, t, _ in toc:
        if CH_CHAPTER_RE.match(t):
            counts[lvl] += 1
    if not counts:
        return None
    best = max(counts, key=lambda L: (counts[L], -L))
    return best if counts[best] >= min_hits else None


def build_chapters(toc, total):
    """按书签中「第X章」那一级切章。

    章的结束页 = 下一个「层级 ≤ 章级」的目录条目的前页。
    这样篇扉页（如「第二篇 皮肤性病学各论」）会自动落在所有章之外，
    不会被错误并进上一章末尾。返回 ([章 dict], 章级)。
    """
    lvl = pick_chapter_level(toc)
    if lvl is None:
        return [], None
    out = []
    for i, (l, t, p) in enumerate(toc):
        if l != lvl or not CH_CHAPTER_RE.match(t):
            continue
        end = total
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= lvl:
                end = toc[j][2] - 1
                break
        m = CH_CHAPTER_RE.match(t)
        part = ""
        for k in range(i - 1, -1, -1):
            if toc[k][0] < lvl and CH_PART_RE.match(toc[k][1]):
                part = toc[k][1]
                break
        out.append({"num": cn2int(m.group(1)),
                    "title": t.strip(),
                    "label": m.group(2).strip() or t.strip(),
                    "part": part, "start": p, "end": end})
    return out, lvl


def read_chapter_list(path):
    """读人工章节清单：每行 `<起始书内页> <标题>`，`#` 开头为注释。

    给**没有内置书签**的 PDF 用。章边界由人从印刷目录页抄下来并核对，
    脚本只负责按它切页。标题以 `@` 开头表示「独立片段」（如后置的推荐阅读与索引）。
    返回 [(书内页, 标题)]，按页码升序。
    """
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, ln in enumerate(f, 1):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                raise SystemExit(
                    f"{path} 第 {i} 行格式不对，应为「起始书内页<TAB>标题」：{s}")
            rows.append((int(parts[0]), parts[1].strip()))
    if not rows:
        raise SystemExit(f"{path} 里没有任何章节行")
    rows.sort(key=lambda x: x[0])
    dup = [r for r in rows if rows.count(r[0]) > 1]
    if dup:
        raise SystemExit(f"{path} 里有重复的起始页：{sorted({d[0] for d in dup})}")
    return rows


def build_chapters_from_list(clist, shift, total):
    """按章节清单切章。清单给的是**书内页码**，这里换算成 PDF 物理页。

    章的结束页 = 下一条目起始页的前一页，最后一条到文末。
    返回 (章列表, 独立片段列表)，元素结构与 `build_chapters` 一致。
    """
    chapters, tails = [], []
    for i, (bp, title) in enumerate(clist):
        phys = bp - (shift or 0)
        if phys < 1:
            raise SystemExit(
                f"清单里的书内 p.{bp} 换算成 PDF 物理页是 {phys}，越界。"
                f"检查页码映射（书内页码 = {fmt_shift(shift)}）是否与本 PDF 匹配。")
        end = total if i + 1 >= len(clist) else clist[i + 1][0] - (shift or 0) - 1
        if end < phys:
            raise SystemExit(
                f"清单顺序有误：「{title}」（书内 p.{bp}）的区间在下一页之前就结束了。")
        if title.startswith("@"):
            tails.append({"title": title[1:].strip(), "start": phys, "end": end})
            continue
        m = CH_CHAPTER_RE.match(title)
        chapters.append({"num": cn2int(m.group(1)) if m else None,
                         "title": title,
                         "label": (m.group(2).strip() if m else "") or title,
                         "part": "", "start": phys, "end": end})
    return chapters, tails


# ---------------------------------------------------------------- extract

def extract(pdf_path, out_dir, kb_title, strip_running, page_images_dir, dpi,
            tidy, liga_file="", chapters_file=""):
    doc = pymupdf.open(pdf_path)
    total = len(doc)
    os.makedirs(out_dir, exist_ok=True)
    src_name = os.path.basename(pdf_path)
    fixes = load_ligature_fixes(liga_file)
    fix_hits = Counter()

    # ---- 收集每页文本 + 页码
    pages_text = {}
    page_numbers = {}
    for i, page in enumerate(doc):
        raw = page.get_text("text", sort=True)
        raw, _ = normalize_text(raw)
        pages_text[i + 1] = raw
        g = guess_book_page(page.get_text("blocks"), page.rect.height)
        if g is not None:
            page_numbers[i + 1] = g

    shift, conf, hits, agree = infer_page_offset(page_numbers, total)
    shift = shift if shift is not None else 0

    # ---- 页眉页脚行（用于可选剥离）
    #
    # ⚠ 已实测的坑：章的**起始页**往往用「第四章 皮肤病和性病的临床表现」占住页眉位置，
    #   与后续页的重复页眉**字面完全相同**。若无脑按字面剥离，章标题就被吃掉了。
    #   所以这里加两道保险：① 文档首页不剥；② 有书签目录时，各章起始页不剥。
    lines_by_page = {p: [l for l in t.splitlines() if l.strip()] for p, t in pages_text.items()}
    toc_all = read_toc(doc)
    # 保护页：文档首页 + 所有「层级 ≤ 章级」的目录条目所在页（篇扉页、章起始页）
    _ch_level_guess = pick_chapter_level(toc_all) or 1
    chapter_starts = {p for lvl, _t, p in toc_all if lvl <= _ch_level_guess}
    runs = detect_running_heads(lines_by_page, total) if strip_running else {}
    strip_keys = set(runs)
    protected = chapter_starts | {1}

    def page_body(phys):
        lines = lines_by_page[phys]
        out = []
        for ln in lines:
            if strip_keys and phys not in protected:
                k = re.sub(r"\d+", "#", ln.strip())
                k = re.sub(r"\s+", "", k)
                if k in strip_keys:
                    continue
            for bad, good in fixes:          # 白名单连字修正（逐条记账）
                if bad in ln:
                    n = ln.count(bad)
                    fix_hits[f"{bad} -> {good}"] += n
                    ln = ln.replace(bad, good)
            if tidy:
                # 只动空白，不动任何字符：去掉行尾空白、把行内连续空格压成一个。
                # 行首缩进保留 —— 它是教材版式给出的层级线索（章 / 节 / （一） / 1.）。
                indent = ln[:len(ln) - len(ln.lstrip())]
                ln = indent + re.sub(r"\s{2,}", " ", ln.strip())
            out.append(ln)
        return "\n".join(out).strip()

    def book_of(phys):
        return book_label(phys, shift)

    def anchor(phys):
        bp = book_of(phys)
        return f"<!-- pdf={phys} book={bp if bp else 'none'} -->"

    def bp_str(phys):
        bp = book_of(phys)
        return f"p.{bp}" if bp else "（无印刷页码）"

    def write_chunk(fname, title, blurb, start, end):
        buf = [f"# {title}\n\n", blurb, "\n---\n\n"]
        for phys in range(start, end + 1):
            buf.append(anchor(phys) + "\n")
            body = page_body(phys)
            buf.append((body if body else "[本页无可提取文字，疑为整页插图]") + "\n")
        pathlib_write(os.path.join(out_dir, fname), "\n".join(buf))
        return (fname, start, end)

    # ---- 章节切分
    #   优先级：人工章节清单（无书签的书）> PDF 内置书签 > 每 25 页机械切块（兜底）
    if chapters_file:
        clist = read_chapter_list(chapters_file)
        chapters, tails = build_chapters_from_list(clist, shift, total)
        ch_level = None
        split_src = (f"印刷目录页清单 `{os.path.basename(chapters_file)}`"
                     f"（{len(chapters)} 章"
                     + (f" + {len(tails)} 个独立片段" if tails else "")
                     + "，已人工核对）")
    else:
        chapters, ch_level = build_chapters(toc_all, total)
        tails = []
        split_src = (f"PDF 内置书签第 {ch_level} 级（「第X章」所在级）"
                     if chapters else "")

    written = []
    skipped = []          # 未并入任何章的页（篇扉页等）

    segs = [("ch", c) for c in chapters] + [("tail", t) for t in tails]
    segs.sort(key=lambda x: x[1]["start"])

    if segs:
        first_start = segs[0][1]["start"]
        if first_start > 1:
            written.append(write_chunk(
                "00-前置材料.md", "前置材料（封面至目录）",
                f"> PDF 物理页：1–{first_start - 1}　｜　来源文件：{src_name}\n"
                f"> 这部分是封面、版权页、序言、前言、目录等，**正文无印刷页码**，"
                f"故锚点记为 `book=none`。\n"
                f"> 抽取方式：PDF 文字层直接提取（未经 OCR）",
                1, first_start - 1))
        for kind, c in segs:
            if kind == "ch":
                idx = c["num"] if c["num"] else len(written) + 1
                fname = f"{idx:02d}-{sanitize_filename(c['label'])}.md"
                blurb = (f"> **{c['title']}**"
                         + (f"　（{c['part']}）" if c["part"] else "") + "\n"
                         f"> 教材书内页码：{bp_str(c['start'])}–{bp_str(c['end'])}"
                         f"　｜　PDF 物理页：{c['start']}–{c['end']}\n"
                         f"> 来源文件：{src_name}　｜　书内页码 = {fmt_shift(shift)}"
                         f"（置信度 {conf * 100:.0f}%）\n"
                         f"> 抽取方式：PDF 文字层直接提取（未经 OCR）")
            else:
                # 清单里的 `@` 片段（推荐阅读、索引等）：单独成文，前缀 99
                fname = f"99-{sanitize_filename(c['title'])}.md"
                blurb = (f"> **{c['title']}**（位于正文之后）\n"
                         f"> 教材书内页码：{bp_str(c['start'])}–{bp_str(c['end'])}"
                         f"　｜　PDF 物理页：{c['start']}–{c['end']}\n"
                         f"> 来源文件：{src_name}\n"
                         f"> 抽取方式：PDF 文字层直接提取（未经 OCR）")
            written.append(write_chunk(fname, c["title"], blurb, c["start"], c["end"]))
            print(f"  写出 {fname}  ({bp_str(c['start'])}–{bp_str(c['end'])})")
        if not chapters_file:
            last_end = max(e for _f, _s, e in written)
            if last_end < total:
                written.append(write_chunk(
                    "99-后置材料.md", "后置材料（推荐阅读与索引）",
                    f"> PDF 物理页：{last_end + 1}–{total}　｜　来源文件：{src_name}\n"
                    f"> 含推荐阅读、中英文名词对照索引、封底页。\n"
                    f"> 抽取方式：PDF 文字层直接提取（未经 OCR）",
                    last_end + 1, total))
        # 找出所有没被任何片段的区间覆盖的页
        covered = set()
        for _f, s, e in written:
            covered |= set(range(s, e + 1))
        skipped = [p for p in range(1, total + 1) if p not in covered]
    else:
        # 无目录 → 不擅自切章，按固定页数分块，交人工确认
        CHUNK = 25
        for s in range(1, total + 1, CHUNK):
            e = min(s + CHUNK - 1, total)
            fname = f"未编目-pdf{s:04d}-{e:04d}.md"
            written.append(write_chunk(
                fname, f"未编目片段（PDF 物理页 {s}–{e}）",
                f"> 教材书内页码：{bp_str(s)}–{bp_str(e)}　｜　来源文件：{src_name}\n"
                f"> ⚠ 该 PDF 未识别出章级书签目录，此处按每 {CHUNK} 页机械切块，"
                f"**章节边界需人工确认**。", s, e))
            print(f"  写出 {fname}  ({bp_str(s)}–{bp_str(e)})")

    # ---- 完整目录（含页码映射）
    def write_toc():
        if chapters_file:
            rp = [f"# 目录 · {kb_title}", "",
                  f"> 来源：`{src_name}` **无内置书签**，本表由印刷目录页整理而成",
                  f"> 章节清单：`{os.path.basename(chapters_file)}`"
                  f"（{len(clist)} 条，章起始页已与书本目录页逐条核对）",
                  f"> 书内页码 = {fmt_shift(shift)}　｜　"
                  f"「书内页」列为 `—` 的条目位于正文前，无印刷页码", "",
                  "| 层级 | 标题 | PDF 物理页 | 书内页 |", "|---|---|---|---|"]
            for bp, title in clist:
                shown = title[1:].strip() if title.startswith("@") else title
                rp.append(f"| 1 | {shown} | {bp - (shift or 0)} | {bp} |")
            pathlib_write(os.path.join(out_dir, "_toc.md"), "\n".join(rp) + "\n")
            return
        rp = [f"# 目录 · {kb_title}", "",
              f"> 来源：`{src_name}` 的内置书签（{len(toc_all)} 条）",
              f"> 书内页码 = {fmt_shift(shift)}　｜　"
              f"「书内页」列为 `—` 的条目位于正文前，无印刷页码", "",
              "| 层级 | 标题 | PDF 物理页 | 书内页 |", "|---|---|---|---|"]
        for lvl, t, p in toc_all:
            bp = book_label(p, shift)
            rp.append(f"| {'　' * (lvl - 1)}{lvl} | {t} | {p} | "
                      f"{bp if bp else '—'} |")
        pathlib_write(os.path.join(out_dir, "_toc.md"), "\n".join(rp) + "\n")

    write_toc()

    # ---- 页面图片（视觉核对备用）
    if page_images_dir:
        os.makedirs(page_images_dir, exist_ok=True)
        for i, page in enumerate(doc):
            page.get_pixmap(dpi=dpi).save(
                os.path.join(page_images_dir, f"pdfp{i + 1:04d}.png"))

    # ---- 抽取报告
    low = [p for p, t in pages_text.items() if len(t.strip()) < 80]
    lig = collect_ligatures("\n".join(pages_text.values()))
    applied = len(fixes) > 0
    rp = [f"# 抽取报告 · {kb_title}", "",
          f"- 来源文件：`{src_name}`",
          f"- PDF 物理页数：{total}",
          f"- 页码映射：**书内页码 = {fmt_shift(shift)}**，置信度 {conf * 100:.0f}%",
          f"- 切章依据：{split_src if segs else '⚠ 未识别出章级目录，按每 25 页机械切块'}",
          f"- 逐页锚点：每个 md 内以 `<!-- pdf=N book=M -->` 标记，可直接 grep 定位",
          f"- 连字修正：{'已启用白名单，共 ' + str(sum(fix_hits.values())) + ' 处' if applied else '**未启用**（候选见下节）'}", ""]

    if applied and fix_hits:
        rp += ["## 已应用的连字修正（白名单）", "", "| 修正 | 处数 |", "|---|---|"]
        for k, v in fix_hits.most_common():
            rp.append(f"| `{k}` | {v} |")
        rp += [""]

    if skipped:
        rp += [f"## 未并入任何文件的页（{len(skipped)} 页）", "",
               "这些页是**篇扉页**（如「第二篇 皮肤性病学各论」）—— 书签层级 ≤ 章级，"
               "被切章算法排除，属正常。若需保留可单独处理：", "",
               "　" + "、".join(f"pdf p.{p}（{bp_str(p)}）" for p in skipped), ""]

    if low:
        rp += [f"## 需人工核对：低文字页（{len(low)} 页）", "",
               "这些页几乎提取不到文字，通常是整页插图或扫描图，务必翻书确认：", "",
               "　" + "、".join(f"pdf p.{p}（{bp_str(p)}）" for p in low), ""]

    if lig:
        rp += [f"## 需人工核对：连字断裂可疑（{len(lig)} 种 / {sum(lig.values())} 处）", "",
               "PDF 文字层把 fi/fl 连字拆成「f + 空格 + 词」的缺陷（词首型 `f issure`、",
               "词内型 `lichenif ication` 都会被命中）。**脚本默认不自动合并** ——",
               "`of interest`、`if left` 这类**真词界**同样符合该模式，自动合并会粘成不存在的词。",
               "确认后用 `--ligature-fixes` 走白名单修正。", "",
               "| 可疑片段 | 处数 | 合并后 |", "|---|---|---|"]
        for k, v in lig.most_common():
            rp.append(f"| `{k}` | {v} | `{k.replace(' ', '')}` |")
        rp += [""]

    rp += ["## 文件清单", "", "| 文件 | PDF 物理页 |", "|---|---|"]
    for fname, s, e in written:
        rp.append(f"| `{fname}` | {s}–{e} |")
    rp += ["", "## 下一步", "",
           "1. 抽查 3–5 页与纸质书逐字比对（重点核对上表可疑项）",
           "2. 回填 `原文索引.md` 的主题 → 文件 + 页码映射",
           "3. 交付物类内容一律从此目录取材，并标注 `p.xx`",
           ""]
    pathlib_write(os.path.join(out_dir, "_extract_report.md"), "\n".join(rp))
    print("  写出 _extract_report.md")
    print("  写出 _toc.md")
    doc.close()


def pathlib_write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="教材 PDF → 知识库 Markdown 管线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("probe", help="只诊断，不写知识库")
    p1.add_argument("pdf")
    p1.add_argument("--show-lines", type=int, default=0,
                    help="额外打印前 N 个低文字页的逐行内容")
    p1.add_argument("--chapters", default="",
                    help="章节清单文件（无内置书签的书用），每行 `起始书内页<TAB>标题`")

    p2 = sub.add_parser("extract", help="抽取为逐章 Markdown")
    p2.add_argument("pdf")
    p2.add_argument("--out", required=True, help="知识库目录")
    p2.add_argument("--title", default="", help="知识库标题，默认取目录名")
    p2.add_argument("--strip-running-head", action="store_true",
                    help="剥离跨页重复的页眉页脚行（默认保留，先看清再决定）")
    p2.add_argument("--tidy", action="store_true",
                    help="只整理空白（去行尾空格、行内多空格压成一个），不改任何字符")
    p2.add_argument("--ligature-fixes", default="",
                    help="连字修正白名单文件，每行 `坏 -> 好`；不传则不修")
    p2.add_argument("--page-images", default="", help="导出页面图片到此目录（视觉核对用）")
    p2.add_argument("--dpi", type=int, default=150, help="页面图片分辨率，默认 150")
    p2.add_argument("--chapters", default="",
                    help="章节清单文件（无内置书签的书用），每行 `起始书内页<TAB>标题`；"
                         "标题以 @ 开头者为正文后的独立片段")

    args = ap.parse_args()
    if not os.path.isfile(args.pdf):
        raise SystemExit(f"文件不存在：{args.pdf}")

    if args.cmd == "probe":
        probe(args.pdf, args.show_lines, args.chapters)
    else:
        title = args.title or os.path.basename(os.path.abspath(args.out))
        print(f"抽取 {os.path.basename(args.pdf)} → {args.out}")
        extract(args.pdf, args.out, title, args.strip_running_head,
                args.page_images, args.dpi, args.tidy, args.ligature_fixes,
                args.chapters)


if __name__ == "__main__":
    main()
