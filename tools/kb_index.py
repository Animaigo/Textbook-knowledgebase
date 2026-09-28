# -*- coding: utf-8 -*-
"""
从已抽取的知识库生成 `原文索引.md`。

输入：
  `原文/_toc.md`            书签目录（层级 / 标题 / 物理页 / 书内页）
  `原文/_extract_report.md` 抽取体检报告（书名 / 来源文件 / 页数 / 页码映射 / 置信度）
  `原文/*.md`               实际文件清单
输出：
  知识库根目录的 `原文索引.md`

为什么单独写个脚本：索引是「想找什么 → 哪个文件 + 哪一页」的入口，
手工维护几百条必然和原文脱节。生成式索引永远与实际文件一致。

**书名、页码公式、置信度全部从上面两份报告读取，本脚本不写死任何一本资料的信息。**
库专属内容（条目级细目表、待办、交付物登记）放在知识库根的 `_索引补充.md`，
若存在则原样追加到索引末尾 —— 学科内容不进本脚本。

用法：
    python kb_index.py --kb ".workbuddy/kb/妇产科学-第10版"
"""
import argparse
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

TOC_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*$")
HEAD_BOOK = re.compile(r"<!--\s*pdf=(\d+)\s+book=(\d+|none)\s*-->")
# 章 / 节按**标题**判定，不按书签层级 —— 层级因书而异：有书签的书章可能在二级，
# 无书签的书由章节清单生成、全是 1 级。按层级统计会把后者的章数算成 0。
CH_RE = re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*章")
SEC_RE = re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*节")

SUPPLEMENT = "_索引补充.md"


def read_toc(kb):
    """读 `原文/_toc.md`，返回 [(level, title, pdf_page, book_page)]。"""
    path = os.path.join(kb, "原文", "_toc.md")
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            m = TOC_ROW.match(ln.rstrip())
            if not m:
                continue
            lvl = int(m.group(1))
            title = m.group(2).strip().replace("　", " ")
            pdfp = int(m.group(3))
            book = m.group(4).strip()
            bp = None if book in ("—", "-", "") else int(book)
            rows.append((lvl, title, pdfp, bp))
    return rows


def read_files(kb):
    """返回 [(文件名, 起 pdf 页, 止 pdf 页)]，按文件名排序。"""
    d = os.path.join(kb, "原文")
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        pages = []
        with open(os.path.join(d, name), encoding="utf-8") as f:
            for ln in f:
                m = HEAD_BOOK.match(ln.strip())
                if m:
                    pages.append(int(m.group(1)))
        if pages:
            out.append((name, min(pages), max(pages)))
    return out


