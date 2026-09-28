# -*- coding: utf-8 -*-
"""
HTML → PDF 转换（调用本机 Edge / Chrome 的 headless 打印引擎）。

特点：
1. 不修改源 HTML —— 打印优化 CSS 只注入到临时副本
2. 自动避开"表格行被分页截断"的问题（break-inside:avoid）
3. 源文件的 @page 若写在 @media print 内可能不生效，这里在顶层再声明一次
4. 中文路径安全：临时文件走系统 temp，浏览器只看到 ASCII 路径

用法：
    python html2pdf.py <输入.html> <输出.pdf>
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

PRINT_CSS_TMPL = """
/* ---- 打印优化 CSS：由 html2pdf.py 自动注入，源文件未被修改 ---- */
/* 纸张尺寸从源文件的 @page 读取（源文件的 @page 常写在 @media print 内而不生效，
   这里在样式表顶层重新声明一次）；源文件未声明时默认 A3 landscape。 */
@page {{ size: {page_size}; margin: 8mm; }}
@media print {{
  body {{ background: #fff; }}
  /* 只保护"行级"元素不被分页截断。
     注意：不要对 .grp / .hero 这类整块卡片用 avoid —— 卡片高度可达数页，
     会被整体推到下一页，造成上一页大面积空白。 */
  .row, .layer, .sub-grid {{
    break-inside: avoid; page-break-inside: avoid;
  }}
  .head, .ghead {{ break-after: avoid; page-break-after: avoid; }}
  .name, .cell, .sub-grid > div, .disc-item {{ break-inside: avoid; }}
  /* 尾部小卡片整体不拆页（体积小，不会造成大片留白） */
  .foot, .disc, .ov, .sx {{ break-inside: avoid; page-break-inside: avoid; }}
  /* 轻微收紧间距，避免只剩页脚一行溢出到新页 */
  body {{ padding-bottom: 0; }}
  .wrap {{ max-width: none; }}
  .hero, .grp {{ margin-bottom: 12px; }}
  .disc, .foot, .ov {{ margin-bottom: 10px; }}
  .layer {{ margin-top: 13px; margin-bottom: 3px; }}
  .tbl {{ padding-bottom: 10px; }}
  /* 症状图（A4）：收紧间距，避免页脚被挤到第二页 */
  .hero {{ padding: 11px 20px; margin-bottom: 7px; }}
  .hero h1 {{ font-size: 18px; margin-bottom: 5px; }}
  .hero .meta {{ line-height: 1.6; }}
  .hero .src {{ margin-top: 7px; padding: 7px 13px; line-height: 1.6; }}
  .ov, .sx-wrap {{ margin-bottom: 7px; }}
  .ov {{ padding: 11px 18px 12px; }}
  .ov-grid {{ gap: 4px 20px; }}
  .sxb {{ padding-bottom: 4px; }}
  .sxb .r {{ padding: 4px 15px; }}
  .sxh {{ padding: 9px 18px; }}
  /* 页脚收紧：最后一页往往只差几毫米就能并回上一页 */
  .foot {{ font-size: 11px; line-height: 1.6; padding: 8px 18px; }}
  .fgrid {{ gap: 7px 18px; }}
  .foot .end {{ margin-top: 4px; }}
  .sx-wrap {{ margin-bottom: 5px; }}
}}
"""

# 从源 HTML 的 @page 规则里读纸张尺寸，读不到就用这个
DEFAULT_PAGE_SIZE = "A3 landscape"


def detect_page_size(html):
    m = re.search(r"@page\s*\{[^}]*?size:\s*([^;}]+)", html, re.I)
    if m:
        return m.group(1).strip()
    return DEFAULT_PAGE_SIZE


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    raise SystemExit("找不到 Edge 或 Chrome，无法转换。")


def convert(src_html, out_pdf):
    src_html = pathlib.Path(src_html).resolve()
    out_pdf = pathlib.Path(out_pdf).resolve()
    if not src_html.exists():
        raise SystemExit(f"源文件不存在：{src_html}")

    html = src_html.read_text(encoding="utf-8")
    if "</style>" not in html:
        raise SystemExit("HTML 中未找到 </style>，无法注入打印样式。")
    page_size = detect_page_size(html)
    html = html.replace("</style>", PRINT_CSS_TMPL.format(page_size=page_size) + "</style>", 1)

    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="html2pdf_"))
    tmp_html = tmpdir / "src.html"
    tmp_pdf = tmpdir / "out.pdf"
    tmp_html.write_text(html, encoding="utf-8")

    browser = find_browser()
    base = [
        browser,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={tmpdir / 'profile'}",
        "--no-pdf-header-footer",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=8000",
        f"--print-to-pdf={tmp_pdf}",
        tmp_html.as_uri(),
    ]

    last_err = ""
    for flag in ("--headless=new", "--headless"):
        cmd = [base[0], flag] + base[1:]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            last_err = (r.stderr or "")[-1500:]
        except subprocess.TimeoutExpired:
            last_err = "浏览器调用超时"
            continue
        if tmp_pdf.exists() and tmp_pdf.stat().st_size > 0:
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp_pdf, out_pdf)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return out_pdf

    shutil.rmtree(tmpdir, ignore_errors=True)
    raise SystemExit(f"PDF 生成失败。\n浏览器输出：\n{last_err}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    print("OK:", convert(sys.argv[1], sys.argv[2]))
