#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把知识库打包成可迁移的便携包（zip）。

用途：换电脑 / 备份 / 发给别人。产出的 zip 解压到任意路径都能直接用，
     因为所有工具都靠显式参数定位文件，不含硬编码路径。

用法：
    python make_portable.py --kb <知识库目录> [--out <zip路径>] [--keep-pages] [--tools <工具目录>]
    python make_portable.py --dist                 # 只含工具链、不含教材原文（发给别人 / 传公开仓库）

两种模式：
    **便携包**（默认，需 --kb）：kb/ + tools/ + skills/ + README，含教材原文，自己换机/备份用。
    **分发包**（--dist）：不含任何教材原文，kb/ 下只放一份「放知识库在这里」的说明；
                          额外带面向新读者的 README、新书入库核对清单、.gitignore。

默认行为：
    - 收录 kb/ 与 tools/，排除 _pages/（页面图片，仅视觉核对用，占体积大头）
    - 若本机存在配套智能体技能（~/.workbuddy/skills/textbook-kb-cite），一并收进 skills/
    - zip 内结构：README-先读我.md / kb/<库名>/... / tools/... / skills/...
    - 打包后自动打印体积与文件数

加 `--deploy <落点目录>` 则打包后**就地覆盖**同步到落点（如 `--deploy D:/kb`），
把「工作副本 / zip / 落点主副本」三处一次对齐；只覆盖同名文件，不删除任何目录。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import zipfile

SKIP_DIRS = {"__pycache__", ".git", ".idea"}
SKIP_EXTS = {".pyc", ".pyo", ".zip"}

# 配套技能（把「先读原文→写作标页码→跑校验」的流程固化下来，默认随包带上）
DEFAULT_SKILL = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "textbook-kb-cite")


def human(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n/1:.1f}{u}"
        n /= 1024.0
    return f"{n:.1f}GB"


def collect(root: str, arc_prefix: str, extra_skip_dirs: set[str]):
    """产出 (绝对路径, zip 内路径)。跳过缓存目录与二进制垃圾。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and d not in extra_skip_dirs]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in SKIP_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((full, f"{arc_prefix}/{rel}"))
    return out


README = r"""# 教材知识库 · 便携包

一本资料的原文（带页码锚点）+ 配套工具链 + 引用校验器。
解压到固定位置长期使用；包内无硬编码路径，换盘符、换目录、换电脑均可直接运行。

包内 `tools/` 与 `skills/` 不绑定本书：同一套脚本可为任何带文字层的 PDF 建库，
换成法学、工程、金融等资料用法完全一致，只是校验依据的原文不同。

## 1. 包内结构

```text
kb/<书名>/              原文（唯一事实来源）+ 索引 + 使用说明
tools/                  全部脚本
skills/                 AI 技能，可选，复制到 ~/.workbuddy/skills/
README-先读我.md        本文件
```

每本资料一个便携包，包名形如 `<书名>-便携包`；`tools/` 是同一套，各包内各存一份。

## 2. 落点

保持 `<书名>-便携包\` 这一层结构放在固定位置即可，本机统一放在 `D:\kb\` 下：

```text
D:\kb\<书名>-便携包\
```

不要放在随会话变动的工程目录：落点一飘，各处引用都要重新指路。
换电脑换盘符不受影响。
技能内的路径集中写在开头的 `ROOT` 一行，其余命令统一写作 `$ROOT/...`，换位置只改这一行。

## 3. 日常用法

### 3.1 让 AI 按原文写东西

把本包路径告知 AI，说明「按这个知识库写」。固定流程为：

读 `原文索引.md` 定位章 → 读 `原文/NN-*.md` 取原句 → 写作时标注书内页码 → 跑引用校验。

### 3.2 自己查原文（不用 AI）

检索 `kb/<书名>/原文/` 下的 md。每个文件内嵌 `<!-- pdf=42 book=21 -->` 锚点，
`book` 是书上印刷页码，`pdf` 是 PDF 物理页。

**本书的页码偏移与置信度写在库内 `00-使用说明与版本信息.md` 的「来源登记」里** ——
偏移量绑定这一份 PDF，不要从别处沿用。

原文保留 PDF 的原始换行，跨页处会被页眉和图注打断，所以取长句应先用短关键词检索，
再按锚点读上下文。

### 3.3 交付前校验

```bash
python tools/cite_check.py 交付物.html      # --kb 可省：按包结构自动定位
```

`kb/` 下有多本时会报错并列出候选，此时用 `--kb kb/<库名>/原文` 指定。
报告中出现「无出处」或「含未见片段」即不要交付。

确认校验器本身没坏：

```bash
python tools/cite_check_selftest.py 交付物.html
```

第 0 / 0b / 0c 步通用；第 2 步的植入锚点针对某本特定资料，基底交付物不含这些锚点时
该步跳过，脚本判「检出能力未验证」并以非 0 退出。

## 4. 交付物结构约定

校验器按 class 识别内容，不是通用 HTML 文本提取器。最小可用结构：

```html
<div class="row">
  <span class="pg">p.21</span>
  <div class="name">术语名</div>
  <div class="cell">照抄原文的一句，标注了来源页码</div>
  <div class="cell">这句是编的：所有条目都会在三天内自行消失</div>
