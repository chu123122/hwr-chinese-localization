"""import_official.py — SPEC §5: 官方 tl/chinese → units.jsonl 的 official_zh 导入

解析官方骨架目录（游戏 game/tl/chinese，权威样例 = 骨架格式，SPEC §4）的
全部 *.rpy 为 units 列表，按唯一键与 db/units.jsonl 条目对齐（SPEC §3）：

  * 唯一键：dialogue → block_id；string → (file, old)。
  * 匹配条目：official_zh = 官方 new 值（unescape 后）；官方未译
    （new 为空或 == old）→ official_zh 清空为空串（SPEC §3: 空串 = 官方未译）。
  * 官方有而 units 无的条目 → 追加：official_zh 填官方 new（官方已译时），
    zh 留空，status="new"；old 为空的纯语句块（nvl clear 等，无文本可译）
    不追加。追加条目的 seq 由 save_units 自动分配。
  * units 有条目而官方没有（手工新增/旧版遗留）→ 不动（不猜、不删）。

--write 才落盘，否则 dry-run 只打印统计：
官方条目数 / 匹配 / 新增 / 官方缺译 / 新填充 official_zh 数。

Usage:
    python scripts/import_official.py --official "<game>\\game\\tl\\chinese" \\
        --units db/units.jsonl [--write]

Exit codes: 0 = success, 1 = error (official dir missing).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.units import Unit, load_units, parse_dir, save_units  # noqa: E402


def _force_utf8() -> None:
    """报告含中文（zh/official_zh 片段），统一以 UTF-8 输出避免编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Import official Chinese translations into units.jsonl (SPEC §5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--official", required=True, metavar="DIR",
                    help="official tl/chinese directory, e.g. <game>/game/tl/chinese")
    ap.add_argument("--units", required=True, metavar="PATH",
                    help="db/units.jsonl (missing file = empty db)")
    ap.add_argument("--write", action="store_true",
                    help="persist results to --units (default: dry-run, stats only)")
    ap.add_argument("--no-add", action="store_true",
                    help="only fill official_zh on matching units; do not append "
                         "entries that exist in official but not in units "
                         "(default: append them as new units)")
    args = ap.parse_args(argv)
    _force_utf8()

    if not os.path.isdir(args.official):
        print("error: official tl directory not found: %s" % args.official,
              file=sys.stderr)
        return 1

    official = parse_dir(args.official)
    units = load_units(args.units)
    n_db = len(units)

    by_key = {}
    for u in units:
        by_key.setdefault(u.key(), u)  # 重复键取第一个（库文件应已 dedupe）

    matched = 0
    filled = 0
    added = 0
    for off in official:
        u = by_key.get(off.key())
        if u is not None:
            matched += 1
            if off.zh:  # 官方已译（new != old 且非空；parse 已 unescape）
                if not u.official_zh:
                    filled += 1
                u.official_zh = off.zh
            else:
                u.official_zh = ""  # 官方未译 → 锚点清空
            continue
        if args.no_add:
            continue  # --no-add：官方残留条目（当前基座已不存在）不追加
        if not off.old:
            continue  # 纯语句块（无文本可译），不追加
        added += 1
        units.append(Unit(
            kind=off.kind, file=off.file, block_id=off.block_id,
            label=off.label, old=off.old, zh="", official_zh=off.zh,
            src_lang=off.src_lang, status="new", seq=-1,
            occurrences=off.occurrences,
            src_line=off.src_line, body_lines=list(off.body_lines),
        ))

    missing = sum(1 for off in official if off.old and not off.zh)
    n_dlg = sum(1 for off in official if off.kind == "dialogue")

    print("official: %d units (%d dialogue, %d string) from %s"
          % (len(official), n_dlg, len(official) - n_dlg, args.official))
    print("units: %d loaded from %s" % (n_db, args.units))
    print("matched: %d | new entries added: %d | official untranslated: %d | "
          "official_zh newly filled: %d"
          % (matched, added, missing, filled))
    if args.write:
        n = save_units(args.units, units)
        print("wrote %d units to %s" % (n, args.units))
    else:
        print("dry-run: no changes written (use --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
