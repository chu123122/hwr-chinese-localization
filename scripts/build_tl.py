"""build_tl.py — SPEC §5: units.jsonl + skeleton -> build/tl/chinese/*.rpy

Reconstructs the tl/chinese output directory from the skeleton (version
bound, the authoritative block layout) and the translation database
(version free).  For each skeleton file the output has the same file set,
same block order, and the same source-position comments.

Usage:
    python scripts/build_tl.py --units db/units.jsonl \\
        --skeleton skeleton/v2.99e/tl/chinese --out build/tl/chinese [--force]

Rules (SPEC §5):
  * dialogue blocks:
        [# game/<file>:<line>]        if the skeleton has the comment
        translate chinese <block_id>:
                                      (blank line)
        <body lines, e.g. "nvl clear">  kept from the skeleton
        new "<escaped zh>"            zh if non-empty, else the skeleton's
                                      old text (identity, "保持骨架原样")
  * strings blocks: one `translate chinese strings:` block per file (the
    skeleton's multiple blocks are merged, per SPEC §4.2), with
    old/new pairs in skeleton order; new = zh if non-empty, else old.
  * python/style blocks are not rebuilt (they belong to the base scripts).
  * the file-header `# TODO: Translation updated at ...` comment is kept
    when the skeleton has one (SPEC §5 "保留 TODO 标记注释").
  * a db entry that does not exist in the skeleton is ignored; a skeleton
    block without a db entry falls back to writing its old text.
  * existing output files are only overwritten with --force.

Exit codes: 0 = success, 1 = error (bad skeleton dir / output exists
without --force).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.units import escape_str, load_units, parse_tl_file  # noqa: E402


def read_todo_header(path: str) -> list:
    """Return the leading `# TODO: ...` comment lines of a skeleton file.

    Only comment lines before the first translate block are considered;
    returns [] when the file has none (SPEC §4.1: optional).
    """
    with open(path, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    header = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("# TODO"):
            header.append(s)
        else:
            break
    return header


def build_file(skel_path: str, out_path: str, by_block: dict, by_str: dict) -> dict:
    """Rebuild one skeleton file into *out_path*.

    *by_block* maps dialogue block_id -> Unit; *by_str* maps
    (file, old) -> Unit (string entries).  Returns a stats dict with
    counts: dialogue blocks, string pairs, db entries used, blocks that
    fell back to the skeleton old text.
    """
    stats = {"dialogue": 0, "strings": 0, "used": 0, "fallback": 0}
    header = read_todo_header(skel_path)
    skel = parse_tl_file(skel_path)

    out = []
    if header:
        out.extend(header)
        out.append("")

    strings_emitted = False
    for u in skel:
        if u.kind == "dialogue":
            db = by_block.get(u.block_id)
            if db and db.zh:
                text = db.zh
                stats["used"] += 1
            else:
                text = u.old
                stats["fallback"] += 1
            if u.src_line:
                out.append(u.src_line)
            out.append("translate chinese %s:" % u.block_id)
            out.append("")
            for bl in u.body_lines:
                out.append("    " + bl)
            out.append('    "%s"' % escape_str(text))
            out.append("")
            stats["dialogue"] += 1
        else:
            if not strings_emitted:
                strings_emitted = True
                out.append("translate chinese strings:")
                out.append("")
                for su in skel:
                    if su.kind != "string":
                        continue
                    db = by_str.get((su.file, su.old))
                    if db and db.zh:
                        text = db.zh
                        stats["used"] += 1
                    else:
                        text = su.old
                        stats["fallback"] += 1
                    out.append('    old "%s"' % escape_str(su.old))
                    out.append('    new "%s"' % escape_str(text))
                    stats["strings"] += 1
                out.append("")

    if out:
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(out))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Build tl/chinese output from units.jsonl + skeleton (SPEC §5).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--units", required=True, metavar="PATH",
                    help="db/units.jsonl (missing file = empty db, identity build)")
    ap.add_argument("--skeleton", required=True, metavar="DIR",
                    help="skeleton tl/chinese directory, e.g. skeleton/v2.99e/tl/chinese")
    ap.add_argument("--out", required=True, metavar="DIR",
                    help="output tl/chinese directory, e.g. build/tl/chinese")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing output files")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.skeleton):
        print("error: skeleton directory not found: %s" % args.skeleton,
              file=sys.stderr)
        return 1

    units = load_units(args.units) if os.path.exists(args.units) else []
    if not units:
        print("warning: no units loaded from %s; output will be identity "
              "(old text)" % args.units, file=sys.stderr)
    by_block = {u.block_id: u for u in units if u.kind == "dialogue"}
    by_str = {(u.file, u.old): u for u in units if u.kind == "string"}

    files = sorted(n for n in os.listdir(args.skeleton) if n.endswith(".rpy"))
    if not files:
        print("error: no *.rpy files in skeleton directory: %s" % args.skeleton,
              file=sys.stderr)
        return 1

    # pre-flight overwrite check: refuse to clobber anything without --force
    existing = [f for f in files if os.path.exists(os.path.join(args.out, f))]
    if existing and not args.force:
        for f in existing[:10]:
            print("error: output exists (use --force to overwrite): %s"
                  % os.path.join(args.out, f), file=sys.stderr)
        if len(existing) > 10:
            print("error: ... and %d more" % (len(existing) - 10), file=sys.stderr)
        return 1

    totals = {"dialogue": 0, "strings": 0, "used": 0, "fallback": 0}
    for fname in files:
        skel_path = os.path.join(args.skeleton, fname)
        out_path = os.path.join(args.out, fname)
        s = build_file(skel_path, out_path, by_block, by_str)
        for k in totals:
            totals[k] += s[k]

    print("built %d file(s): %d dialogue blocks, %d string pairs, "
          "%d db entries used, %d blocks fell back to old text"
          % (len(files), totals["dialogue"], totals["strings"],
             totals["used"], totals["fallback"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