</div>
```

`row` 是一行；页码放 `pg`；正文格是 `cell`；名称列是 `name`。
旁注放 `note`（软性，无出处不算错），明说「教材未述」的留白放 `na`（找不到才是对的）。
条目卡用 `sx` / `sxh` / `v`，总览区用 `ov-grid`，辨析区用 `disc-grid`（不锁页码）。
层次带、页脚、分组说明与标题（`layer` / `foot` / `gnote` / `ghead` / `schead`）均为软性。

软性位置允许是编者自己的话；其余位置要求字面有原文支撑。
例外：自称「教材原文」或标了 `p.xx` 的文字，无论位于何处一律严格校验。

普通 HTML（`<h1>`/`<p>` 不带这些 class）会被抽出 0 个单元，校验器**报错退出**，
不会给出一份假的「通过」报告。

## 5. 报告判读

| 分类 | 判定 | 含义 |
|---|---|---|
| 必须处理 | ✗ 无出处 | 整句在所引原文中找不到，且未声明为归纳 |
| 必须处理 | ✗ 含未见片段 | 句子有原文作底，但夹入了原文没有的内容 |
| 必须处理 | ⚠ 异地 | 内容出自原文，页码标错；修正页码即可 |
| 人工确认 | ⚠ 改写·接近 / 改写·部分 | 措辞有出入；后者重合度偏低，可能是转述也可能混写 |
| 通过 | ✓ 原文 / ◇ 重组 / ◇ 声明留白 / ◇ 归纳 | 字面命中、信息元重排、声明的留白、声明为归纳 |

「需处理 N 条」统计的是前三类。另加 `--verbose` 展开通过项明细，
`--json 结果.json` 另存机器可读结果。

## 6. 改过东西之后：一条命令对齐三处副本

同一份知识库有三个副本：工程目录下的工作副本、本 zip、落点主副本。它们会各自漂移，
所以改过 `kb/` 或 `tools/` 之后跑一次：

```bash
python tools/make_portable.py --kb <工作副本目录> --deploy D:/kb
```

它把内容就地覆盖写入落点，并把 zip 一并复制过去。只覆盖同名文件、不删除任何目录 ——
zip 本身即 `kb` + `tools` 的完整快照，覆盖即达一致，也不会误伤落点里后放的东西。

## 7. 换电脑 / 入库新书

- **工具链不绑定学科**：新书放到 `kb/<书名>/原文/`，与本书平级即可，不必改脚本或配置
- **页码偏移绑定具体 PDF**：换印次或换 PDF 后整套溯源作废，必须重跑 `probe` 重新判定，
  不要沿用任何现成数字。本书的偏移值见库内 `00-使用说明与版本信息.md`
- 包内不含原 PDF（体积与版权），重新入库时从原存放处另拷
- 入库命令为 `probe` / `extract` / `kb_index` 三步，四项诊断的判读范围与停手条件见
  《新书入库核对清单》（本落点下 `..\教材知识库工具包\新书入库核对清单.md`）
- **PDF 没有内置书签时**，`probe` 会提示、`extract` 会退回「每 25 页机械切块」——
  那样切出来的文件无法按章溯源。做法是先做一份章节清单，再交 `--chapters` 抽取：

  ```text
  # kb/<新书名>/_章节目录.txt，每行「起始书内页<TAB>标题」，# 开头为注释
  1	第一章 绪论
  10	第二章 XXX
  330	@后置材料（推荐阅读与索引）      ← @ 开头 = 正文后的独立片段，文件名前缀 99
  ```

  ```bash
  python tools/pdf2kb.py probe   新书.pdf --chapters kb/<新书名>/_章节目录.txt
  python tools/pdf2kb.py extract 新书.pdf --out kb/<新书名>/原文 --tidy \
         --chapters kb/<新书名>/_章节目录.txt
  ```

  清单从印刷目录页抄（目录页上「第X章 / 章名 / 起始页码」通常三行一组），
  抄完对照目录页与各章首页核两遍。清单进库留档，它同时是切章依据。
- 书写约定（别名用 `/`、并列项用顿号、加粗词后不换行等）记在工程目录的
  `.workbuddy/memory/` 中，不在包内；希望新机器上的 AI 也遵守，需同时复制这几个 md

## 8. 环境要求

| 脚本 | 依赖 | 说明 |
|---|---|---|
| `cite_check.py` | 仅 Python 标准库（≥3.9） | 引用校验，日常必跑 |
| `cite_check_selftest.py` | 仅标准库 | 校验器自检 |
| `kb_index.py` | 仅标准库 | 重建 `原文索引.md` |
| `pdf2kb.py` | `pymupdf` | 仅新书入库时需要 |
| `html2pdf.py` | 本机 Edge 或 Chrome | 仅导出 PDF 时需要 |

查原文、写作、跑校验这条主线只需 Python 本体。

## 9. 已知限制

- 只比对字面，不做语义判断
- 不含 OCR，扫描版 PDF 不可用
- 表格被压平成普通文本行；插图本身不抽取，只留图注
- 资料存在整页插图页时，跨页条目会绕过该页续排，标注页码前须确认该页有无正文
- 不传 `--chapter` 时加载整库：跨章交付物不必逐章切换，但别章的巧合字面串
  可能把本页已有支撑的内容判成「异地」，可疑时用 `--chapter` 复跑单章模式
- PDF 无内置书签时，章边界来自人工整理的章节清单；清单是切章依据，
  抄错一处即让那一章的页码区间整体错位

---

本文件是便携包专用说明（与分发包的 `dist-assets/README-先读我.md` 是两份不同文档），
由 `tools/make_portable.py` 顶部的 `README` 常量原样写入包内。
要改内容请改脚本里的常量后重新打包；直接改包内这份会被下次打包覆盖。
"""


def deploy(zip_path: str, dest: str) -> int:
    """把 zip 内容就地覆盖写入落点目录（并同步一份 zip），返回覆盖的文件数。

    刻意**只覆盖、不删除**：
    1. zip 是 kb + tools 的完整快照，覆盖即可达到一致；
    2. 落点目录里可能有用户后放的东西，删除式同步会误伤；
    3. Windows 上对落点目录做递归删除常被安全机制拦截（fail-closed），删不掉反而中断流程。
    """
    import shutil

    os.makedirs(dest, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            target = os.path.join(dest, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1

    # zip 本身也放一份到落点；但若 --out 已经直接指向落点目录，跳过同文件复制
    # （shutil.copy2 对同一文件会抛 SameFileError，把整条同步流程打断）
    src_z = os.path.abspath(zip_path)
    dst_z = os.path.abspath(os.path.join(dest, os.path.basename(zip_path)))
    if src_z != dst_z:
        shutil.copy2(src_z, dst_z)
    return n


# 分发模式的素材目录（放在 tools/ 下，随工具一起走）
DIST_ASSETS_DIR = "dist-assets"


# ── 分发前脱敏 ────────────────────────────────────────────────
# 分发包要公开出去，本机绝对路径（带用户名）不能跟着走 —— 曾漏过一次：
# 一个临时脚本里带着微信文件路径，被一并收进了包。规则刻意保守，
# 只替换「<盘符>:/Users/<用户名>/…」这种真正的用户目录路径，不碰作为
# 示例出现的 D:/kb 之类；非文本后缀一律原样通过。
USERPATH_RE = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/][^\\/\s\"'`<>]+")
TEXT_EXT = {".md", ".py", ".txt", ".json", ".html", ".cfg", ".toml", ".yml", ".yaml"}


