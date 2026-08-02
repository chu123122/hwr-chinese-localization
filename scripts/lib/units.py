"""Shared core library for the HWR localization toolchain (SPEC §3 / §4 / §6).

Provides the Unit data model (db/units.jsonl entry), parsing of Ren'Py 7
`translate` skeleton files (tl/chinese/*.rpy), jsonl persistence, and
dedupe/sort/escape utilities.  All other scripts import from here:

    import lib.units          # after adding scripts/ to sys.path

Data model (SPEC §3)
--------------------
One Unit per jsonl line.  Unique key: dialogue -> block_id; string ->
(file, old).  Field semantics follow SPEC §3 exactly; two in-memory-only
fields (`src_line`, `body_lines`) carry skeleton layout details used by
build_tl.py and are never serialized.

Parsing (SPEC §4 / §6)
----------------------
parse_tl_file() / parse_dir() read the generator's skeleton format:
  * dialogue blocks   ``translate chinese <label>_<hash>:``
      - old   : from the first comment line whose content starts with a
        quote (``# "..."``, or a triple-quoted ``# \"\"\"...\"\"\"`` that
        may span comment lines);
        if the block has no such comment, old falls back to the new line's
        value and the unit is marked src_lang="unknown",
        status="needs_review" (SPEC §6 priority ②).  Note: real official
        data contains many comment-less blocks that have no string literal
        at all (pure `nvl clear` blocks) -> old == new == "".
      - new   : the LAST string literal in the block (`"..."` or multiline
        triple-quoted), skipping comments and Python statement lines.
      - zh    : new if new != old, else "" (skeleton state = untranslated).
      - src_lang: "ru" if the old text contains Cyrillic, else "en"
        (SPEC §6; the no-comment fallback is forced to "unknown").
      - status: "ok" if new != old (already translated), "new" otherwise;
        the no-comment fallback is forced to "needs_review".
      - src_line : the `# game/<file>:<line>` source-position comment,
        preserved verbatim for build_tl.py (empty if absent).
      - body_lines: the block's Python statement lines (e.g. `nvl clear`,
        `nvl show`) in order, excluding comments and the new-literal lines.
      - label : block_id with the trailing `_<8-hex-hash>` and any
        duplicate counter `_<n>` stripped (best effort, informational).
  * strings blocks  ``translate chinese strings:``
      - consecutive `old "..."` / `new "..."` pairs (4-space indent);
        an `old` without a following `new` is kept with new == old;
        a `new` without a preceding `old` is kept with old == new.
        Multiple strings blocks in one file are merged: the same
        (file, old) appears once with occurrences incremented.
  * python / style blocks are skipped entirely (SPEC §4.3).

Escaping (SPEC §6)
------------------
Values are stored UNESCAPED.  unescape_str() only converts the escapes the
generator produces: escaped-quote to quote, escaped-backslash to
backslash, escaped-n to newline, escaped-t to tab.  Any other
backslash-letter sequence (such as escaped-brace) is kept verbatim, so a
literal-brace tag written with an escaped brace round-trips unchanged.
escape_str() is the inverse for writing output: quote to escaped-quote,
backslash to escaped-backslash, newline to escaped-n; an escaped-brace
pair already present is emitted as-is (plain braces need no escaping).
Both are applied only to string values, never to surrounding quotes.

Persistence (SPEC §6)
---------------------
save_units() writes UTF-8 JSON lines without BOM, only the SPEC fields
(occurrences is omitted when 1), and assigns monotonically increasing
seq values (starting after the max of the file's and the list's seqs) to
any unit whose seq is unassigned (-1), in list order.  load_units() is
tolerant: unknown json fields are ignored, missing fields get defaults.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__all__ = [
    "Unit", "KINDS", "STATUSES", "SRC_LANGS",
    "detect_src_lang", "unescape_str", "escape_str",
    "parse_tl_file", "parse_dir",
    "load_units", "save_units", "unit_from_dict",
    "dedupe", "sort_by_seq",
]

KINDS = ("dialogue", "string")
STATUSES = ("new", "changed", "ok", "needs_review")
SRC_LANGS = ("en", "ru", "unknown")

# JSON fields serialized by save_units(), in SPEC §3 field order.
JSON_FIELDS = (
    "kind", "file", "block_id", "label", "old", "zh", "official_zh",
    "src_lang", "status", "seq", "occurrences",
)

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
# block_id = <label>_<8-hex-hash> optionally followed by _<duplicate counter>
_ID_RE = re.compile(r"^(.+)_([0-9a-f]{8})(?:_([0-9]{1,4}))?$")


@dataclass
class Unit:
    """One translatable entry (SPEC §3).

    Fields:
      kind:        "dialogue" | "string"
      file:        source script basename (script.rpy, screens.rpy, ...)
      block_id:    dialogue translation block id `<label>_<hash>`; "" for strings
      label:       dialogue label name (informational); "" for strings
      old:         base text, unescaped, Ren'Py tags preserved
      zh:          translation; "" = untranslated
      official_zh: official Chinese anchor (filled by import_official.py)
      src_lang:    "en" | "ru" | "unknown" (Cyrillic detection on old)
      status:      "new" | "changed" | "ok" | "needs_review"
      seq:         global monotonic sequence number; -1 = unassigned
      occurrences: how often (file, old) occurs; only meaningful for strings

    In-memory only (never serialized):
      src_line:    raw `# game/<file>:<line>` comment from the skeleton,
                   used by build_tl.py to re-emit source positions
      body_lines:  the block's Python statement lines (nvl clear, ...),
                   used by build_tl.py to preserve them
    """

    kind: str = "dialogue"
    file: str = ""
    block_id: str = ""
    label: str = ""
    old: str = ""
    zh: str = ""
    official_zh: str = ""
    src_lang: str = "en"
    status: str = "new"
    seq: int = -1
    occurrences: int = 1
    # --- not serialized ---
    src_line: str = ""
    body_lines: List[str] = field(default_factory=list, repr=False, compare=False)

    def key(self) -> Tuple[str, ...]:
        """Unique key per SPEC §3: dialogue -> block_id; string -> (file, old)."""
        if self.kind == "dialogue":
            return ("dialogue", self.block_id)
        return ("string", self.file, self.old)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def detect_src_lang(text: str) -> str:
    """Return "ru" if *text* contains any Cyrillic character, else "en"."""
    return "ru" if _CYRILLIC_RE.search(text or "") else "en"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

def unescape_str(s: str) -> str:
    """Unescape the content of a string literal read from a skeleton file.

    Converts ``\\"`` -> ``"``, ``\\\\`` -> ``\\``, ``\\n`` -> newline,
    ``\\t`` -> tab.  Every other ``\\x`` sequence (``\\{``, ``\\}`` and any
    unknown escape) is kept verbatim so it round-trips unchanged through
    escape_str().  *s* must already be the text between the quotes.
    """
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt in ('"', "\\"):
                out.append(nxt)
            else:
                out.append(c)
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape_str(s: str) -> str:
    """Escape a value for writing inside a ``"..."`` literal (SPEC §6).

    ``"`` -> ``\\"``, ``\\`` -> ``\\\\``, newline -> ``\\n``.  ``{``/``}``
    need no escaping; a ``\\{`` / ``\\}`` sequence already present in the
    value is kept as-is.  *s* is the unescaped value; the surrounding
    quotes are the caller's job.
    """
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] in "{}":
                out.append(s[i:i + 2])
                i += 2
            else:
                out.append("\\\\")
                i += 1
        elif c == '"':
            out.append('\\"')
            i += 1
        elif c == "\n":
            out.append("\\n")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Literal scanning
# ---------------------------------------------------------------------------

def _is_escaped(line: str, k: int) -> bool:
    """True if the char at *k* is preceded by an odd number of backslashes."""
    bs = 0
    t = k - 1
    while t >= 0 and line[t] == "\\":
        bs += 1
        t -= 1
    return bs % 2 == 1


def _consume_literal(get_chunk, i: int):
    """Parse one double-quoted literal starting at chunk 0, char *i*.

    *get_chunk* is a callable returning the text of chunk *ci* on demand
    (chunks are line texts; callers decide whether they are stripped), or
    None when no such chunk exists.  A literal may span several chunks
    (triple-quoted ``\"\"\"...\"\"\"``), in which case chunk boundaries
    become newlines in the value.  Supports ``"..."`` and
    ``\"\"\"...\"\"\"``.  Returns (value, end_ci, end_i): the unescaped
    value and the position just past the closing quote (end_ci indexes
    into the chunk space; end_i is the char index after the closing
    quote, always >= 1 on success).  Returns (None, 0, i) if no literal
    starts there or it is unterminated.
    """
    line = get_chunk(0)
    if line is None:
        return None, 0, i
    if line.startswith('"""', i):
        delim = '"""'
        i += 3
    elif i < len(line) and line[i] == '"':
        delim = '"'
        i += 1
    else:
        return None, 0, i
    parts: List[str] = []
    seg = i
    ci = 0
    while True:
        line = get_chunk(ci)
        if line is None:
            return None, 0, i  # unterminated
        k = seg
        L = len(line)
        while k < L:
            if delim == '"':
                if line[k] == '"' and not _is_escaped(line, k):
                    parts.append(line[seg:k])
                    return unescape_str("".join(parts)), ci, k + 1
            else:  # triple
                if line.startswith('"""', k) and not _is_escaped(line, k):
                    parts.append(line[seg:k])
                    return unescape_str("".join(parts)), ci, k + 3
            k += 1
        if delim == '"""':
            parts.append(line[seg:])
            parts.append("\n")
            ci += 1
            seg = 0
        else:
            return None, 0, i  # unterminated single-line literal


# ---------------------------------------------------------------------------
# Skeleton parsing
# ---------------------------------------------------------------------------

def _basename(path: str) -> str:
    return os.path.basename(path)


def _block_id_of(line: str) -> str:
    """Extract the block identifier from a `translate chinese <ident>:` line."""
    parts = line.strip().split(None, 2)
    if len(parts) < 3:
        return ""
    return parts[2].rstrip(":").strip()


def _skip_block(lines, start: int) -> int:
    """Return the index just past the block starting at *start* (python/style)."""
    j = start + 1
    n = len(lines)
    while j < n and not lines[j].strip().startswith("translate "):
        j += 1
    return j


def _label_of(block_id: str) -> str:
    """Best-effort label: block_id minus `_<8-hex-hash>` and duplicate counter."""
    m = _ID_RE.match(block_id)
    return m.group(1) if m else ""


def _comment_literal(contents: List[str]) -> Optional[str]:
    """Value of the first comment whose content starts with a quote.

    *contents* are the `#`-comment texts of a dialogue block.  The first
    one that begins with `"` (or `\"\"\"`) is the old text; a multiline
    `# \"\"\"...` may continue on following comment lines, which are
    joined with newlines.  Returns None if the block has no such comment.
    """
    for idx, c in enumerate(contents):
        if c.startswith('"'):
            val, _end_ci, _end_i = _consume_literal(
                lambda ci, idx=idx: contents[idx + ci] if idx + ci < len(contents) else None, 0)
            return val
    return None


def _parse_dialogue_block(lines, start: int, file: str,
                          src_line: str = "") -> Tuple[Unit, int]:
    """Parse one dialogue translate block; returns (unit, index past block).

    *src_line* is the `# game/<file>:<line>` comment that precedes the
    block in the file (Ren'Py writes it before the translate line, so
    parse_tl_file's main loop collects it and hands it in).
    """
    block_id = _block_id_of(lines[start])
    body: List[str] = []
    j = start + 1
    n = len(lines)
    # body ends at the next translate line or at the next block's source
    # comment (which the main loop picks up as src_line for that block)
    while j < n:
        s = lines[j].strip()
        if s.startswith("translate ") or s.startswith("# game/"):
            break
        body.append(s)
        j += 1

    comment_contents: List[str] = []
    py_lines: List[str] = []
    literal_values: List[str] = []
    k, b = 0, len(body)
    while k < b:
        s = body[k]
        if not s:
            k += 1
            continue
        if s.startswith("#"):
            comment_contents.append(s[1:].strip())
            k += 1
            continue
        if s.startswith('"'):
            # dialogue string literal candidate (new); may be multiline """
            val, end_ci, _end_i = _consume_literal(
                lambda ci: body[k + ci] if k + ci < b else None, 0)
            if val is None:
                k += 1
            else:
                literal_values.append(val)
                k += end_ci + 1
            continue
        # Python statement line (nvl clear, nvl show, ...)
        py_lines.append(s)
        k += 1

    old = _comment_literal(comment_contents)
    new = literal_values[-1] if literal_values else ""
    if old is None:
        # SPEC §6 priority ②: no `# "..."` comment -> old from the new line.
        old = new
        src_lang = "unknown"
        status = "needs_review"
    else:
        src_lang = detect_src_lang(old)
        status = "ok" if new and new != old else "new"
    zh = new if new != old else ""
    unit = Unit(
        kind="dialogue", file=file, block_id=block_id,
        label=_label_of(block_id), old=old, zh=zh,
        src_lang=src_lang, status=status,
        src_line=src_line, body_lines=py_lines,
    )
    return unit, j


def _parse_strings_block(lines, start: int, file: str, units: List[Unit]) -> int:
    """Parse one `translate chinese strings:` block into *units*; returns
    the index just past the block.  `old` without a `new` keeps
    new == old (SPEC §6 tolerance); pairs may be triple-quoted."""
    j = start + 1
    n = len(lines)
    pending_old: Optional[str] = None

    def emit(old_val: str, new_val: str) -> None:
        units.append(Unit(
            kind="string", file=file, old=old_val,
            zh=new_val if new_val != old_val else "",
            src_lang=detect_src_lang(old_val),
            status="ok" if new_val and new_val != old_val else "new",
        ))

    while j < n:
        s = lines[j].strip()
        if s.startswith("translate "):
            break
        if not s or s.startswith("#"):
            j += 1
            continue
        if s.startswith("old ") or s.startswith("new "):
            is_new = s.startswith("new ")
            # literal starts after the keyword and any extra whitespace
            i0 = 3
            while i0 < len(s) and s[i0].isspace():
                i0 += 1
            # continuation chunks are stripped lazily (only triple-quoted
            # literals ever advance past chunk 0)
            val, end_ci, _end_i = _consume_literal(
                lambda ci: lines[j + ci].strip() if j + ci < n else None, i0)
            if is_new:
                old_val = pending_old if pending_old is not None else val
                emit(old_val, val)
                pending_old = None
            else:
                pending_old = val
            j += end_ci + 1
            continue
        j += 1
    if pending_old is not None:
        emit(pending_old, pending_old)  # old without new -> new == old
    return j


def _merge_units(units: List[Unit]) -> List[Unit]:
    """Merge units sharing a unique key (first occurrence wins).

    For strings the duplicate count accumulates in occurrences; a later
    duplicate carrying a non-empty zh upgrades an earlier empty zh (and
    its status).  Dialogue duplicates (malformed data) are dropped.
    """
    out: List[Unit] = []
    seen = {}
    for u in units:
        k = u.key()
        if k in seen:
            e = seen[k]
            if u.kind == "string":
                e.occurrences += 1
                if not e.zh and u.zh:
                    e.zh = u.zh
                    e.status = u.status
            # dialogue duplicate: keep first, ignore later
        else:
            seen[k] = u
            out.append(u)
    return out


def parse_tl_file(path: str) -> List[Unit]:
    """Parse one Ren'Py tl skeleton file into Units (SPEC §4).

    Dialogue blocks, strings blocks and skipped python/style blocks are
    recognized; strings blocks in the same file are merged (occurrences).
    Returns units in file order.  Raises OSError if *path* is unreadable;
    raises UnicodeDecodeError if the file is not valid UTF-8.
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if text.startswith("﻿"):
        text = text[1:]  # tolerate a BOM
    lines = text.splitlines()
    units: List[Unit] = []
    n = len(lines)
    i = 0
    pending_src = ""
    while i < n:
        s = lines[i].strip()
        if s.startswith("translate "):
            ident = _block_id_of(lines[i])
            head = ident.split()[0] if ident else ""
            if head == "strings":
                i = _parse_strings_block(lines, i, _basename(path), units)
            elif head in ("python", "style"):
                i = _skip_block(lines, i)
            elif ident:
                unit, i = _parse_dialogue_block(lines, i, _basename(path), pending_src)
                units.append(unit)
            pending_src = ""  # consumed (or dropped for strings/python/style)
            continue
        if s.startswith("# game/"):
            pending_src = s  # source-position comment of the NEXT translate block
        i += 1
    return _merge_units(units)


def parse_dir(tl_dir: str) -> List[Unit]:
    """Parse every ``*.rpy`` file in *tl_dir* into Units (SPEC §6).

    Files are processed in sorted-name order (deterministic build order);
    results are concatenated in that order.  Raises OSError if the
    directory is missing or unreadable.
    """
    units: List[Unit] = []
    for name in sorted(os.listdir(tl_dir)):
        if name.endswith(".rpy"):
            units.extend(parse_tl_file(os.path.join(tl_dir, name)))
    return units


# ---------------------------------------------------------------------------
# jsonl persistence
# ---------------------------------------------------------------------------

def unit_from_dict(d: dict) -> Unit:
    """Build a Unit from a decoded jsonl record.

    Unknown fields are ignored (so files written by future versions still
    load); missing fields fall back to the dataclass defaults.
    """
    kw = {k: d[k] for k in JSON_FIELDS if k in d}
    return Unit(**kw)


def load_units(path: str) -> List[Unit]:
    """Load units from a jsonl file (UTF-8, BOM tolerated per line set).

    Returns [] if the file does not exist.  Raises ValueError with
    file/line context on malformed JSON.
    """
    if not os.path.exists(path):
        return []
    units: List[Unit] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith("﻿"):
                line = line[1:]
            try:
                d = json.loads(line)
            except ValueError as exc:
                raise ValueError("%s:%d: invalid JSON: %s" % (path, lineno, exc)) from exc
            units.append(unit_from_dict(d))
    return units


def save_units(path: str, units: List[Unit]) -> int:
    """Write units to *path* as UTF-8 JSON lines without BOM (SPEC §6).

    Only the SPEC fields are serialized (occurrences omitted when 1).
    Units whose seq is unassigned (-1) receive monotonically increasing
    seq values, in list order, starting after the max of (the file's
    existing seqs, the given units' seqs) so appending new entries to a
    loaded list keeps seq increasing and never collides.  Returns the
    number of units written.
    """
    base_max = -1
    if os.path.exists(path):
        try:
            base_max = max((u.seq for u in load_units(path) if u.seq >= 0), default=-1)
        except ValueError:
            base_max = -1  # malformed existing file: ignore, rewrite fresh
    list_max = max((u.seq for u in units if u.seq >= 0), default=-1)
    next_seq = max(base_max, list_max) + 1
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for u in units:
            if u.seq < 0:
                u.seq = next_seq
                next_seq += 1
            d = {k: getattr(u, k) for k in JSON_FIELDS}
            if d["occurrences"] <= 1:
                del d["occurrences"]
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return len(units)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def dedupe(units: List[Unit]) -> List[Unit]:
    """Return a new list with duplicate-key units merged (first wins).

    Same rule as parse_tl_file's internal merge: strings accumulate
    occurrences, a duplicate with non-empty zh upgrades an empty zh.
    """
    return _merge_units(list(units))


def sort_by_seq(units: List[Unit]) -> List[Unit]:
    """Return units sorted by seq ascending; unassigned (seq < 0) last."""
    return sorted(units, key=lambda u: u.seq if u.seq >= 0 else float("inf"))
