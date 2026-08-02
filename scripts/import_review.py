# -*- coding: utf-8 -*-
"""
导入审校结果：解析 export_review.py 导出的 TSV（外部模型填写了修订zh列），
patch 回 units.jsonl。只接受修订zh 非空且不等于原 zh 的行。

用法:
    python scripts/import_review.py --in review_batch.tsv --units db/units.jsonl [--write]

校验（不符合则拒绝该行并报告）：
- 插值 [x] 变量集与 old 一致（方括号内容不被翻译）
- 文本标签 {vspace} {color} {font} {cps} 等未被整体丢弃（warn 级别，允许排版微调）
- * 效果行数量一致（error 级别）
"""
import argparse
import json
import re
import sys

def _force_utf8():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def load_units(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]

def _restore(s):
    return s.replace("\\n", "\n").replace("\\t", "\t")

def _brackets(s):
    # [[ 是 Ren'Py 转义（显示字面 [），不是插值；用负向断言排除，
    # 且内容排除 [ 本身（防止 [[你有[player_gold] 整段误匹配）
    return re.findall(r"(?<!\[)\[([^\[\]]+)\]", s)

def check(old, new):
    errs, warns = [], []
    ov = set(_brackets(old))
    nv = set(_brackets(new))
    for v in sorted(ov - nv):
        errs.append("插值 [%s] 丢失" % v)
    for v in sorted(nv - ov):
        errs.append("新增插值 [%s]" % v)
    for c in _brackets(new):
        if any(ord(ch) > 127 for ch in c):
            errs.append("插值内出现中文 [%s]" % c)
    if old.count("*") > new.count("*"):
        errs.append("* 效果行减少（%d -> %d）" % (old.count("*"), new.count("*")))
    for tag in ("{vspace", "{cps", "{color", "{font"):
        if tag in old and tag not in new:
            warns.append("%s 标签丢失" % tag)
    return errs, warns

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="审校 TSV")
    ap.add_argument("--units", required=True, help="db/units.jsonl")
    ap.add_argument("--write", action="store_true",
                    help="写回 units（默认 dry-run）")
    args = ap.parse_args(argv)
    _force_utf8()

    rows = []
    with open(args.inp, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            if not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            rows.append(dict(zip(header, parts)))

    units = load_units(args.units)
    by_seq = {u["seq"]: u for u in units}

    applied = 0
    rejected = 0
    warns = 0
    for r in rows:
        seq = int(r.get("seq", -1))
        rev = r.get("修订zh", "")
        if not rev or rev == r.get("zh", ""):
            continue  # 未修订
        u = by_seq.get(seq)
        if u is None:
            print("WARN: seq %d 不存在，跳过" % seq)
            rejected += 1
            continue
        errs, ws = check(u["old"], rev)
        if errs:
            print("REJECT seq %d (%s): %s" % (seq, "; ".join(errs), rev[:60]))
            rejected += 1
            continue
        u["zh"] = rev
        if u.get("status") in ("ok", "needs_review", "changed"):
            u["status"] = "needs_review"  # 外部修订后留人工复核标记
        else:
            u["status"] = "ok"
        applied += 1
        warns += len(ws)
        for w in ws:
            print("WARN seq %d: %s" % (seq, w))

    print("applied: %d | rejected: %d | tag warns: %d"
          % (applied, rejected, warns))
    if args.write and applied:
        with open(args.units, "w", encoding="utf-8", newline="\n") as f:
            for u in units:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        print("wrote %d units to %s" % (len(units), args.units))
    else:
        print("dry-run: 未写回（加 --write 落盘）")
    return 1 if rejected else 0

if __name__ == "__main__":
    sys.exit(main())