def sanitize_bytes(data: bytes, arcname: str) -> tuple[bytes, int]:
    """把文本内容里的本机用户目录路径换成占位符，返回 (新内容, 替换处数)。

    非文本或解码失败的原样返回 —— 脱敏绝不把文件改坏。
    """
    if os.path.splitext(arcname)[1].lower() not in TEXT_EXT:
        return data, 0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    new, n = USERPATH_RE.subn("<用户目录>", text)
    return (new.encode("utf-8"), n) if n else (data, 0)


def build_dist(args, tools: str, skills: list[str]) -> int:
    """产出「只含工具链」的分发包：不带任何教材原文。

    与便携包（默认模式）的区别：

    - **不含 kb/ 的正文**，只放一份「放知识库在这里」的说明；
      收到的人拿自己的 PDF 入库到 `kb/<书名>/原文/`
    - 额外带**面向第一次接触的人**的 README、《新书入库核对清单》、`.gitignore`
    - **强制带技能** —— 分发的卖点就是「装一次技能，AI 就懂流程」

    为什么单独做这个包：教材正文受版权保护，含原文的包不能公开分发。
    分发形态必须是「工具 + 空库」，而不是「工具 + 我这本书的原文」。
    """
    assets = os.path.join(tools, DIST_ASSETS_DIR)
    need = ["README-先读我.md", "新书入库核对清单.md", "_gitignore",
            "kb占位说明.md", "LICENSE"]
    missing = [f for f in need if not os.path.isfile(os.path.join(assets, f))]
    if missing:
        raise SystemExit(f"分发素材缺失：{'、'.join(missing)}\n  应位于：{assets}")

    if not skills:
        raise SystemExit(
            "分发模式必须带技能（skills/textbook-kb-cite），否则收到的人不知道该怎么用。\n"
            f"  默认技能位置：{DEFAULT_SKILL}\n"
            "  用 --skills <技能目录> 指定，或先建好该技能。")

    bundle = args.name or "教材知识库工具包"
    out = args.out or os.path.join(os.path.dirname(tools),
                                   f"{bundle}-{time.strftime('%Y%m%d')}.zip")

    files: list[tuple[str, str]] = []
    files += collect(tools, f"{bundle}/tools", {DIST_ASSETS_DIR})
    # 自带示例：让第一次接触的人不必先入库一本书，就能跑通一次校验看到结果。
    # 排除跑出来的报告，那是产物，不该跟着分发。
    demo_dir = os.path.join(assets, "demo")
    if os.path.isdir(demo_dir):
        files += [p for p in collect(demo_dir, f"{bundle}/demo", set())
                  if not os.path.basename(p[0]).endswith("-引用校验报告.md")]
    for sk in skills:
        files += collect(sk, f"{bundle}/skills/{os.path.basename(sk.rstrip(chr(92) + '/'))}", set())

    leaks: dict[str, int] = {}

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:

        def add(arc: str, src_path: str) -> None:
            """写入一个文件；文本先过脱敏，替换处数记进 leaks。"""
            with open(src_path, "rb") as f:
                data = f.read()
            data, n = sanitize_bytes(data, arc)
            if n:
                leaks[arc] = leaks.get(arc, 0) + n
            z.writestr(arc, data)

        # 交给别人的首页用标准名 README.md —— 解压后直接当仓库根目录时，
        # GitHub 才会自动渲染成首页（便携包是自用，仍叫 README-先读我.md）
        add(f"{bundle}/README.md", os.path.join(assets, "README-先读我.md"))
        for name in ("新书入库核对清单.md", "LICENSE"):
            add(f"{bundle}/{name}", os.path.join(assets, name))
        # 源文件名是 _gitignore：叫 .gitignore 的话，本仓库自己就会把它忽略掉
        add(f"{bundle}/.gitignore", os.path.join(assets, "_gitignore"))
        add(f"{bundle}/kb/放知识库在这里.md", os.path.join(assets, "kb占位说明.md"))
        for full, arc in files:
            add(arc, full)

    size = os.path.getsize(out)
    raw = sum(os.path.getsize(f) for f, _ in files) + sum(
        os.path.getsize(os.path.join(assets, n)) for n in need)
    print(f"✓ 分发工具链包：{out}")
    print(f"  顶层目录：{bundle}/")
    print(f"  收录 {len(files) + len(need)} 个文件，原始 {raw/1024:.0f}KB → 压缩后 {size/1024:.0f}KB")
    print("  · 不含任何教材原文（kb/ 下只有一份「放知识库在这里」的说明）")
    print(f"  已带技能：{'、'.join(os.path.basename(s) for s in skills)}")
    if leaks:
        print(f"  · 脱敏：{len(leaks)} 个文件中的本机用户目录路径已换成占位符"
              f"（共 {sum(leaks.values())} 处）")
        for arc, n in sorted(leaks.items()):
            print(f"      {arc} ×{n}")
    else:
        print("  · 脱敏：未发现本机用户目录路径")
    print(f"  交付前自查：解压到临时目录跑一次 `python tools/cite_check_selftest.py <交付物.html>`，")
    print("              第 0 / 0b / 0c 步都打 ✓ ，且植入错误全检出，才算包是完整的。")
    return 0


