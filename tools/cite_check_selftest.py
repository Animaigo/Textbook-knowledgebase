#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cite_check 自检 —— 用一个"已知有错"的样本来验证校验器真的抓得住。

为什么需要这个
--------------
一个"看什么都报通过"的校验器毫无价值，而且比没有更危险：它会给人虚假的安全感。
所以每次改动 cite_check 之后，都应当跑一遍本脚本。

做法是往真实交付物里植入三类**历史上真犯过的**错，再要求校验器逐条检出：

    植入 A  编造内容   ——  给结节加「直径一般大于 1cm」限定
                           （当年凭常识补的限定，看起来极其合理）
    植入 B  编造英文名 ——  给「感觉异常」补 paresthesia
                           （教材只给了 symptom / sign，其余都没标英文）
    植入 C  页码标错   ——  把丘疹的 p.21 改成 p.23

任一漏报即判定校验器退化（exit 1）。

用法
----
    python cite_check_selftest.py 交付物.html --kb 知识库目录/原文
    python cite_check_selftest.py 交付物.html          # --kb 可省，按包结构自动定位
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cite_check import (check, extract_units, guard_units, kb_candidates,  # noqa: E402
                        load_kb, rel_or_abs, resolve_kb, resolve_kb_files)
# resolve_kb 仅在 check_kb_discovery 里间接用到，保留导入以便该步能单独验定位行为


def check_kb_discovery():
    """第 0b 步：按便携包结构自动定位知识库的能力。

    包结构固定为 `tools/` 与 `kb/` 兄弟并存，所以脚本可凭自身位置推出库的位置。
    这是「解压到任意目录都能用、不必传 --kb」的依据，必须常态化验证。

    用临时目录造假包结构来判定，不依赖真实路径；三件事各自有独立判据：
    - 无 kb/ 时返回空 → 不瞎猜
    - 只有一本时定位到它 → 能成事
    - 有两本时返回两个候选 → 交给上层报错，绝不静默选一个
    """
    with tempfile.TemporaryDirectory() as td:
        tools = os.path.join(td, "tools")
        os.makedirs(tools)

        if kb_candidates(tools) != []:
            return False, "包内没有 kb/ 时，不应给出任何候选"

        os.makedirs(os.path.join(td, "kb", "甲书", "原文"))
        got = kb_candidates(tools)
        if len(got) != 1 or not got[0].endswith(os.path.join("甲书", "原文")):
            return False, f"只有一本时应定位到它，实际得到 {got}"

        os.makedirs(os.path.join(td, "kb", "乙书", "原文"))
        if len(kb_candidates(tools)) != 2:
            return False, "有两本时应返回两个候选（交由上层报错），不得只挑一个"

        os.makedirs(os.path.join(td, "kb", "丙书"))     # 无 原文/ 子目录
        if len(kb_candidates(tools)) != 2:
            return False, "缺 原文/ 子目录的文件夹不应算候选"

    return True, "单本可定位 / 多本不猜 / 空包不瞎猜 / 半成品不算数"


def check_cross_drive():
    """第 0 步：跨盘符时报告仍须能生成。

    交付物留在工程目录（C:）、知识库放在固定落点（D:）是常态。
    此时 os.path.relpath 在 Windows 上抛 ValueError，会把报告生成整个打断
    （2026-09-20 实测踩到，报告写不出来但控制台摘要一切正常，极易漏过）。

    用不存在的盘符做判定是确定性的：ntpath 只比较盘符，不要求路径真实存在。
    """
    try:
        cross = rel_or_abs("Z:/kb/原文", "C:/work")
    except Exception as e:                                    # noqa: BLE001
        return False, f"跨盘符时抛异常：{type(e).__name__}: {e}"
    if not os.path.isabs(cross):
        return False, f"跨盘符未退回绝对路径，得到 {cross!r}"
    same = rel_or_abs("C:/work/交付物.html", "C:/work")
    if os.path.isabs(same):
        return False, f"同盘符下未走相对路径，得到 {same!r}"
    return True, f"跨盘退回绝对路径（{cross}），同盘保持相对（{same}）"


