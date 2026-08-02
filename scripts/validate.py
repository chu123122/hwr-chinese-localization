#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate.py — full validation of a Ren'Py tl/chinese directory (SPEC §5/§7).

Usage:
    python scripts/validate.py --tl <dir> [--units <units.jsonl>]

Checks every dialogue / strings block of every *.rpy file under --tl
(build product or skeleton; both share the SPEC §4 format):

  1. interpolation (error): the [var] sets of `old` and `new` must match;
     a variable lost from old or newly introduced in new is an error, and
     non-ASCII characters inside new's brackets are an error.  Only
     bracket contents that are Ren'Py interpolation variables (ASCII
     identifiers, optionally dotted and/or with a `!transform` suffix)
     take part in the set comparison; literal bracketed text (e.g.
     `[Luck check passed!]`) is ignored.  The `!transform` suffix is
     normalized away so a suffix-only change is not flagged.
  2. tags (error/warn)    : Ren'Py auto-closes tags at the end of a
     displayed line, so an unbalanced paired tag ({b} {i} {u} {s} {a}
     {font=} {color=} {size=} {alpha=} {outline=} {kern=}) in `new` is an
     error only when the base text (`old`) used and explicitly balanced
     that tag kind -- i.e. the translation dropped close tags the base
     used, which changes rendering -> error; a {vspace=} {cps=}
     {color=} {font=} tag present in `old` but entirely absent from
     `new` -> warn
  3. empty translation (error): a dialogue block that carries an old text
     (the `# "..."` comment) but no new string literal, or a strings
     `old "..."` line without a following `new "..."` line
  4. encoding (error)     : file not decodable as UTF-8
  5. structure (error)    : duplicate block_id / duplicate (file, old)
     within one file; mis-indented translate / old / new / literal lines

With --units, additionally checks the units db (jsonl) for entries with
status="ok" but empty zh (SPEC §7.3 on the db side).

