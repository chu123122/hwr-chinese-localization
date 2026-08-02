"""diff_skeleton.py — SPEC §5: 新旧骨架目录 diff → units.jsonl 增量

把新版本生成的骨架（skeleton/v2/tl/chinese）与译文库 db/units.jsonl 对齐，
产出增量。dialogue 的 block_id（<label>_<hash>）会随源文本变化而变化，
故对齐分两步：

  1. 唯一键对齐：新骨架条目按唯一键（dialogue → block_id；string →
     (file, old)）匹配 units 条目（SPEC §3）。命中且 old 相同 → 内容未变，
     保持原状；命中但 old 不同（异常：key 相同文本不同）→ 按 changed 处理。
  2. 内容对齐：仍未匹配的 dialogue 按内容 (kind, file, old) 在 units 中找
     唯一候选 → old 文本变化（block_id 随之变化）：更新 block_id / label /
     old / src_lang 及骨架布局（src_line、body_lines），zh 保留但 status 标
     "needs_review"（SPEC §5 "zh 保留但 needs_review"），并在报告中列出
     （人工复核旧译是否仍适用）。候选不唯一（old 重复的块）→ 不配对，
     避免错配，走"新增/消失"路径。
  3. 仍未匹配的新骨架条目 → 作为新条目追加：status="new"，zh 与
     official_zh 留空；old 为空的纯语句块（nvl clear 等）不追加。
  4. units 中未匹配任何新骨架条目的条目 → 新骨架中已消失，从 units 移除
     并在报告中列出。

--old 目录用于校验与报告上下文（规模对照）；对齐以 units 库为准。
--write 才落盘，否则 dry-run 打印统计 + changed / removed 明细。

Usage:
    python scripts/diff_skeleton.py --old skeleton/v1/tl/chinese \\
        --new skeleton/v2/tl/chinese --units db/units.jsonl [--write]

Exit codes: 0 = success, 1 = error (skeleton dir missing).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.units import Unit, load_units, parse_dir, save_units  # noqa: E402


def _force_utf8() -> None:
    """报告含中文（zh 片段），统一以 UTF-8 输出避免编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _truncate(s: str, limit: int = 80) -> str:
    s = (s or "").replace("\n", "\\n")
    return s if len(s) <= limit else s[:limit] + "..."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Diff old/new skeletons against units.jsonl (SPEC §5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--old", required=True, metavar="DIR",
                    help="old skeleton tl/chinese directory, e.g. skeleton/v1/tl/chinese")
    ap.add_argument("--new", required=True, metavar="DIR",
                    help="new skeleton tl/chinese directory, e.g. skeleton/v2/tl/chinese")
    ap.add_argument("--units", required=True, metavar="PATH",
                    help="db/units.jsonl (missing file = empty db)")
    ap.add_argument("--write", action="store_true",
                    help="persist results to --units (default: dry-run)")
    args = ap.parse_args(argv)
    _force_utf8()

    for d in (args.old, args.new):
        if not os.path.isdir(d):
            print("error: skeleton directory not found: %s" % d, file=sys.stderr)
            return 1

    old_units = parse_dir(args.old)  # 报告上下文（规模对照）
    new_units = parse_dir(args.new)
    units = load_units(args.units)

    # --- 1) 唯一键对齐 ---
    by_key = {}
    for i, u in enumerate(units):
        by_key.setdefault(u.key(), []).append(i)
    used = [False] * len(units)
    used_new = [False] * len(new_units)
    changed = []  # (units_idx, new_unit, old_block_id)
    for ni, n in enumerate(new_units):
        for i in by_key.get(n.key(), []):
            used[i] = True
            used_new[ni] = True
            if units[i].old != n.old:
                changed.append((i, n, units[i].block_id))  # key 未变但文本变

    # --- 2) 内容对齐（dialogue；string 的唯一键即 (file, old)，无需兜底）---
    by_content = {}
    for i, u in enumerate(units):
        if not used[i]:
            by_content.setdefault((u.kind, u.file, u.old), []).append(i)
    for ni, n in enumerate(new_units):
        if used_new[ni]:
            continue
        if n.kind != "dialogue" or not n.old:
            continue
        cands = [i for i in by_content.get((n.kind, n.file, n.old), [])
                 if not used[i]]
        if len(cands) == 1:  # 唯一候选才配对，避免错配
            used[cands[0]] = True
            used_new[ni] = True
            changed.append((cands[0], n, units[cands[0]].block_id))

    # --- 3) 应用：changed 更新 / 新增 / 移除 ---
    for i, n, _old_id in changed:
        u = units[i]
        u.block_id = n.block_id
        u.label = n.label
        u.old = n.old
        u.src_lang = n.src_lang
        u.src_line = n.src_line
        u.body_lines = list(n.body_lines)
        u.status = "changed"  # 源文本已变化（SPEC §5）；zh 保留，报告中提示待复核

    added = []
    for ni, n in enumerate(new_units):
        if used_new[ni]:
            continue
        if not n.old:
            continue
        added.append(Unit(
            kind=n.kind, file=n.file, block_id=n.block_id, label=n.label,
            old=n.old, zh="", official_zh="", src_lang=n.src_lang,
            status="new", seq=-1, occurrences=n.occurrences,
            src_line=n.src_line, body_lines=list(n.body_lines),
        ))

    removed = [u for i, u in enumerate(units) if not used[i]]

    n_old_dlg = sum(1 for u in old_units if u.kind == "dialogue")
    n_new_dlg = sum(1 for u in new_units if u.kind == "dialogue")
    print("old skeleton: %d units (%d dialogue) | new skeleton: %d units (%d dialogue)"
          % (len(old_units), n_old_dlg, len(new_units), n_new_dlg))
    print("units: %d loaded | matched: %d | changed: %d | added: %d | removed: %d"
          % (len(units), sum(used), len(changed), len(added), len(removed)))

    if changed:
        print()
        print("[needs_review] changed (%d):" % len(changed))
        for i, n, old_id in changed:
            u = units[i]
            ident = "%s -> %s" % (old_id or "(string)", n.block_id or "(string)")
            print("  %s %s (file %s)" % (u.kind, ident, n.file))
            print("    old: %s" % _truncate(n.old))
            print("    zh kept: %s" % _truncate(u.zh))
    if removed:
        print()
        print("[removed] vanished from new skeleton (%d):" % len(removed))
        for u in removed:
            ident = u.block_id if u.kind == "dialogue" else "%s | %s" % (u.file, u.old)
            print("  %s %s" % (u.kind, _truncate(ident)))
            print("    old: %s" % _truncate(u.old))

    if args.write:
        keep = [u for i, u in enumerate(units) if used[i]]
        n = save_units(args.units, keep + added)
        print("wrote %d units to %s" % (n, args.units))
    else:
        print("dry-run: no changes written (use --write to persist)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