def check_empty_guard():
    """第 0c 步：抽不到单元时必须报错，不能输出「通过」。

    校验器按 class 抽单元（不是通用 HTML 文本提取器），普通 HTML 会抽出 0 个。
    若此时代码照常出报告，结论是「未检出教材未见的内容」、退出码 0 ——
    一个会被信以为真的假绿。
    （2026-09-20 实测：同一句编造，写进普通 HTML 报绿，写进约定结构判「无出处」。）

    用临时构造的两段 HTML 判定，不依赖真实交付物：一段普通 HTML、一段约定结构。
    """
    plain = ('<html><body><h1>笔记</h1>'
             '<p>所有皮损都会在三天内自行消退。</p></body></html>')
    ok_struct = ('<div class="row"><span class="pg">p.21</span>'
                 '<div class="cell">皮肤黏膜的局限性颜色改变</div></div>')

    if extract_units(plain):
        return False, "普通 HTML 本应抽不出单元（该判定的前提不成立）"
    struct_units = extract_units(ok_struct)
    if not struct_units:
        return False, "约定结构的 HTML 应能抽出单元，否则守卫会误拦正常交付物"

    try:
        guard_units(extract_units(plain))
    except SystemExit:
        pass
    else:
        return False, "抽到 0 单元却未拦截 —— 会给出假绿报告"

    try:
        guard_units(struct_units, 3)
    except SystemExit as e:
        return False, f"有单元且有子句时不应拦截，却拦下了：{e}"

    return True, "0 单元 → 拦截；约定结构且子句非空 → 放行"


# 每项：(名称, 原文锚点, 替换文本, 判定函数)
# 判定函数接收 rows，返回 True 表示"检出"
PLANTS = [
    (
        "植入 A · 编造内容（给结节加直径限定）",
        '<div class="cell"><b>实质性、深在性、可触诊</b>的皮损<span class="note">教材未设直径限定</span></div>',
        '<div class="cell"><b>实质性、深在性、可触诊</b>的皮损，直径一般大于 1cm，触之较硬'
        '<span class="note">教材未设直径限定</span></div>',
        lambda rows: any(
            r["verdict"] in ("无出处", "含未见片段") and "直径一般大于" in r["clause"]
            for r in rows
        ),
    ),
    (
        "植入 B · 编造英文名（给感觉异常补 paresthesia）",
        '<div class="sxh"><b>感觉异常</b><span class="pg">p.21</span></div>',
        '<div class="sxh"><b>感觉异常</b><span>paresthesia</span><span class="pg">p.21</span></div>',
        lambda rows: any(
            r["verdict"] in ("无出处", "含未见片段") and "paresthesia" in r["clause"]
            for r in rows
        ),
    ),
    (
        "植入 C · 页码标错（丘疹 p.21 → p.23）",
        '<div class="name"><b>丘　疹</b><span>papule</span><span class="pg">p.21</span></div>',
        '<div class="name"><b>丘　疹</b><span>papule</span><span class="pg">p.23</span></div>',
        lambda rows: any(
            r["verdict"] == "异地" and r["page_label"] == "p.23" for r in rows
        ),
    ),
]