def read_report(kb):
    """从 `原文/_extract_report.md` 读元信息。缺失字段留 None，由调用方兜底。"""
    meta = {}
    path = os.path.join(kb, "原文", "_extract_report.md")
    if not os.path.isfile(path):
        return meta
    with open(path, encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            m = re.match(r"^#\s*抽取报告\s*·\s*(.+)$", s)
            if m:
                meta["raw_title"] = m.group(1).strip()
            m = re.match(r"^-\s*来源文件：`(.+?)`", s)
            if m:
                meta["src"] = m.group(1).strip()
            m = re.match(r"^-\s*PDF 物理页数：(\d+)", s)
            if m:
                meta["pages"] = int(m.group(1))
            m = re.search(r"书内页码\s*=\s*物理页\s*([−\-+＋])\s*(\d+)", s)
            if m:
                meta["sign"] = "+" if m.group(1) in "+＋" else "−"
                meta["offset"] = int(m.group(2))
            m = re.search(r"置信度\s*(\d+)\s*%", s)
            if m:
                meta["conf"] = int(m.group(1))
            m = re.match(r"^-\s*切章依据：(.+)$", s)
            if m:
                meta["split"] = m.group(1).strip()
    return meta


def book_title(raw, kb):
    """把「皮肤性病学（第10版）」/「妇产科学-第10版」一类写法规范成《书名》第 N 版。"""
    t = (raw or "").strip() or os.path.basename(os.path.normpath(kb))
    for pat in (r"^(.+?)[-－]\s*第\s*(\d+)\s*版$", r"^(.+?)（第\s*(\d+)\s*版）$"):
        m = re.match(pat, t)
        if m:
            return "《%s》第 %s 版" % (m.group(1).strip(), m.group(2))
    m = re.match(r"^(.+?)\s*第\s*(\d+)\s*版$", t)
    if m:
        return "《%s》第 %s 版" % (m.group(1).strip(), m.group(2))
    return "《%s》" % t


def scan_dir(kb, n_toc):
    """扫描知识库根目录，生成「目录下的其他文件」表。缺什么就不列什么。"""
    rows = []
    for name in sorted(os.listdir(kb)):
        p = os.path.join(kb, name)
        if name in ("原文索引.md", "原文"):
            continue
        if os.path.isdir(p):
            if name == "_pages":
                rows.append(("`_pages/`", "页面图片，视觉核对备用"))
            else:
                rows.append(("`%s/`" % name, "—"))
            continue
        if name.startswith("_连字修正白名单"):
            rows.append(("`%s`" % name, "连字断裂的逐条修正表（只做字面替换）"))
        elif name == "00-使用说明与版本信息.md":
            rows.append(("`%s`" % name, "抽取规范、标记约定、交付物书写约定、引用校验流程"))
        elif name == SUPPLEMENT:
            rows.append(("`%s`" % name, "本库专属附录：条目级细目、待办、交付物登记"))
        elif name == "_章节目录.txt":
            rows.append(("`%s`" % name,
                         "章节清单：该 PDF 无内置书签，章边界由本文件给定（`pdf2kb.py --chapters`）"))
        elif re.match(r"^\d\d-", name) and name.endswith(".md"):
            rows.append(("`%s`" % name, "留档"))
        else:
            rows.append(("`%s`" % name, "—"))
    rows.append(("`原文/`", "**原文（唯一事实来源）**"))
    rows.append(("`原文/_toc.md`", "完整书签目录（%d 条）＋页码映射" % n_toc))
    rows.append(("`原文/_extract_report.md`",
                 "本次抽取的体检报告：低文字页、连字清单、未并入页"))
    return rows


def file_for(pdfp, files):
    for name, s, e in files:
        if s <= pdfp <= e:
            return name
    return "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    args = ap.parse_args()
    kb = args.kb

    toc = read_toc(kb)
    files = read_files(kb)
    meta = read_report(kb)
    n_ch = sum(1 for l in toc if CH_RE.match(l[1]))
    n_sec = sum(1 for l in toc if SEC_RE.match(l[1]))

    title = book_title(meta.get("raw_title"), kb)
    bp = [r[3] for r in toc if r[3]]
    book_range = ("%d–%d" % (min(bp), max(bp))) if bp else "—"
    off = meta.get("offset")
    sign = meta.get("sign") or "−"
    if off is None:
        formula = "见 `原文/_extract_report.md`"
        back = "书内页 ± 偏移"
    else:
        formula = "书内页码 = PDF 物理页 %s %d" % (sign, off)
        back = "book %s %d" % ("+" if sign == "−" else "−", off)
        if meta.get("conf") is not None:
            formula += "（自动推断置信度 %d%%）" % meta["conf"]
    n_pages = meta.get("pages")

    L = []
    L += ["# 原文索引 ·%s" % title, "",
          "> **用途**：把「想找什么」映射到「哪个文件 + 哪一页」。这是知识库的入口，"
          "也是交付物标注页码时的对照表。",
          "> **原文目录**：`原文/`"
          + ("　｜　PDF 物理页 %d 页" % n_pages if n_pages else "")
          + "　｜　书内 p.%s" % book_range,
          "> **页码公式**：%s" % formula]
    if meta.get("src"):
        L.append("> **来源文件**：`%s`" % meta["src"])
    if meta.get("split"):
        L.append("> **切章依据**：%s" % meta["split"])
    L += ["> **生成方式**：`tools/kb_index.py` 从 `原文/_toc.md` 与 "
          "`原文/_extract_report.md` 自动生成，勿手工维护条目（会与原文脱节）", "",
          "## 一、怎么用", "",
          "1. **精确引用** → 在 `原文/` 里 `Grep` 关键词，命中行附近就是 `<!-- pdf=N book=M -->` 锚点，"
          "`book` 即该页印刷页码。",
          "2. **整章阅读** → 按下表直接取文件；文件头有页码区间与来源说明。",
          "3. **写交付物** → 逐格标 `p.xx`；原文没有的写「教材未述」，不得用模型知识补。",
          "4. **核对** → 用 `book` 值反推 PDF 物理页 = %s，可回原 PDF 翻页比对。" % back, "",
          "> ⚠️ **文本保留了 PDF 的原始换行**（每行约 40 字），且页眉、页码、图注**原样保留在文中**，",
          "> 段落会在跨页处被这些版面元素打断。这是刻意的 —— 只做字符级保真，不做编辑性重排。",
          "> 取长句时请用短关键词检索，然后按 `book` 锚点读上下文。", "",
          "## 二、章节目录（全 %d 条：章 %d / 节 %d / 其他 %d）"
          % (len(toc), n_ch, n_sec, len(toc) - n_ch - n_sec),
          "",
          "| 标题 | 书内页 | 文件 |", "|---|---|---|"]

    for lvl, t, pdfp, b in toc:
        f = file_for(pdfp, files)
        if SEC_RE.match(t):          # 节：缩进，不加粗
            L.append("| 　　%s | %s | `%s` |" % (t, b if b else "—", f))
        else:                        # 篇 / 章：顶格加粗
            L.append("| **%s** | %s | `%s` |" % (t, b if b else "—", f))

    L += ["",
          "## 三、文件清单", "",
          "| 文件 | PDF 物理页 |", "|---|---|"]
    for name, s, e in files:
        L.append("| `原文/%s` | %s–%s |" % (name, s, e))

    L += ["",
          "## 四、目录下的其他文件", "",
          "| 条目 | 用途 |", "|---|---|"]
    for a, b in scan_dir(kb, len(toc)):
        L.append("| %s | %s |" % (a, b))

    L += ["",
          "## 五、交付物约定", "",
          "**统一口径**：每格标页码 · 原文未述处标「教材未述」· 别名用 `/` · 并列项用顿号",
          "",
          "**定稿门槛**：交付物必须跑 `tools/cite_check.py`，"
          "且报告里不得出现「无出处」或「含未见片段」。", ""]

    sup = os.path.join(kb, SUPPLEMENT)
    if os.path.isfile(sup):
        with open(sup, encoding="utf-8") as f:
            body = f.read().rstrip()
        if body:
            L += [body, ""]

    out = os.path.join(kb, "原文索引.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("写出", out, "| %d 行" % len(L))


if __name__ == "__main__":
    main()
