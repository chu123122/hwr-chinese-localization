# -*- coding: utf-8 -*-
"""
导出审校批次：从 units.jsonl 提取子集为 TSV，供外部模型（GPT/Gemini）手动精修。
配合 import_review.py 形成"手动桥接交叉"闭环。

用法:
    python scripts/export_review.py --units db/units.jsonl --out review_batch.tsv \\
        [--file script.rpy] [--limit 100] [--src-lang ru|en] [--status new]
    python scripts/export_review.py --units db/units.jsonl --out review_batch.tsv --limit 50

输出 TSV 列：seq \t file \t kind \t old \t zh \t 修订zh
（修订zh 列留空，供外部模型填写；同时输出 review_batch.prompt.txt 提示词模板，
内含术语表与硬约束，复制给外部模型时附带。）
"""
import argparse
import json
import os
import sys

def _force_utf8():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def load_units(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--units", required=True, help="db/units.jsonl")
    ap.add_argument("--out", required=True, help="输出 TSV 路径")
    ap.add_argument("--file", default=None, help="只导出该文件（如 script.rpy）")
    ap.add_argument("--limit", type=int, default=100, help="最大条数（默认 100）")
    ap.add_argument("--src-lang", default=None, choices=["en", "ru"],
                    help="只导出该源语言")
    ap.add_argument("--status", default=None,
                    help="只导出该 status（如 ok/new）")
    ap.add_argument("--glossary", default="db/glossary.tsv",
                    help="术语表路径（用于生成 prompt 模板）")
    args = ap.parse_args(argv)
    _force_utf8()

    units = load_units(args.units)
    sel = units
    if args.file:
        sel = [u for u in sel if u["file"] == args.file]
    if args.src_lang:
        sel = [u for u in sel if u.get("src_lang") == args.src_lang]
    if args.status:
        sel = [u for u in sel if u.get("status") == args.status]
    # 优先未译（zh 空），其次已译
    sel.sort(key=lambda u: (1 if u.get("zh") else 0, u.get("seq", 0)))
    sel = sel[: args.limit]

    if not sel:
        print("error: 无匹配条目", file=sys.stderr)
        return 1

    tsv_lines = ["seq\tfile\tkind\told\tzh\t修订zh"]
    for u in sel:
        old = u["old"].replace("\t", "\\t").replace("\n", "\\n")
        zh = (u.get("zh") or "").replace("\t", "\\t").replace("\n", "\\n")
        tsv_lines.append("%d\t%s\t%s\t%s\t%s\t" %
                         (u["seq"], u["file"], u["kind"], old, zh))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(tsv_lines) + "\n")

    # prompt 模板
    glossary = ""
    if os.path.exists(args.glossary):
        with open(args.glossary, encoding="utf-8") as g:
            glossary = g.read()
    prompt = (
        "你是中世纪战争题材游戏《人头落地：重铸》的汉化审校。\n"
        "任务：对下面的 TSV 表格逐行精修译文（修订zh列）。\n"
        "硬约束：\n"
        "1) 方括号 [xxx] 插值原样保留，一个字都不能改\n"
        "2) {vspace=5} {color=#...} {font=...} {cps=50} 等文本标签保留\n"
        "3) * 开头的效果行数量必须与原文一致（内容可译）\n"
        "4) 术语表（term_zh 锁定译名，除非注明显著变体否则必须使用）：\n"
        "====术语表开始====\n%s====术语表结束====\n"
        "5) 风格：中世纪严肃语境，拒绝网络口语/机翻腔；角色对话符合人物身份\n"
        "6) 只输出 TSV（保持 seq/old 列原样，只改修订zh列），不要解释\n"
    ) % glossary
    with open(args.out.replace(".tsv", ".prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    print("exported %d units -> %s" % (len(sel), args.out))
    print("prompt 模板 -> %s（复制给外部模型时附上）"
          % args.out.replace(".tsv", ".prompt.txt"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
