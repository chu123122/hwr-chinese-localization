"""glossary.py — SPEC §5: 术语表 TSV 读写与一致性检查

术语表 db/glossary.tsv：\\t 分隔，列 term_zh|term_en|category|locked|note，
首行为表头；空行与以 # 开头的注释行跳过；term_zh / term_en 任一为空的行
跳过（stderr 告警）。

--check: 对 units 中 zh 非空的条目，检测英文术语 term_en 出现在 old
  而对应中文术语 term_zh 未出现在 zh → 输出 warn 列表（术语替换漏用，
  大小写不敏感匹配）。退出码：0 = 无告警，2 = 有告警。
--dump: 打印术语表全部条目（含表头，tab 分隔原样输出）。

Usage:
    python scripts/glossary.py --check --units db/units.jsonl --glossary db/glossary.tsv
    python scripts/glossary.py --dump --glossary db/glossary.tsv

Exit codes: 0 = pass, 1 = error, 2 = warnings found (--check only).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.units import load_units  # noqa: E402


def _force_utf8() -> None:
    """报告含中文（zh/term_zh 片段），统一以 UTF-8 输出避免编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_glossary(path: str) -> List[Dict[str, str]]:
    """Read the glossary TSV (SPEC §5): tab-separated, header row first.

    Returns one dict per term: term_zh / term_en / category / locked / note.
    Rows missing either term column are skipped with a stderr warning.
    """
    entries: List[Dict[str, str]] = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            cells = line.split("\t")
            if cells[0].strip() == "term_zh":  # 表头行
                continue
            term_zh = cells[0].strip()
            term_en = cells[1].strip() if len(cells) > 1 else ""
            if not term_zh or not term_en:
                print("warning: %s:%d: missing term_zh or term_en, row skipped"
                      % (path, lineno), file=sys.stderr)
                continue
            entries.append({
                "term_zh": term_zh,
                "term_en": term_en,
                "category": cells[2].strip() if len(cells) > 2 else "",
                "locked": cells[3].strip() if len(cells) > 3 else "",
                "note": cells[4].strip() if len(cells) > 4 else "",
            })
    return entries


def run_check(units, glossary: List[Dict[str, str]]) -> tuple:
    """Warn when term_en appears in old but term_zh is missing from zh.

    Only units with non-empty zh are inspected.  Matching is
    case-insensitive substring.  Returns (checked, warnings).
    """
    checked = 0
    warns = 0
    for u in units:
        if not u.zh:
            continue
        checked += 1
        old_l = u.old.lower()
        zh_l = u.zh.lower()
        for g in glossary:
            if g["term_en"].lower() in old_l and g["term_zh"].lower() not in zh_l:
                warns += 1
                ident = u.block_id if u.kind == "dialogue" \
                    else "%s | %s" % (u.file, u.old)
                print("[warn] %s %s: old contains \"%s\" but zh lacks \"%s\" "
                      "(zh: \"%s\")"
                      % (u.file, _truncate(ident), g["term_en"], g["term_zh"],
                         _truncate(u.zh, 40)))
    return checked, warns


def _truncate(s: str, limit: int = 80) -> str:
    s = (s or "").replace("\n", "\\n")
    return s if len(s) <= limit else s[:limit] + "..."


def run_dump(glossary: List[Dict[str, str]]) -> None:
    print("term_zh\tterm_en\tcategory\tlocked\tnote")
    for g in glossary:
        print("%s\t%s\t%s\t%s\t%s"
              % (g["term_zh"], g["term_en"], g["category"], g["locked"], g["note"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Glossary TSV utilities: consistency check and dump (SPEC §5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--glossary", default="db/glossary.tsv", metavar="PATH",
                    help="glossary TSV (default: db/glossary.tsv)")
    ap.add_argument("--units", metavar="PATH",
                    help="db/units.jsonl (required for --check)")
    ap.add_argument("--check", action="store_true",
                    help="warn when old contains term_en but zh lacks term_zh")
    ap.add_argument("--dump", action="store_true", help="print the glossary")
    args = ap.parse_args(argv)
    _force_utf8()

    if not os.path.exists(args.glossary):
        print("error: glossary file not found: %s" % args.glossary,
              file=sys.stderr)
        return 1
    glossary = load_glossary(args.glossary)
    print("glossary: %d terms from %s" % (len(glossary), args.glossary))

    if args.dump:
        run_dump(glossary)
        return 0
    if args.check:
        if not args.units:
            print("error: --check requires --units", file=sys.stderr)
            return 1
        units = load_units(args.units)
        checked, warns = run_check(units, glossary)
        print("checked %d translated entries: %d glossary warnings"
              % (checked, warns))
        return 2 if warns else 0
    print("error: nothing to do (use --check or --dump)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