def main():
    ap = argparse.ArgumentParser(description="cite_check 自检")
    ap.add_argument("html", help="用作基底的交付物 HTML（需含上述三处锚点）")
    ap.add_argument("--kb", default="",
                    help="知识库「原文」目录，或某一章的 .md；留空则按包结构自动定位")
    ap.add_argument("--chapter", default="",
                    help="只校验某一章（章号前缀，如 04）；留空则加载整库")
    args = ap.parse_args()

    if not os.path.isfile(args.html):
        raise SystemExit(f"文件不存在：{args.html}")

    files, kb_label = resolve_kb_files(args.kb, args.chapter, quiet=False)
    pages = load_kb(files)
    if not pages:
        raise SystemExit("未能从知识库解析出页锚点")

    src = open(args.html, encoding="utf-8").read()

    # 第 0 步：环境健壮性（跨盘符不能把报告生成打断）
    print("=" * 68)
    print("第 0 步 · 环境健壮性（跨盘符 / 相对路径）")
    print("=" * 68)
    env_ok, env_msg = check_cross_drive()
    print(f"  {'✓' if env_ok else '✗'} {env_msg}")
    print()

    # 第 0b 步：知识库定位（解压到任意目录都能用，全靠这一步）
    print("=" * 68)
    print("第 0b 步 · 知识库定位（按便携包结构自动推断）")
    print("=" * 68)
    disc_ok, disc_msg = check_kb_discovery()
    print(f"  {'✓' if disc_ok else '✗'} {disc_msg}")
    print()

    # 第 0c 步：零单元守卫（抽不到东西时必须停下，不能报「通过」）
    print("=" * 68)
    print("第 0c 步 · 零单元守卫（结构不匹配不得报绿）")
    print("=" * 68)
    empty_ok, empty_msg = check_empty_guard()
    print(f"  {'✓' if empty_ok else '✗'} {empty_msg}")
    print()

    # 先跑一遍干净版，确认基线是"零问题"
    base_rows, base_stats = check(extract_units(src), pages)
    base_red = (base_stats.get("无出处", 0) + base_stats.get("含未见片段", 0)
                + base_stats.get("异地", 0))
    print("=" * 68)
    print("第 1 步 · 基线检查（未植入任何错误）")
    print("=" * 68)
    print(f"  子句 {base_stats.get('_子句总数', 0)} 条，"
          f"需处理 {base_red} 条")
    if base_red:
        print("  ⚠ 基线本身就不干净 —— 先修好交付物，再谈自检。")
        for r in base_rows:
            if r["verdict"] in ("无出处", "含未见片段", "异地"):
                print(f"     [{r['verdict']}] L{r['line']} {r['clause'][:50]}")
    else:
        print("  ✓ 基线干净")
    print()

    # 再逐项植入，逐一验证能否检出
    print("=" * 68)
    print("第 2 步 · 植入已知错误，验证检出能力")
    print("=" * 68)
    ok, skipped = 0, 0
    for name, anchor, repl, detect in PLANTS:
        if anchor not in src:
            print(f"  ⊘ {name}\n      锚点未找到，跳过（交付物结构已变，请更新自检锚点）")
            skipped += 1
            continue
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "planted.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, repl, 1))
            rows, _ = check(extract_units(src.replace(anchor, repl, 1)), pages)
        hit = detect(rows)
        print(f"  {'✓' if hit else '✗'} {name}")
        print(f"      判定：{'已检出' if hit else '**漏报**'}")
        ok += 1 if hit else 0

    print()
    print("=" * 68)
    total = len(PLANTS) - skipped
    env_line = (f"环境健壮性 {'通过' if env_ok else '**未通过**'}"
                f"　｜　库定位 {'通过' if disc_ok else '**未通过**'}"
                f"　｜　零单元守卫 {'通过' if empty_ok else '**未通过**'}")
    if total == 0:
        # 锚点全部未找到 = 本次自检没有真正测试到检出能力。
        # 与「查过都没问题 / 根本没查」是同一类问题：不能因为跑完了就报「正常」。
        print(f"结果：检出能力**未验证**　｜　{env_line}")
        print(f"  {len(PLANTS)} 项植入锚点一个都没找到，未测试校验器的检出能力。")
        print("  第 2 步需要一份含 PLANTS 三处锚点的交付物作基底；换书使用时须按新教材改写锚点。")
        return 1
    print(f"结果：检出 {ok}/{total}" + (f"（跳过 {skipped}）" if skipped else "")
          + f"　｜　{env_line}")
    if ok == total and base_red == 0 and env_ok and disc_ok and empty_ok:
        print("校验器工作正常。")
        return 0
    print("校验器能力异常，请检查 cite_check.py 的判定逻辑。")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