def resolve_skills(args) -> list[str]:
    """确定要一并打包的技能目录。默认自动带上配套技能。"""
    if args.skills is None:
        return [DEFAULT_SKILL] if os.path.isdir(DEFAULT_SKILL) else []
    skills = [os.path.abspath(p) for p in args.skills]
    missing = [p for p in skills if not os.path.isdir(p)]
    if missing:
        print(f"⚠ 技能目录不存在，已跳过：{', '.join(missing)}", file=sys.stderr)
    return [p for p in skills if os.path.isdir(p)]


def main() -> int:
    ap = argparse.ArgumentParser(description="打包知识库为可迁移便携包 / 分发工具链包")
    ap.add_argument("--kb", default="",
                    help="知识库目录（如 .workbuddy/kb/<书名>）；--dist 模式下不需要")
    ap.add_argument("--dist", action="store_true",
                    help="产出【只含工具链、不含教材原文】的分发包，用于发给别人或上传公开仓库")
    ap.add_argument("--out", default="", help="输出 zip 路径（默认放在知识库同级目录）")
    ap.add_argument("--tools", default="", help="工具目录（默认为知识库的上上级下的 tools/）")
    ap.add_argument("--keep-pages", action="store_true", help="连 _pages/ 页面图片一起打包")
    ap.add_argument("--skills", nargs="*", default=None,
                    help="一并打包的技能目录（默认自动带上 textbook-kb-cite；传空则不打包）")
    ap.add_argument("--deploy", default="",
                    help="打包后把内容【就地覆盖】写入该落点目录（如 D:/kb），并同步一份 zip 过去。"
                         "只覆盖同名文件，不删除任何目录 —— 因为 zip 是 kb+tools 的完整快照")
    ap.add_argument("--name", default="",
                    help="zip 名与包内顶层目录名（默认：便携包「<库名>-便携包」"
                         "/ 分发包「教材知识库工具包」）")
    args = ap.parse_args()

    here_tools = os.path.dirname(os.path.abspath(__file__))
    skills = resolve_skills(args)

    if args.dist:
        return build_dist(args, here_tools, skills)

    if not args.kb:
        raise SystemExit("便携包模式需要 --kb <知识库目录>；"
                         "只想发不含原文的工具链，请改用 --dist")

    kb = os.path.abspath(args.kb)
    if not os.path.isdir(kb):
        raise SystemExit(f"知识库目录不存在：{kb}")
    kbname = os.path.basename(kb.rstrip("\\/"))

    tools = os.path.abspath(args.tools) if args.tools else \
        os.path.join(os.path.dirname(os.path.dirname(kb)), "tools")
    if not os.path.isdir(tools):
        print(f"⚠ 未找到工具目录，将只打包知识库：{tools}", file=sys.stderr)
        tools = ""

    bundle = args.name or f"{kbname}-便携包"
    out = args.out or os.path.join(
        os.path.dirname(kb), f"{bundle}-{time.strftime('%Y%m%d')}.zip")

    skip = set() if args.keep_pages else {"_pages"}

    files: list[tuple[str, str]] = []
    files += collect(kb, f"{bundle}/kb/{kbname}", skip)
    if tools:
        files += collect(tools, f"{bundle}/tools", {"dist-assets"})
    for sk in skills:
        files += collect(sk, f"{bundle}/skills/{os.path.basename(sk.rstrip(chr(92) + '/'))}", set())

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(f"{bundle}/README-先读我.md", README)
        for full, arc in files:
            z.write(full, arc)

    size = os.path.getsize(out)
    raw = sum(os.path.getsize(f) for f, _ in files)
    print(f"✓ 便携包：{out}")
    print(f"  顶层目录：{bundle}/")
    print(f"  收录 {len(files) + 1} 个文件，原始 {raw/1024/1024:.1f}MB → 压缩后 {size/1024/1024:.1f}MB")
    if skip:
        print("  已排除 _pages/（需要页面图片请加 --keep-pages）")
    if not tools:
        print("  ⚠ 未包含 tools/，校验脚本不在包内")
    if skills:
        print(f"  已带技能：{', '.join(os.path.basename(s) for s in skills)}"
              f"（新电脑上复制到 ~/.workbuddy/skills/）")
    else:
        print("  · 未带技能（--skills 可指定；不装技能也不影响脚本使用）")

    if args.deploy:
        n = deploy(out, args.deploy)
        print(f"✓ 已就地同步到落点：{os.path.abspath(args.deploy)}（覆盖 {n} 个文件，未删除任何目录）")
        print("  三处副本（工作副本 / zip / 落点）现已一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