Output: per-file problem list + summary (errors / warns / checked units).
Exit code: 0 = all pass, 1 = at least one error, 2 = warnings only.
"""

import argparse
import os
import re
import sys
from collections import Counter

# SPEC §6: all toolchain scripts share lib/units.py via scripts/ on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.units import parse_tl_file, load_units  # noqa: E402

_DIALOGUE_HEADS = ("strings", "python", "style")
_PAIRED_TAGS = ("b", "i", "u", "s", "a", "font", "color", "size",
                "alpha", "outline", "kern")
_DROP_TAGS = ("vspace", "cps", "color", "font")

_TRANS_RE = re.compile(r"^\s*translate\s+chinese\s+(\S+):")
_TAG_RE = re.compile(r"\{/??([A-Za-z_][A-Za-z0-9_]*)(?:=[^{}]*)?\}")
# [[ 是 Ren'Py 转义（显示字面 [），不是插值；用负向断言排除，
# 且内容排除 [ 本身（防止 [[你有[player_gold] 整段误匹配）
_INTERP_RE = re.compile(r"(?<!\[)\[([^\[\]]+)\]")
_ESC_BRACE_RE = re.compile(r"\\([{}])")
_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _disp(s, n=60):
    """Short, newline-flattened display form of a value for messages."""
    if not s:
        return ""
    s = (s.replace("\\", "\\\\").replace("\n", "\\n")
          .replace("\r", "\\r").replace("\t", "\\t"))
    return s if len(s) <= n else s[:n] + "..."


def _clean(value):
    """Remove escaped-brace markers so literal braces are not seen as tags."""
    return _ESC_BRACE_RE.sub("", value or "")


def _tags(value):
    """Yield (name, is_closing) for every real tag in *value*."""
    for m in _TAG_RE.finditer(_clean(value)):
        yield m.group(1), m.group(0)[1] == "/"


def _brackets(value):
    """Raw contents of every [x] expression in *value*."""
    return [m.group(1) for m in _INTERP_RE.finditer(_clean(value))]


def _var_of(content):
    """Normalized variable name if *content* is a Ren'Py interpolation.

    The `!transform` suffix is split off before matching, so a suffix-only
    change (or a corrupted suffix in the base that a translation fixes,
    e.g. `[x!极i]` -> `[x!ti]`) is not flagged.  Returns None for bracket
    content that is not a variable -- literal bracketed text such as
    `[Luck check passed!]` is not an interpolation.
    """
    base = content.split("!")[0]
    return base if _VAR_RE.fullmatch(base) else None


def _context(u):
    """Message prefix identifying one unit."""
    if u.kind == "dialogue":
        ctx = "dialogue [%s]" % (u.block_id or "?")
        if u.src_line:
            ctx += "  (%s)" % u.src_line.strip()
        return ctx
    return "string old \"%s\"" % _disp(u.old, 40)


# ---------------------------------------------------------------------------
# raw line scan: structure (§7.5) + empty translations (§7.3)
# ---------------------------------------------------------------------------

def scan_file(path):
    """Raw line scan for structural and empty-translation issues.

    The parser (lib/units) silently tolerates missing `new` literals and
    merges duplicate block ids, so these must be detected on the raw text.
    Returns a list of (lineno, message) tuples.
    """
    issues = []
    ids = {}  # dialogue block_id -> [lineno, ...]
    in_dlg = False
    dlg_id = ""
    dlg_start = 0
    dlg_have_old = False
    dlg_have_new = False
    in_strings = False
    pend_old = None  # (lineno, value) of an `old "..."` awaiting its `new`

    def flush_dialogue():
        nonlocal in_dlg
        if in_dlg and dlg_have_old and not dlg_have_new:
            issues.append((dlg_start, "dialogue [%s]: old text present but "
                                      "no new string literal "
                                      "(empty translation)" % dlg_id))
        in_dlg = False

    def flush_strings():
        nonlocal pend_old
        if pend_old is not None:
            issues.append((pend_old[0], "string old \"%s\": no new \"...\" "
                                        "line (empty translation)"
                           % _disp(pend_old[1], 40)))
            pend_old = None

    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            if raw.startswith("\ufeff"):
                raw = raw[1:]
            line = raw.rstrip("\r\n")
            s = line.strip()
            if s.startswith("translate "):
                flush_dialogue()
                flush_strings()
                in_strings = False
                if line[0].isspace():
                    issues.append((lineno, "'translate' line is indented"))
                m = _TRANS_RE.match(s)
                if m:
                    ident = m.group(1)
                    head = ident.split()[0]
                    if head == "strings":
                        in_strings = True
                    elif head not in _DIALOGUE_HEADS:
                        in_dlg = True
                        dlg_id = ident
                        dlg_start = lineno
                        dlg_have_old = dlg_have_new = False
                        ids.setdefault(ident, []).append(lineno)
                continue
            if in_dlg:
                if s.startswith("# game/"):
                    flush_dialogue()
                elif s.startswith("#"):
                    if s[1:].lstrip().startswith('"'):
                        dlg_have_old = True
                elif s.startswith('"'):
                    # an empty "" literal does not count as a translation
                    if s != '""':
                        dlg_have_new = True
                    if not line[0].isspace():
                        issues.append((lineno,
                                       "dialogue string literal not indented"))
                continue
            if in_strings:
                if s.startswith("old ") or s.startswith("new "):
                    if not line[0].isspace():
                        issues.append((lineno,
                                       "'%s' line not indented" % s[:3]))
                    if s.startswith("old "):
                        flush_strings()
                        pend_old = (lineno, s[4:].strip().strip('"'))
                    else:
                        pend_old = None
                continue
    flush_dialogue()
    flush_strings()
    for ident, lines in sorted(ids.items()):
        if len(lines) > 1:
            issues.append((lines[0], "duplicate block_id %s (%d occurrences "
                                     "in this file, lines %s)"
                           % (ident, len(lines), ", ".join(map(str, lines)))))
    return issues


# ---------------------------------------------------------------------------
# per-unit value checks: interpolation (§7.1) + tags (§7.2)
# ---------------------------------------------------------------------------

def check_unit(u):
    """Value-level checks for one parsed unit; returns (errors, warns)."""
    errs, warns = [], []
    if u.kind == "string" and u.occurrences > 1:
        errs.append("duplicate (file, old): %d occurrences in this file"
                    % u.occurrences)
    if not u.zh:
        return errs, warns  # untranslated skeleton state: nothing to verify

    old_tags = list(_tags(u.old))
    old_opens = Counter(n for n, c in old_tags if not c)
    old_closes = Counter(n for n, c in old_tags if c)
    new_tags = list(_tags(u.zh))
    new_names = {name for name, _ in new_tags}
    opens = Counter(n for n, c in new_tags if not c and n in _PAIRED_TAGS)
    closes = Counter(n for n, c in new_tags if c and n in _PAIRED_TAGS)
    for name, cnt in opens.items():
        if cnt > closes.get(name, 0):
            # Ren'Py auto-closes tags at the end of a displayed line, so an
            # unbalanced tag is only a translation bug when the base text
            # used and explicitly balanced that tag kind and the new text
            # dropped a close (rendering would change).  A new tag that the
            # base never used is a deliberate whole-line style choice and
            # auto-closes fine.
            if (old_opens.get(name, 0) > 0
                    and old_opens.get(name, 0) <= old_closes.get(name, 0)):
                errs.append("unclosed tag {%s...}: %d open(s) vs %d close(s) "
                            "in new (old text balanced this tag)"
                            % (name, cnt, closes.get(name, 0)))
    for name in _DROP_TAGS:
        if name in old_opens and name not in new_names:
            warns.append("{%s=...} tag present in old but entirely missing "
                         "in new" % name)

    old_vars = {_var_of(c) for c in _brackets(u.old)} - {None}
    new_vars = {_var_of(c) for c in _brackets(u.zh)} - {None}
    bad_nascii = [c for c in _brackets(u.zh)
                  if any(ord(ch) > 127 for ch in c)]
    for c in bad_nascii:
        errs.append("interpolation [%s] in new contains non-ASCII "
                    "characters" % c)
    # A translation that rewrote variables into Chinese already fails via
    # bad_nascii; the derived "old variable missing" reports would be noise.
    if not bad_nascii:
        for v in sorted(old_vars - new_vars):
            errs.append("interpolation [%s] from old is missing in new" % v)
    for v in sorted(new_vars - old_vars):
        errs.append("interpolation [%s] in new not present in old" % v)

    # * 效果行保真：物品描述里 * 开头的行是游戏机制效果（"*Virtue: -15" 等），
    # 官方中文在 {vspace} 处截断导致整段效果行丢失（9 个物品实锤）。
    # 允许翻译效果行内容，但不允许减少数量。
    old_stars = u.old.count("*")
    new_stars = u.zh.count("*")
    if old_stars > new_stars:
        errs.append("* effect lines dropped: old has %d, new has %d "
                    "（效果行不可丢失）" % (old_stars, new_stars))
    return errs, warns


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Validate a Ren'Py tl/chinese directory "
                    "(HWR-Localization SPEC §5/§7).",
        epilog="exit code: 0 = all pass, 1 = errors found, "
               "2 = warnings only")
    ap.add_argument("--tl", required=True, metavar="DIR",
                    help="tl/chinese directory to validate (build product "
                         "or skeleton, same SPEC §4 format)")
    ap.add_argument("--units", metavar="JSONL", default=None,
                    help="optional units db: additionally flag status=ok "
                         "entries with empty zh")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not os.path.isdir(args.tl):
        print("ERROR: --tl directory not found: %s" % args.tl)
        return 1

    names = sorted(n for n in os.listdir(args.tl) if n.endswith(".rpy"))
    print("Validating %s (%d .rpy files)\n" % (args.tl, len(names)))

    total_errors = total_warns = total_checked = 0
    nfiles = 0
    for name in names:
        path = os.path.join(args.tl, name)
        try:
            units = parse_tl_file(path)
            issues = scan_file(path)
        except UnicodeDecodeError as exc:
            print("== %s" % name)
            print("  ERROR  file is not valid UTF-8: %s" % exc)
            total_errors += 1
            continue
        except OSError as exc:
            print("== %s" % name)
            print("  ERROR  cannot read file: %s" % exc)
            total_errors += 1
            continue
        nfiles += 1
        total_checked += len(units)

        errs, warns = [], []
        for u in units:
            e, w = check_unit(u)
            errs.extend((u, m) for m in e)
            warns.extend((u, m) for m in w)
        lines = ["  ERROR  line %d: %s" % (ln, msg) for ln, msg in issues]
        lines += ["  ERROR  %s: %s" % (_context(u), m) for u, m in errs]
        lines += ["  WARN   %s: %s" % (_context(u), m) for u, m in warns]
        total_errors += len(issues) + len(errs)
        total_warns += len(warns)
        if lines:
            print("== %s (checked %d)" % (name, len(units)))
            for ln in lines:
                print(ln)
            print()

    if args.units:
        if not os.path.exists(args.units):
            print("ERROR: --units file not found: %s" % args.units)
            return 1
        try:
            db = load_units(args.units)
        except ValueError as exc:
            print("ERROR: --units file invalid: %s" % exc)
            return 1
        lines = []
        for u in db:
            total_checked += 1
            if u.status == "ok" and u.zh == "":
                lines.append("  ERROR  %s: status=ok but zh is empty "
                             "(missed translation)" % _context(u))
        if lines:
            print("== %s" % args.units)
            for ln in lines:
                print(ln)
            print()
        total_errors += len(lines)

    print("========== SUMMARY ==========")
    print("files checked : %d" % nfiles)
    print("units checked : %d" % total_checked)
    print("errors        : %d" % total_errors)
    print("warns         : %d" % total_warns)
    if total_errors:
        code, verdict = 1, "FAIL"
    elif total_warns:
        code, verdict = 2, "WARNINGS"
    else:
        code, verdict = 0, "PASS"
    print("result        : %s (exit %d)" % (verdict, code))
    return code


if __name__ == "__main__":
    sys.exit(main())
