#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
maps_merge.py - record-level three-way merge for MAPS files, with helpers.

Runs on Python 2.7 and Python 3.x. Standard library only. One file.

Commands
  merge  BASE OURS THEIRS   Git merge driver. Writes the result over OURS.
                            Exit 0 = merged cleanly, 1 = conflicts (written with
                            markers), 2 = could not parse (OURS left untouched).
  diff   FILE               Git textconv. Prints one normalised record per line.
  inspect FILE              Describe a MAPS file: record types, arity, keys, order.
  check  FILE               Validate a MAPS file: parse, header, arity, markers.
  selftest FILE             Copy FILE, make scripted edits, merge them, verify.
                            FILE itself is never touched.
  crosscheck MDL MAPS       Compare Simulink model structure with MAPS network
                            records. Reads R2024b text-package MDL, classic MDL,
                            and SLX.
  setup                     Print (or apply) the Git configuration lines. With
                            --matlabroot it also wires MathWorks' model merge
                            (mlAutoMerge) and merge window (mlMerge), and on
                            Linux fixes their Kerberos library clash.

Run  python maps_merge.py <command> -h  for the options of a command.

MAPS file format handled here (as observed):
  line 1      ' # MAPS-file version NNNNN, do not edit manually.'
  lines 2..N  one record per line: "record_type", "field", "field", ...
              every field double quoted, quotes inside fields written as the
              literal token __DOUBLE_QUOTE__, spacing after commas may vary.
  Records are grouped in blocks by record type; blocks and rows are sorted.

Merge rules
  * Each record type has a key (the identifying columns). Two versions of a
    record are the same record when their keys match.
  * A record changed on one side only is taken from that side.
  * A record changed identically on both sides is taken once.
  * A record changed differently on both sides is a conflict and is written
    with conflict markers, never guessed.
  * Records nobody touched are copied byte for byte. Changed records are
    copied byte for byte from the side that changed them. Nothing is
    reformatted.
  * "history" records are unioned. network_properties/version_number takes
    the maximum.
  * A record edited on one side and deleted on the other is a conflict, also
    when the edit touched a key column (a rename).

Notes
  * --check-cmd failure after a clean merge exits 1 with the merged file left
    in place (without markers) so it can be inspected.
  * Blank or comment lines between records of one block are written after
    that block. Real files have none.
"""
from __future__ import print_function, absolute_import

import argparse
import csv
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict

__version__ = "1.1.0"
PY2 = sys.version_info[0] == 2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Key width per record type = number of columns after the record type that
# identify the record. "all" = the whole record is the identity (set
# semantics, add/remove only). "all_but_last" = every column except the last.
# Unknown types use "default_key". If a configured key is not unique within a
# file it is widened automatically until it is, and this is reported.
DEFAULT_CONFIG = {
    "keys": {
        "parameter_value": 6,        # object, param, cond_var, cond_val, dataset, instance -> value
        "parameter_attributes": 6,   # same identity as parameter_value -> attributes
        "open_property": 6,          # object, property, x, config_group, config_block, instance -> value
        "control_mode": 2,           # mode, object -> value
        "object_type_swid": 1,       # object -> type, swid
        "base_network_object": "all",
        "base_network_connection": "all",
        "conf_group_object": "all",      # (group, object) pairs
        "conf_group_option": "all",
        "object_and_dataset": "all",     # (object, dataset) pairs
        "output_port": "all",            # (object, port) pairs
        "history": "all",
        "network_properties": 1,     # name -> value
        "maps_general": 1,
        "fileSettings": 1,
        "extMMDC": 1,
        "data_instance": 1,
        "patch_parameters": 1,
    },
    "default_key": "all_but_last",
    # Counters that take the maximum instead of conflicting: {type: [name, ...]}
    # (name = first column after the type). "*" instead of a list means every
    # numeric single-value record of that type.
    "monotonic": {"network_properties": ["version_number"]},
    # Write a "history" record describing the merge into the merged file
    "add_history_entry": False,
    # Where a config file may live, in order: --config, $MAPS_MERGE_CONFIG,
    # maps_merge.json next to this script.
}

CONFIG_ENV = "MAPS_MERGE_CONFIG"
CONFIG_BASENAME = "maps_merge.json"


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    candidates = []
    if path:
        candidates.append(path)
    else:
        if os.environ.get(CONFIG_ENV):
            candidates.append(os.environ[CONFIG_ENV])
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_BASENAME))
    for cand in candidates:
        if os.path.isfile(cand):
            with open(cand, "rb") as fh:
                raw = fh.read()
            if not PY2:
                raw = raw.decode("utf-8")
            user = json.loads(raw)
            for k, v in user.items():
                if k == "keys" and isinstance(v, dict):
                    cfg["keys"].update(v)
                elif not k.startswith("_"):
                    cfg[k] = v
            cfg["_source"] = cand
            break
    return cfg


# ---------------------------------------------------------------------------
# Small compatibility helpers
# ---------------------------------------------------------------------------

def read_text(path):
    """Read a file as native str, byte-transparent (latin-1 on Python 3)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if not PY2:
        data = data.decode("latin-1")
    return data


def write_text(path, text):
    """Write native str back byte-transparently, via a temp file in the same dir."""
    if not PY2:
        text = text.encode("latin-1")
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".maps_merge_", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text)
        if os.path.exists(path):
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        else:
            os.chmod(tmp, 0o644)
        try:
            os.rename(tmp, path)
        except OSError:
            os.remove(path)
            os.rename(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def to_bytes(s):
    if PY2:
        return s
    return s.encode("latin-1")


def eprint(*args):
    msg = " ".join(str(a) for a in args) + "\n"
    try:
        sys.stderr.write(msg)
    except UnicodeEncodeError:
        sys.stderr.write(msg.encode("ascii", "replace").decode("ascii"))


def write_stdout(text):
    """Byte-transparent stdout write on both Python versions and any locale."""
    stream = getattr(sys.stdout, "buffer", sys.stdout)
    stream.write(to_bytes(text))
    stream.flush()


def is_number(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# MAPS parsing
# ---------------------------------------------------------------------------

HEADER_RE = re.compile(r"^(?:\xef\xbb\xbf)?\s*#\s*MAPS-file\s+version\s+(\d+)", re.IGNORECASE)
MARKER_RE = re.compile(r"^(<{7,}|={7,}|>{7,}|\|{7,})( |\r?$)")
# a record: quoted fields separated by commas with optional blanks, no quote inside a field
RECORD_RE = re.compile(r'^\s*"(?:[^"]*"[ \t]*,[ \t]*")*[^"]*"\s*$')
# separators between quoted fields: quote, optional spaces, comma, optional spaces, quote
SEP_RE = re.compile(r'"([ \t]*,[ \t]*)"')
DEFAULT_SEP = ", "


class ParseError(Exception):
    pass


class Rec(object):
    __slots__ = ("rtype", "fields", "line")

    def __init__(self, fields, line):
        self.fields = fields
        self.rtype = fields[0]
        self.line = line


def normalise_seps(line):
    """Turn every  "  ,   "  between fields into  ","  so csv sees a clean row.
    Quotes never appear inside fields (they are written as __DOUBLE_QUOTE__),
    so a quote-comma-quote sequence is always a field boundary."""
    return SEP_RE.sub('","', line.strip())


def parse_record_lines(lines, path="<maps>", offsets=None):
    """Parse a list of record lines (each starting with a quote) into Rec objects."""
    cleaned = []
    for i, line in enumerate(lines):
        if not RECORD_RE.match(line):
            where = offsets[i] if offsets else i + 1
            raise ParseError("%s line %d: malformed record (every field must be double quoted, "
                             "fields separated by commas, no quote inside a field)" % (path, where))
        cleaned.append(normalise_seps(line))
    try:
        rows = list(csv.reader(cleaned, delimiter=",", quotechar='"', skipinitialspace=True))
    except csv.Error as exc:
        raise ParseError("%s: %s" % (path, exc))
    if len(rows) != len(lines):
        raise ParseError("%s: record count mismatch while parsing (%d lines, %d rows)"
                         % (path, len(lines), len(rows)))
    recs = []
    for i, row in enumerate(rows):
        fields = tuple(row)
        if not fields or not fields[0]:
            where = offsets[i] if offsets else i + 1
            raise ParseError("%s line %d: record without a type" % (path, where))
        recs.append(Rec(fields, lines[i]))
    return recs


class MapsFile(object):
    """A parsed MAPS file. Keeps every original line verbatim."""

    def __init__(self, path=None, text=None):
        self.path = path or "<maps>"
        if text is None:
            text = read_text(path)
        self.trailing_newline = text.endswith("\n")
        lines = text.split("\n")
        if self.trailing_newline:
            lines.pop()
        self.header = None
        self.version = None
        self.preamble = []              # raw lines before the first record
        self.blocks = OrderedDict()     # rtype -> [Rec] in file order
        self.trailers = OrderedDict()   # rtype -> raw lines that followed that block
        self.nlines = len(lines)
        record_lines = []
        record_offsets = []
        record_slots = []               # (rtype placeholder) filled after parsing
        last_type_marker = []
        for idx, line in enumerate(lines):
            if self.header is None and not record_lines and HEADER_RE.match(line):
                self.header = line
                self.version = int(HEADER_RE.match(line).group(1))
                continue
            if line.lstrip().startswith('"'):
                record_lines.append(line)
                record_offsets.append(idx + 1)
                last_type_marker.append(("rec", len(record_lines) - 1))
            else:
                if MARKER_RE.match(line):
                    raise ParseError("%s line %d: conflict marker present" % (self.path, idx + 1))
                last_type_marker.append(("raw", line))
        recs = parse_record_lines(record_lines, self.path, record_offsets)
        self.nrecords = len(recs)
        last_type = None
        for kind, payload in last_type_marker:
            if kind == "rec":
                rec = recs[payload]
                self.blocks.setdefault(rec.rtype, []).append(rec)
                last_type = rec.rtype
            else:
                if last_type is None:
                    self.preamble.append(payload)
                else:
                    self.trailers.setdefault(last_type, []).append(payload)

    def all_records(self):
        for recs in self.blocks.values():
            for r in recs:
                yield r

    def dominant_sep(self):
        counts = {}
        for r in self.all_records():
            for m in SEP_RE.finditer(r.line):
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        if not counts:
            return DEFAULT_SEP
        return max(counts.items(), key=lambda kv: kv[1])[0]


def make_line(fields, sep=DEFAULT_SEP):
    return '"' + ('"' + sep + '"').join(fields) + '"'


def types_sorted(types):
    return all(types[i] <= types[i + 1] for i in range(len(types) - 1))


def block_sorted(recs):
    return all(recs[i].fields <= recs[i + 1].fields for i in range(len(recs) - 1))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def arity_of(lists):
    """Smallest field count over all records (keys are derived from the shortest
    record shape, so an extra column on one side becomes a value difference,
    never an invisible key difference)."""
    best = None
    for lst in lists:
        for r in lst:
            if best is None or len(r.fields) < best:
                best = len(r.fields)
    return best or 0


def arity_range(lists):
    lo = hi = None
    for lst in lists:
        for r in lst:
            n = len(r.fields)
            if lo is None or n < lo:
                lo = n
            if hi is None or n > hi:
                hi = n
    return lo or 0, hi or 0


def key_width(cfg, rtype, arity):
    spec = cfg["keys"].get(rtype, cfg.get("default_key", "all_but_last"))
    full = max(arity - 1, 0)
    if spec == "all":
        return full
    if spec == "all_but_last":
        return max(arity - 2, 0)
    try:
        n = int(spec)
    except (TypeError, ValueError):
        return full
    return max(0, min(n, full))


def index_records(recs, n, full):
    """Map key -> Rec. With n == full (whole record is the key) identical
    duplicates are kept apart by an occurrence counter. Returns (index, dupes)."""
    index = OrderedDict()
    dupes = 0
    seen = {}
    for r in recs:
        k = r.fields[1:1 + n]
        if n >= full:
            occ = seen.get(k, 0)
            seen[k] = occ + 1
            k = k + (occ,)
        if k in index:
            dupes += 1
        else:
            index[k] = r
    return index, dupes


def resolve_width(cfg, rtype, lists):
    """Pick the key width for a type, widening until unique in every list."""
    arity = arity_of(lists)
    full = max(arity - 1, 0)
    n = key_width(cfg, rtype, arity)
    widened = False
    while True:
        if all(index_records(lst, n, full)[1] == 0 for lst in lists):
            return n, full, widened
        if n >= full:
            return full, full, widened
        n += 1
        widened = True


def min_unique_width(recs, full):
    for n in range(0, full + 1):
        if index_records(recs, n, full)[1] == 0 and n < full:
            return n
    return full


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeResult(object):
    def __init__(self):
        self.text = None
        self.conflicts = []        # (rtype, key, ours_rec, base_rec, theirs_rec)
        self.ours_changes = 0
        self.theirs_changes = 0
        self.widened = []          # (rtype, from, to)
        self.notes = []


def monotonic_names(cfg, rtype):
    """Names (first column) of counters of this type that take the maximum, or
    "*" for all, or None."""
    mono = cfg.get("monotonic") or {}
    if isinstance(mono, dict):
        names = mono.get(rtype)
        if names == "*":
            return "*"
        return list(names) if names else None
    names = []
    for entry in mono:            # legacy list form: "type" or "type:name"
        if ":" in entry:
            t, nm = entry.split(":", 1)
            if t == rtype:
                names.append(nm)
        elif entry == rtype:
            return "*"
    return names or None


def merge_type(rtype, b, o, t, n, full, cfg, result):
    """Merge one record type. Returns list of items: ("line", fields, line) or
    ("conflict", fields, ours_rec, base_rec, theirs_rec)."""
    B, _ = index_records(b, n, full)
    O, _ = index_records(o, n, full)
    T, _ = index_records(t, n, full)
    keys = list(B.keys())
    for k in O:
        if k not in B:
            keys.append(k)
    for k in T:
        if k not in B and k not in O:
            keys.append(k)
    mono = monotonic_names(cfg, rtype)
    items = []
    for k in keys:
        rb, ro, rt = B.get(k), O.get(k), T.get(k)
        fb = rb.fields if rb is not None else None
        fo = ro.fields if ro is not None else None
        ft = rt.fields if rt is not None else None
        if fo != fb:
            result.ours_changes += 1
        if ft != fb:
            result.theirs_changes += 1
        chosen = None
        if fo == ft:
            chosen = ro if ro is not None else rt
        elif fo == fb:
            chosen = rt
        elif ft == fb:
            chosen = ro
        else:
            resolved = False
            if mono is not None and ro is not None and rt is not None and len(fo) > 1 \
                    and (mono == "*" or fo[1] in mono):
                vo, vt = fo[1 + n:], ft[1 + n:]
                if len(vo) == 1 and len(vt) == 1 and is_number(vo[0]) and is_number(vt[0]):
                    chosen = ro if float(vo[0]) >= float(vt[0]) else rt
                    result.notes.append("%s %s: took the higher value %s" % (rtype, "/".join(k[:n]), chosen.fields[1 + n]))
                    resolved = True
            if not resolved:
                rep = fo if fo is not None else ft
                items.append(("conflict", rep, ro, rb, rt))
                result.conflicts.append((rtype, k[:n], ro, rb, rt))
                continue
        if chosen is None:
            continue  # deleted on one side, untouched on the other
        items.append(("line", chosen.fields, chosen.line))
    return guard_rewrites(rtype, B, O, T, n, full, items, result)


def _masked_index(recs):
    """(column, fields-with-that-column-blanked) -> [Rec]. Used to find records
    that differ from another record in exactly one column."""
    index = {}
    for r in recs:
        f = r.fields
        for c in range(1, len(f)):
            masked = f[:c] + (None,) + f[c + 1:]
            index.setdefault((c, masked), []).append(r)
    return index


def guard_rewrites(rtype, B, O, T, n, full, items, result):
    """Catch edits that hide behind delete+add.

    A base record that is absent from both sides, while a side added a record
    that differs from it in exactly one column, is that record edited on that
    side (or renamed, when the column is part of the key). Then:
      * both sides added such a variant and they differ -> conflict
      * only one side added a variant -> edit on that side, delete on the
        other -> conflict
    Records added identically on both sides are not rewrites. Types with a
    single data column are exempt: a one-column variant of a deleted record is
    indistinguishable from a new record.
    """
    if full < 2:
        return items
    removed_both = [B[k] for k in B if k not in O and k not in T]
    if not removed_both:
        return items
    added_o = [O[k] for k in O if k not in B]
    added_t = [T[k] for k in T if k not in B]
    same = set(r.fields for r in added_o) & set(r.fields for r in added_t)
    added_o = [r for r in added_o if r.fields not in same]
    added_t = [r for r in added_t if r.fields not in same]
    if not added_o and not added_t:
        return items
    idx_o = _masked_index(added_o)
    idx_t = _masked_index(added_t)
    used = set()
    pairs = []
    for rb in removed_both:
        f = rb.fields
        ro = rt = None
        for c in range(1, len(f)):
            key = (c, f[:c] + (None,) + f[c + 1:])
            if ro is None:
                for r in idx_o.get(key, []):
                    if id(r) not in used:
                        ro = r
                        break
            if rt is None:
                for r in idx_t.get(key, []):
                    if id(r) not in used:
                        rt = r
                        break
        if ro is None and rt is None:
            continue
        if ro is not None:
            used.add(id(ro))
        if rt is not None:
            used.add(id(rt))
        pairs.append((rb, ro, rt))
    if not pairs:
        return items
    for rb, ro, rt in pairs:
        for r in (ro, rt):
            if r is not None:
                _remove_one_line(items, r)
        rep = ro.fields if ro is not None else rt.fields
        items.append(("conflict", rep, ro, rb, rt))
        result.conflicts.append((rtype, rb.fields[1:1 + n], ro, rb, rt))
    return items


def _remove_one_line(items, rec):
    for i, it in enumerate(items):
        if it[0] == "line" and it[2] is rec.line:
            del items[i]
            return
    for i, it in enumerate(items):
        if it[0] == "line" and it[1] == rec.fields and it[2] == rec.line:
            del items[i]
            return


def render_conflict(ro, rb, rt, marker_size):
    lines = ["<" * marker_size + " ours"]
    if ro is not None:
        lines.append(ro.line)
    lines.append("|" * marker_size + " base")
    if rb is not None:
        lines.append(rb.line)
    lines.append("=" * marker_size)
    if rt is not None:
        lines.append(rt.line)
    lines.append(">" * marker_size + " theirs")
    return lines


def merge_headers(base, ours, theirs, result):
    hb, ho, ht = base.header, ours.header, theirs.header
    if ho == ht:
        return ho
    if ho == hb:
        return ht
    if ht == hb:
        return ho
    vo = ours.version if ours.version is not None else -1
    vt = theirs.version if theirs.version is not None else -1
    result.notes.append("header differs on both sides, kept version %d" % max(vo, vt))
    return ho if vo >= vt else ht


def history_entry_line(recs, ours_changes, theirs_changes, sep):
    fmt_sep = "  "
    for r in reversed(recs):
        if len(r.fields) > 1:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})(\s+)(\d{2}:\d{2}:\d{2})$", r.fields[1])
            if m:
                fmt_sep = m.group(2)
                break
    stamp = time.strftime("%Y-%m-%d") + fmt_sep + time.strftime("%H:%M:%S")
    user = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("USER") or getpass.getuser()
    comment = "maps_merge %s: merged %d change(s) from ours and %d from theirs" % (
        __version__, ours_changes, theirs_changes)
    fields = ("history", stamp, user, comment)
    return Rec(fields, make_line(fields, sep))


def merge_files(base, ours, theirs, cfg, marker_size=7):
    result = MergeResult()
    header = merge_headers(base, ours, theirs, result)

    preamble = list(base.preamble)
    for src in (ours, theirs):
        for line in src.preamble:
            if line not in preamble:
                preamble.append(line)

    types = list(base.blocks.keys())
    for src in (ours, theirs):
        for rtype in src.blocks:
            if rtype not in types:
                types.append(rtype)
    if types_sorted(list(base.blocks.keys())):
        types = sorted(types)

    out = []
    if header is not None:
        out.append(header)
    out.extend(preamble)
    trailers = OrderedDict()
    for src in (base, ours, theirs):
        for rtype, lines in src.trailers.items():
            cur = trailers.setdefault(rtype, [])
            for line in lines:
                if line not in cur:
                    cur.append(line)

    # pass 1: merge every record type
    merged = OrderedDict()
    for rtype in types:
        b = base.blocks.get(rtype, [])
        o = ours.blocks.get(rtype, [])
        t = theirs.blocks.get(rtype, [])
        n, full, widened = resolve_width(cfg, rtype, [b, o, t])
        if widened:
            result.widened.append((rtype, key_width(cfg, rtype, arity_of([b, o, t])), n))
        lo, hi = arity_range([b, o, t])
        if lo != hi:
            result.notes.append("%s: records have between %d and %d fields, key taken from the shortest"
                                % (rtype, lo, hi))
        items = merge_type(rtype, b, o, t, n, full, cfg, result)
        if b:
            keep_sorted = block_sorted(b)
        else:
            keep_sorted = block_sorted(o) and block_sorted(t)
        merged[rtype] = (items, keep_sorted, b or o or t)

    # optional history record describing the merge, once all changes are known
    if cfg.get("add_history_entry") and "history" in merged and not result.conflicts \
            and (result.ours_changes or result.theirs_changes):
        items, keep_sorted, sample = merged["history"]
        entry = history_entry_line(sample, result.ours_changes, result.theirs_changes, base.dominant_sep())
        items.append(("line", entry.fields, entry.line))

    # pass 2: write out
    for rtype in types:
        items, keep_sorted, _ = merged[rtype]
        if keep_sorted:
            items.sort(key=lambda it: it[1])
        for it in items:
            if it[0] == "line":
                out.append(it[2])
            else:
                out.extend(render_conflict(it[2], it[3], it[4], marker_size))
        out.extend(trailers.get(rtype, []))

    result.text = "\n".join(out) + ("\n" if ours.trailing_newline or base.trailing_newline else "")
    return result


def describe_conflict(rtype, key, ro, rb, rt, n_hint=None):
    def val(r):
        if r is None:
            return "<deleted>"
        part = r.fields[1 + len(key):]
        if not part or r.fields[1:1 + len(key)] != tuple(key):
            part = r.fields[1:]
        return '"' + '", "'.join(part) + '"'
    return "CONFLICT %s [%s]: ours=%s theirs=%s base=%s" % (
        rtype, " | ".join(key), val(ro), val(rt), val(rb))


def run_check_cmd(cmd, path):
    full = cmd.replace("{file}", _sh_quote(path))
    proc = subprocess.Popen(full, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.communicate()[0]
    if not PY2:
        output = output.decode("latin-1", "replace")
    return proc.returncode, output


def cmd_merge(args):
    cfg = load_config(args.config)
    label = args.path or os.path.basename(args.ours)
    try:
        base = MapsFile(args.base)
        ours = MapsFile(args.ours)
        theirs = MapsFile(args.theirs)
    except ParseError as exc:
        eprint("maps_merge: %s: cannot merge, %s" % (label, exc))
        eprint("maps_merge: %s left untouched; resolve by hand" % args.ours)
        return 2
    res = merge_files(base, ours, theirs, cfg, marker_size=args.marker_size)
    out_path = args.output or args.ours
    write_text(out_path, res.text)
    for rtype, frm, to in res.widened:
        eprint("maps_merge: %s: key for %s widened from %d to %d columns (not unique)" % (label, rtype, frm, to))
    for note in res.notes:
        eprint("maps_merge: %s: %s" % (label, note))
    for c in res.conflicts:
        eprint("maps_merge: %s: %s" % (label, describe_conflict(*c)))
    eprint("maps_merge: %s: %d change(s) from ours, %d from theirs, %d conflict(s)"
           % (label, res.ours_changes, res.theirs_changes, len(res.conflicts)))
    if res.conflicts:
        return 1
    if args.check_cmd:
        code, output = run_check_cmd(args.check_cmd, out_path)
        if code != 0:
            eprint("maps_merge: %s: check command failed (exit %d):" % (label, code))
            eprint(output.rstrip())
            return 1
        eprint("maps_merge: %s: check command passed" % label)
    return 0


# ---------------------------------------------------------------------------
# diff (textconv)
# ---------------------------------------------------------------------------

def cmd_diff(args):
    try:
        mf = MapsFile(args.file)
    except ParseError as exc:
        # Show the raw file so git diff still works on a broken file
        write_stdout(read_text(args.file))
        eprint("maps_merge: %s" % exc)
        return 0
    out = []
    if mf.header is not None:
        out.append(mf.header.strip())
    for rtype in sorted(mf.blocks):
        for r in sorted(mf.blocks[rtype], key=lambda r: r.fields):
            out.append(make_line(r.fields))
    write_stdout("\n".join(out) + "\n")
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

def inspect_file(mf, cfg):
    rows = []
    for rtype, recs in mf.blocks.items():
        arities = sorted(set(len(r.fields) for r in recs))
        full = max(arities[-1] - 1, 0)
        n_cfg = key_width(cfg, rtype, arities[-1])
        n_eff, _, widened = resolve_width(cfg, rtype, [recs])
        n_min = min_unique_width(recs, full)
        spec = cfg["keys"].get(rtype)
        rows.append({
            "type": rtype, "rows": len(recs), "arity": arities,
            "key_cfg": n_cfg, "key_eff": n_eff, "key_min": n_min,
            "from_default": spec is None, "widened": widened,
            "sorted": block_sorted(recs),
        })
    seps = {}
    escapes = 0
    commas = 0
    for r in mf.all_records():
        for m in SEP_RE.finditer(r.line):
            seps[m.group(1)] = seps.get(m.group(1), 0) + 1
        for f in r.fields:
            if "__DOUBLE_QUOTE__" in f:
                escapes += 1
            if "," in f:
                commas += 1
    return rows, seps, escapes, commas


def cmd_inspect(args):
    cfg = load_config(args.config)
    try:
        mf = MapsFile(args.file)
    except ParseError as exc:
        eprint("maps_merge: %s" % exc)
        return 2
    rows, seps, escapes, commas = inspect_file(mf, cfg)
    print("file:      %s" % args.file)
    print("header:    %s   (version %s)" % (mf.header.strip() if mf.header else "<none>", mf.version))
    print("lines:     %d   records: %d   other lines: %d" % (
        mf.nlines, mf.nrecords, len(mf.preamble) + sum(len(v) for v in mf.trailers.values())))
    print("types:     %d   type order sorted: %s" % (len(mf.blocks), "yes" if types_sorted(list(mf.blocks)) else "no"))
    print("separators: %s" % "  ".join('"%s" x%d' % (k.replace("\t", "\\t"), v) for k, v in sorted(seps.items(), key=lambda kv: -kv[1])))
    print("fields with __DOUBLE_QUOTE__: %d   fields with embedded commas: %d" % (escapes, commas))
    if cfg.get("_source"):
        print("config:    %s" % cfg["_source"])
    print("")
    print("%-28s %7s %7s %8s %8s %8s %7s" % ("record type", "rows", "arity", "key cfg", "key used", "key min", "sorted"))
    for r in rows:
        arity = "/".join(str(a) for a in r["arity"])
        flag = "*" if r["from_default"] else " "
        wid = "+" if r["widened"] else " "
        print("%-28s %7d %7s %7d%s %7d%s %8d %7s" % (
            r["type"], r["rows"], arity, r["key_cfg"], flag, r["key_eff"], wid, r["key_min"],
            "yes" if r["sorted"] else "no"))
    print("")
    print("key cfg  = configured key width (* = from the default rule, not configured)")
    print("key used = width actually used after widening to be unique (+ = widened)")
    print("key min  = smallest width that is unique in this file")
    if args.write_config:
        cfgout = {"keys": OrderedDict(), "_key_min": OrderedDict(), "_arity": OrderedDict()}
        for r in rows:
            cfgout["keys"][r["type"]] = r["key_eff"]
            cfgout["_key_min"][r["type"]] = r["key_min"]
            cfgout["_arity"][r["type"]] = r["arity"][-1]
        with open(args.write_config, "w") as fh:
            json.dump(cfgout, fh, indent=2)
        print("wrote %s" % args.write_config)
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def check_file(path, cfg):
    problems = []
    warnings = []
    text = read_text(path)
    for i, line in enumerate(text.split("\n")):
        if MARKER_RE.match(line):
            problems.append("line %d: conflict marker" % (i + 1))
    if problems:
        return problems, warnings, None
    try:
        mf = MapsFile(path, text=text)
    except ParseError as exc:
        problems.append(str(exc))
        return problems, warnings, None
    if mf.header is None:
        problems.append("no '# MAPS-file version' header line")
    if not mf.trailing_newline:
        warnings.append("file does not end with a newline")
    for rtype, recs in mf.blocks.items():
        arities = set(len(r.fields) for r in recs)
        if len(arities) > 1:
            problems.append("%s: inconsistent field count %s" % (rtype, sorted(arities)))
        n, full, widened = resolve_width(cfg, rtype, [recs])
        if widened:
            warnings.append("%s: configured key not unique, widened to %d columns" % (rtype, n))
        if not block_sorted(recs):
            warnings.append("%s: rows not in sorted order" % rtype)
    if not types_sorted(list(mf.blocks)):
        warnings.append("record type blocks not in sorted order")
    for rtype, lines in mf.trailers.items():
        for line in lines:
            if line.strip():
                warnings.append("unexpected non-record line after %s block: %r" % (rtype, line[:60]))
    return problems, warnings, mf


def cmd_check(args):
    cfg = load_config(args.config)
    problems, warnings, mf = check_file(args.file, cfg)
    for w in warnings:
        print("WARN  %s" % w)
    for p in problems:
        print("ERROR %s" % p)
    if not problems and args.run:
        code, output = run_check_cmd(args.run, args.file)
        if code != 0:
            print("ERROR external check failed (exit %d)" % code)
            print(output.rstrip())
            problems.append("external check failed")
        else:
            print("ok    external check passed")
    if problems:
        print("%s: %d problem(s), %d warning(s)" % (args.file, len(problems), len(warnings)))
        return 1
    print("%s: ok (%d records, %d types, %d warning(s))" % (
        args.file, mf.nrecords if mf else 0, len(mf.blocks) if mf else 0, len(warnings)))
    return 0


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

class SelfTest(object):
    def __init__(self, path, keep=False, config=None, verbose=True, use_git=False,
                 matlabroot=None, mdl=None, preload=None, arch=None):
        self.src = path
        self.preload = preload
        self.arch = arch
        self.keep = keep
        self.cfg = load_config(config)
        self.verbose = verbose
        self.use_git = use_git
        self.matlabroot = matlabroot
        self.mdl = mdl
        self.results = []
        self.work = tempfile.mkdtemp(prefix="maps_selftest_")

    def log(self, ok, name, detail=""):
        self.results.append((ok, name, detail))
        if self.verbose:
            print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (" + detail + ")") if detail else ""))

    def write(self, name, text):
        p = os.path.join(self.work, name)
        write_text(p, text)
        return p

    def cli(self, *args):
        devnull = open(os.devnull, "w")
        try:
            return subprocess.call([sys.executable, os.path.abspath(__file__)] + list(args), stderr=devnull)
        finally:
            devnull.close()

    def merge(self, base_text, ours_text, theirs_text):
        base = MapsFile(text=base_text)
        ours = MapsFile(text=ours_text)
        theirs = MapsFile(text=theirs_text)
        return merge_files(base, ours, theirs, self.cfg)

    def replaced(self, text, old_line, new_line):
        assert old_line in text.split("\n"), "line to replace not found"
        return "\n".join(new_line if l == old_line else l for l in text.split("\n"))

    def removed(self, text, old_line):
        assert old_line in text.split("\n"), "line to remove not found"
        return "\n".join(l for l in text.split("\n") if l != old_line)

    def run(self):
        base_text = read_text(self.src)
        base = MapsFile(text=base_text)
        sep = base.dominant_sep()

        # pick a value-bearing type with at least three records
        pick = None
        order = ["parameter_value", "open_property", "control_mode"] + list(base.blocks.keys())
        for rtype in order:
            recs = base.blocks.get(rtype)
            if not recs or len(recs) < 3:
                continue
            n, full, _ = resolve_width(self.cfg, rtype, [recs])
            if n < full:
                pick = (rtype, recs, n)
                break
        if pick is None:
            self.log(False, "find a value-bearing record type with 3+ records")
            return self.finish()
        rtype, recs, n = pick
        r1, r2, r3 = recs[0], recs[len(recs) // 2], recs[-1]
        if self.verbose:
            print("using record type %s (key width %d), work dir %s" % (rtype, n, self.work))

        def with_value(rec, value):
            f = list(rec.fields)
            f[1 + n] = value
            return make_line(tuple(f), sep), tuple(f)

        # 0. identical inputs give identical output
        res = self.merge(base_text, base_text, base_text)
        self.log(res.text == base_text and not res.conflicts, "merge of three identical copies is byte-identical")

        # 1. disjoint edits
        l1, f1 = with_value(r1, "SELFTEST_OURS")
        l2, f2 = with_value(r2, "SELFTEST_THEIRS")
        ours = self.replaced(base_text, r1.line, l1)
        theirs = self.replaced(base_text, r2.line, l2)
        res = self.merge(base_text, ours, theirs)
        merged = MapsFile(text=res.text)
        got = dict((r.fields[1:1 + n], r.fields) for r in merged.blocks[rtype])
        ok = (not res.conflicts and got.get(f1[1:1 + n]) == f1 and got.get(f2[1:1 + n]) == f2
              and merged.nrecords == base.nrecords)
        self.log(ok, "two edits to different records merge cleanly", "%d records before, %d after" % (base.nrecords, merged.nrecords))
        # everything else byte-identical
        expect = set(base_text.split("\n")) - set([r1.line, r2.line]) | set([l1, l2])
        self.log(set(res.text.split("\n")) == expect, "all untouched lines are byte-identical")

        # 2. only one side edits: result equals that side as a record set
        res = self.merge(base_text, ours, base_text)
        self.log(not res.conflicts and sorted(res.text.split("\n")) == sorted(ours.split("\n")),
                 "edit on one side only is taken as-is")

        # 3. same record, different values -> conflict with markers
        l1b, _ = with_value(r1, "SELFTEST_CLASH")
        theirs_clash = self.replaced(base_text, r1.line, l1b)
        res = self.merge(base_text, ours, theirs_clash)
        has_markers = any(MARKER_RE.match(l) for l in res.text.split("\n"))
        self.log(len(res.conflicts) == 1 and has_markers and l1 in res.text and l1b in res.text,
                 "same record changed differently is reported as a conflict, both versions kept")

        # 4. same record, same new value -> no conflict
        res = self.merge(base_text, ours, ours)
        self.log(not res.conflicts and sorted(res.text.split("\n")) == sorted(ours.split("\n")),
                 "same change on both sides is taken once")

        # 5. delete vs modify -> conflict
        ours_del = self.removed(base_text, r2.line)
        res = self.merge(base_text, ours_del, theirs)
        self.log(len(res.conflicts) == 1, "delete on one side, edit on the other is a conflict")

        # 6. delete vs untouched -> deleted
        res = self.merge(base_text, ours_del, base_text)
        merged = MapsFile(text=res.text)
        self.log(not res.conflicts and merged.nrecords == base.nrecords - 1
                 and r2.line not in res.text, "delete on one side only is applied")

        # 7. spacing-only change on one side does not conflict with a value change on the other
        respaced = make_line(r1.fields, "," if sep != "," else ",  ")
        theirs_space = self.replaced(base_text, r1.line, respaced)
        res = self.merge(base_text, ours, theirs_space)
        self.log(not res.conflicts and l1 in res.text, "whitespace-only change never conflicts")

        # 8. both add the same new record -> once
        newf = list(r3.fields)
        newf[1] = newf[1] + "_SELFTEST_NEW"
        newl = make_line(tuple(newf), sep)
        added = self.replaced(base_text, r3.line, r3.line + "\n" + newl)
        res = self.merge(base_text, added, added)
        self.log(not res.conflicts and res.text.count(newl) == 1, "same new record added on both sides appears once")

        # 9. each side adds a different record -> both present, order sorted if base sorted
        newf2 = list(r3.fields)
        newf2[1] = newf2[1] + "_SELFTEST_NEW2"
        newl2 = make_line(tuple(newf2), sep)
        added2 = self.replaced(base_text, r3.line, r3.line + "\n" + newl2)
        res = self.merge(base_text, added, added2)
        merged = MapsFile(text=res.text)
        recs_m = merged.blocks[rtype]
        self.log(not res.conflicts and newl in res.text and newl2 in res.text
                 and merged.nrecords == base.nrecords + 2
                 and (block_sorted(recs_m) or not block_sorted(recs)),
                 "different new records from both sides are both kept")

        # 10. history union
        if "history" in base.blocks and base.blocks["history"]:
            h = base.blocks["history"][-1]
            hf_o = list(h.fields); hf_o[-1] = "selftest ours entry"
            hf_t = list(h.fields); hf_t[-1] = "selftest theirs entry"
            if len(hf_o) > 1:
                hf_o[1] = "2999-01-01  00:00:01"
                hf_t[1] = "2999-01-01  00:00:02"
            hl_o, hl_t = make_line(tuple(hf_o), sep), make_line(tuple(hf_t), sep)
            ours_h = self.replaced(base_text, h.line, h.line + "\n" + hl_o)
            theirs_h = self.replaced(base_text, h.line, h.line + "\n" + hl_t)
            res = self.merge(base_text, ours_h, theirs_h)
            self.log(not res.conflicts and hl_o in res.text and hl_t in res.text,
                     "history entries from both sides are both kept")
        else:
            self.log(True, "history union (skipped, no history records)")

        # 11. version counters take the maximum
        np_ = base.blocks.get("network_properties") or []
        vrec = None
        for r in np_:
            if len(r.fields) >= 3 and is_number(r.fields[-1]):
                vrec = r
                break
        if vrec is not None:
            v = int(float(vrec.fields[-1]))
            fo = list(vrec.fields); fo[-1] = str(v + 1)
            ft = list(vrec.fields); ft[-1] = str(v + 2)
            lo, lt = make_line(tuple(fo), sep), make_line(tuple(ft), sep)
            res = self.merge(base_text, self.replaced(base_text, vrec.line, lo),
                             self.replaced(base_text, vrec.line, lt))
            self.log(not res.conflicts and lt in res.text and lo not in res.text,
                     "network_properties counter takes the maximum")
        else:
            self.log(True, "version counter maximum (skipped, no numeric network_properties)")

        # 12. header version takes the maximum
        if base.header is not None:
            hv = base.version
            ho = HEADER_RE.sub(lambda m: m.group(0).replace(str(hv), str(hv + 1)), base.header)
            ht = HEADER_RE.sub(lambda m: m.group(0).replace(str(hv), str(hv + 5)), base.header)
            res = self.merge(base_text, self.replaced(base_text, base.header, ho),
                             self.replaced(base_text, base.header, ht))
            self.log(not res.conflicts and res.text.split("\n")[0] == ht, "header version takes the maximum")

        # 13. determinism and symmetry
        resA = self.merge(base_text, ours, theirs)
        resB = self.merge(base_text, ours, theirs)
        resC = self.merge(base_text, theirs, ours)
        self.log(resA.text == resB.text, "merge is deterministic")
        self.log(sorted(resA.text.split("\n")) == sorted(resC.text.split("\n")), "swapping ours/theirs gives the same records")

        # 14. the command line driver: exit codes and in-place write
        pb = self.write("base.MAPS", base_text)
        po = self.write("ours.MAPS", ours)
        pt = self.write("theirs.MAPS", theirs)
        code = self.cli("merge", pb, po, pt)
        self.log(code == 0 and l1 in read_text(po) and l2 in read_text(po), "command line: clean merge exits 0 and writes result over OURS")
        po2 = self.write("ours2.MAPS", ours)
        pt2 = self.write("theirs2.MAPS", theirs_clash)
        code = self.cli("merge", pb, po2, pt2)
        self.log(code == 1 and "<<<<<<< ours" in read_text(po2), "command line: conflict exits 1 and writes markers")
        pbad = self.write("bad.MAPS", base_text + '"broken\n')
        po3 = self.write("ours3.MAPS", ours)
        code = self.cli("merge", pb, po3, pbad)
        self.log(code == 2 and read_text(po3) == ours, "command line: unparsable input exits 2 and leaves OURS untouched")

        # 15. the merged file passes check
        problems, _, _ = check_file(po, self.cfg)
        self.log(not problems, "merged file passes structural check")

        if self.use_git:
            self.run_git_suite(base_text, ours, theirs, theirs_clash, l1, l2, l1b)
        if self.matlabroot:
            self.run_matlab_suite()
        return self.finish()

    def run_git_suite(self, base_text, ours, theirs, clash, l1, l2, l1b):
        """A throwaway git repository around a copy of the file, with the driver
        registered exactly as `setup --apply` would do it."""
        repo = os.path.join(self.work, "repo")
        os.makedirs(repo)
        name = os.path.basename(self.src)
        fpath = os.path.join(repo, name)

        def git(*args):
            try:
                proc = subprocess.Popen(["git"] + list(args), cwd=repo,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            except OSError as exc:
                return 127, str(exc)
            out = proc.communicate()[0]
            if not PY2:
                out = out.decode("latin-1", "replace")
            return proc.returncode, out

        code, out = git("init", "-q")
        if code != 0:
            self.log(False, "git: repository created", out.strip())
            return
        git("config", "user.email", "selftest@maps_merge")
        git("config", "user.name", "maps_merge selftest")
        git("config", "commit.gpgsign", "false")
        git("checkout", "-q", "-b", "base")
        write_text(fpath, base_text)
        write_text(os.path.join(repo, ".gitattributes"), "\n".join(ATTR_LINES) + "\n")
        for k, v in git_config_lines(os.path.abspath(__file__), sys.executable, None):
            git("config", k, v)
        git("add", ".")
        code, out = git("commit", "-q", "-m", "base")
        self.log(code == 0, "git: repository created with the driver registered as setup does it", out.strip())
        code, out = git("check-attr", "merge", "--", name)
        self.log("merge: maps" in out, "git: the file is routed to the maps driver", out.strip())

        git("checkout", "-q", "-b", "alice")
        write_text(fpath, ours)
        git("commit", "-q", "-am", "alice")
        git("checkout", "-q", "base")
        git("checkout", "-q", "-b", "bob")
        write_text(fpath, theirs)
        git("commit", "-q", "-am", "bob")
        code, out = git("merge", "--no-edit", "alice")
        merged = read_text(fpath)
        self.log(code == 0 and l1 in merged and l2 in merged and "0 conflict(s)" in out,
                 "git merge: two branches with different edits merge cleanly",
                 " ".join(l for l in out.split("\n") if "maps_merge" in l).strip())

        code, out = git("diff", "base", "alice", "--", name)
        self.log("+" + l1 in out and "Binary files" not in out,
                 "git diff: shows the changed record instead of 'Binary files differ'")

        git("checkout", "-q", "base")
        git("checkout", "-q", "-b", "carol")
        write_text(fpath, clash)
        git("commit", "-q", "-am", "carol")
        code, out = git("merge", "--no-edit", "alice")
        text = read_text(fpath)
        _, status = git("status", "--porcelain")
        self.log(code != 0 and "<<<<<<< ours" in text and l1 in text and l1b in text and "UU " in status,
                 "git merge: same record changed on both branches stops with a conflict",
                 " ".join(l for l in out.split("\n") if "CONFLICT" in l).strip()[:120])
        git("merge", "--abort")

        git("checkout", "-q", "base")
        git("checkout", "-q", "-b", "dave")
        write_text(fpath, theirs)
        git("commit", "-q", "-am", "dave")
        code, out = git("rebase", "alice")
        merged = read_text(fpath)
        self.log(code == 0 and l1 in merged and l2 in merged, "git rebase: uses the driver too")

    def run_matlab_suite(self):
        exe = mlautomerge_path(self.matlabroot, self.arch)
        self.log(os.path.isfile(exe), "MathWorks mlAutoMerge present", exe)
        gui = matlab_tool_path(self.matlabroot, "mlMerge", self.arch)
        self.log(os.path.isfile(gui), "MathWorks mlMerge (merge window) present", gui)
        if not os.path.isfile(exe):
            return
        preload = self.preload or find_krb5_preload(self.matlabroot, self.arch)
        env = dict(os.environ)
        if preload:
            env["LD_PRELOAD"] = preload
            if self.verbose:
                print("starting mlAutoMerge with LD_PRELOAD=%s, as git will" % preload)
        if not self.mdl:
            self.log(True, "mlAutoMerge run on a model (skipped, pass --mdl MODEL.mdl to try it)")
            return
        ext = os.path.splitext(self.mdl)[1] or ".mdl"
        copies = []
        for n in ("mt_base", "mt_ours", "mt_theirs"):
            dst = os.path.join(self.work, n + ext)
            shutil.copyfile(self.mdl, dst)
            copies.append(dst)
        original = read_text(copies[1])
        try:
            proc = subprocess.Popen([exe, copies[0], copies[1], copies[2], copies[1]],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            out = proc.communicate()[0]
            if not PY2:
                out = out.decode("latin-1", "replace")
            code = proc.returncode
        except OSError as exc:
            code, out = 127, str(exc)
        ok = code == 0 and os.path.isfile(copies[1]) and read_text(copies[1]) == original
        self.log(ok, "mlAutoMerge runs on this model (three identical copies, exit 0, unchanged)",
                 ("exit %d " % code) + out.strip().replace("\n", " | ")[:200])

    def finish(self):
        passed = sum(1 for ok, _, _ in self.results if ok)
        total = len(self.results)
        if self.verbose:
            print("")
            print("%d of %d checks passed. Source file untouched." % (passed, total))
            if self.keep:
                print("work dir kept: %s" % self.work)
        if not self.keep:
            shutil.rmtree(self.work, ignore_errors=True)
        return 0 if passed == total else 1


def cmd_selftest(args):
    try:
        MapsFile(args.file)
    except ParseError as exc:
        eprint("maps_merge: %s" % exc)
        return 2
    return SelfTest(args.file, keep=args.keep, config=args.config, use_git=args.git,
                    matlabroot=args.matlabroot, mdl=args.mdl, preload=args.preload,
                    arch=args.matlab_arch).run()


# ---------------------------------------------------------------------------
# crosscheck: Simulink model structure vs MAPS network records
# ---------------------------------------------------------------------------

class ModelStructure(object):
    def __init__(self):
        self.blocks = OrderedDict()   # path -> block type
        self.lines = []               # (src_path, dst_path)
        self.unresolved = 0


def _clean_name(name):
    return (name or "").replace("\r", "").replace("\n", " ")


def parse_opc_package(text):
    parts = OrderedDict()
    cur = None
    buf = []
    for line in text.split("\n"):
        if line.startswith("__MWOPC_PART_BEGIN__"):
            if cur is not None:
                parts[cur] = "\n".join(buf)
            cur = line[len("__MWOPC_PART_BEGIN__"):].strip()
            buf = []
        elif line.startswith("__MWOPC_PART_END__") or line.startswith("__MWOPC_PACKAGE_END__"):
            if cur is not None:
                parts[cur] = "\n".join(buf)
            cur = None
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        parts[cur] = "\n".join(buf)
    return parts


def _xml_root(content):
    import xml.etree.ElementTree as ET
    return ET.fromstring(to_bytes(content.lstrip()))


def _endpoint_sid(text):
    # "12#out:1" -> "12"; "3:5#in:2" -> "3:5"
    return (text or "").split("#")[0].strip()


def _p(elem, name):
    for p in elem.findall("P"):
        if p.get("Name") == name:
            return p.text or ""
    return ""


def parse_slx_parts(parts):
    """Build structure from /simulink/systems/system_*.xml parts (SLX or R2024b MDL)."""
    systems = OrderedDict()
    for path, content in parts.items():
        m = re.match(r"^/?simulink/systems/(system_[^/]+)\.xml$", path)
        if not m:
            continue
        root = _xml_root(content)
        systems[m.group(1)] = root
    if not systems:
        raise ParseError("no simulink/systems/system_*.xml parts found")
    struct = ModelStructure()
    children_of = {}   # system id -> list of (sid, name, btype, child_system_id)

    def collect(root):
        blocks = []
        for blk in root.findall("Block"):
            child = None
            sysref = blk.find("System")
            if sysref is not None:
                child = sysref.get("Ref")
                if child is None:
                    child = ("inline", sysref)
            blocks.append((blk.get("SID"), _clean_name(blk.get("Name")), blk.get("BlockType"), child))
        lines = []
        for ln in root.findall("Line"):
            src = _endpoint_sid(_p(ln, "Src"))
            dsts = []

            def walk(el):
                d = _p(el, "Dst")
                if d:
                    dsts.append(_endpoint_sid(d))
                for br in el.findall("Branch"):
                    walk(br)
            walk(ln)
            lines.append((src, dsts))
        return blocks, lines

    def visit(sysid_or_root, prefix):
        root = sysid_or_root if not isinstance(sysid_or_root, str) else systems.get(sysid_or_root)
        if root is None:
            struct.unresolved += 1
            return
        blocks, lines = collect(root)
        sid_to_path = {}
        for sid, name, btype, child in blocks:
            path = prefix + name if prefix else name
            struct.blocks[path] = btype or ""
            if sid:
                sid_to_path[sid] = path
                sid_to_path[sid.split(":")[-1]] = path
            if child is not None:
                if isinstance(child, tuple):
                    visit(child[1], path + "/")
                else:
                    visit(child, path + "/")
        for src, dsts in lines:
            sp = sid_to_path.get(src) or sid_to_path.get(src.split(":")[-1])
            for d in dsts:
                dp = sid_to_path.get(d) or sid_to_path.get(d.split(":")[-1])
                if sp is None or dp is None:
                    struct.unresolved += 1
                    continue
                struct.lines.append((sp, dp))

    if "system_root" in systems:
        visit("system_root", "")
    else:
        # roots = systems never referenced by another system
        referenced = set()
        for root in systems.values():
            for s in root.iter("System"):
                if s.get("Ref"):
                    referenced.add(s.get("Ref"))
        roots = [s for s in systems if s not in referenced] or list(systems)[:1]
        for r in roots:
            visit(r, "")
    return struct


def parse_classic_mdl(text):
    """Classic brace-format MDL (pre-R2024b)."""
    open_re = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*\{\s*$")
    close_re = re.compile(r"^\s*\}\s*$")
    prop_re = re.compile(r"^\s*([A-Za-z_][\w.]*)\s+(.*?)\s*$")
    root = {"type": "ROOT", "props": {}, "children": []}
    stack = [root]
    for line in text.split("\n"):
        if close_re.match(line):
            if len(stack) > 1:
                stack.pop()
            continue
        m = open_re.match(line)
        if m:
            node = {"type": m.group(1), "props": {}, "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
            continue
        m = prop_re.match(line)
        if m:
            val = m.group(2)
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1].replace('\\"', '"')
            stack[-1]["props"][m.group(1)] = val
    struct = ModelStructure()

    def find_root_system(node):
        for ch in node["children"]:
            if ch["type"] == "Model":
                for s in ch["children"]:
                    if s["type"] == "System":
                        return s
            r = find_root_system(ch)
            if r is not None:
                return r
        return None

    def visit(sysnode, prefix):
        names = {}
        for ch in sysnode["children"]:
            if ch["type"] == "Block":
                name = _clean_name(ch["props"].get("Name", ""))
                path = prefix + name if prefix else name
                struct.blocks[path] = ch["props"].get("BlockType", "")
                names[name] = path
                for sub in ch["children"]:
                    if sub["type"] == "System":
                        visit(sub, path + "/")
        for ch in sysnode["children"]:
            if ch["type"] == "Line":
                src = names.get(_clean_name(ch["props"].get("SrcBlock", "")))
                dsts = []

                def walk(n):
                    d = n["props"].get("DstBlock")
                    if d:
                        dsts.append(names.get(_clean_name(d)))
                    for b in n["children"]:
                        if b["type"] == "Branch":
                            walk(b)
                walk(ch)
                for d in dsts:
                    if src is None or d is None:
                        struct.unresolved += 1
                    else:
                        struct.lines.append((src, d))

    sysroot = find_root_system(root)
    if sysroot is None:
        raise ParseError("no Model/System block found in classic MDL")
    visit(sysroot, "")
    return struct


def load_model_structure(path):
    lower = path.lower()
    if lower.endswith(".slx"):
        import zipfile
        parts = OrderedDict()
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.startswith("simulink/systems/") and name.endswith(".xml"):
                    data = zf.read(name)
                    parts["/" + name] = data if PY2 else data.decode("latin-1")
        return parse_slx_parts(parts), "slx"
    text = read_text(path)
    head = text[:4096]
    if "__MWOPC_PART_BEGIN__" in text[:200000] or "MathWorks OPC Text Package" in head:
        return parse_slx_parts(parse_opc_package(text)), "mdl-text-package"
    return parse_classic_mdl(text), "mdl-classic"


def map_block_name(path, rule="dots", strip=0, prefix=""):
    parts = path.split("/")
    if strip:
        parts = parts[strip:]
    if not parts:
        return ""
    if rule == "leaf":
        return prefix + parts[-1]
    if rule == "slash":
        return prefix + "/".join(parts)
    return prefix + ".".join(parts)


def cmd_crosscheck(args):
    try:
        struct, kind = load_model_structure(args.mdl)
    except (ParseError, IOError, OSError) as exc:
        eprint("maps_merge: cannot read model %s: %s" % (args.mdl, exc))
        return 2
    except Exception as exc:  # XML errors etc.
        eprint("maps_merge: cannot parse model %s: %s" % (args.mdl, exc))
        return 2
    try:
        mf = MapsFile(args.maps)
    except ParseError as exc:
        eprint("maps_merge: %s" % exc)
        return 2

    mapped = OrderedDict()
    for path, btype in struct.blocks.items():
        mapped.setdefault(map_block_name(path, args.map, args.strip, args.prefix), []).append((path, btype))
    mdl_names = set(mapped)
    mdl_lines = set((map_block_name(s, args.map, args.strip, args.prefix),
                     map_block_name(d, args.map, args.strip, args.prefix)) for s, d in struct.lines)

    maps_objects = OrderedDict()
    for r in mf.blocks.get("base_network_object", []):
        if len(r.fields) > 1:
            maps_objects[r.fields[1]] = True
    conns = []
    for r in mf.blocks.get("base_network_connection", []):
        if len(r.fields) >= 4:
            conns.append((r.fields[1], r.fields[3], r))
        elif len(r.fields) >= 3:
            conns.append((r.fields[1], r.fields[2], r))

    orphan_objects = [o for o in maps_objects if o not in mdl_names]
    bad_endpoints = OrderedDict()
    for a, b, r in conns:
        for e in (a, b):
            if e not in mdl_names and e not in bad_endpoints:
                bad_endpoints[e] = r
    unmatched_conns = [(a, b) for a, b, r in conns if (a, b) not in mdl_lines and a in mdl_names and b in mdl_names]
    mdl_only = [n for n in mdl_names if n not in maps_objects]
    by_type = {}
    for n in mdl_only:
        for path, btype in mapped[n]:
            by_type[btype] = by_type.get(btype, 0) + 1

    print("model:  %s  (%s)  %d blocks, %d lines, %d unresolved references"
          % (args.mdl, kind, len(struct.blocks), len(struct.lines), struct.unresolved))
    print("maps:   %s  %d network objects, %d connections" % (args.maps, len(maps_objects), len(conns)))
    print("naming: rule=%s strip=%d prefix=%r   e.g. %s" % (
        args.map, args.strip, args.prefix,
        (list(mdl_names)[:1] or ["<none>"])[0]))
    print("")
    errors = 0

    def show(title, items, limit):
        print("%s: %d" % (title, len(items)))
        for it in items[:limit]:
            print("    %s" % it)
        if len(items) > limit:
            print("    ... %d more" % (len(items) - limit))

    show("ERROR  MAPS objects not present in the model", orphan_objects, args.limit)
    errors += len(orphan_objects)
    show("ERROR  MAPS connection endpoints not present in the model", list(bad_endpoints), args.limit)
    errors += len(bad_endpoints)
    show("INFO   MAPS connections with no direct line in the model (links through ports are expected here)",
         ["%s -> %s" % ab for ab in unmatched_conns], args.limit)
    print("INFO   model blocks with no MAPS object: %d  by block type: %s" % (
        len(mdl_only), ", ".join("%s=%d" % kv for kv in sorted(by_type.items(), key=lambda kv: -kv[1])[:12])))
    if args.show_model_only:
        for n in mdl_only[:args.limit]:
            print("    %s" % n)
    print("")
    if errors:
        print("%d problem(s). If every MAPS object is reported, the naming rule is probably wrong:"
              " try --map leaf, --strip 1, or --prefix." % errors)
        return 1 if args.strict else 0
    print("ok: every MAPS object and connection endpoint exists in the model")
    return 0


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

ATTR_LINES = [
    "*.MAPS merge=maps diff=maps",
    "*.maps merge=maps diff=maps",
    "*.mdl binary merge=mlAutoMerge",
    "*.slx binary merge=mlAutoMerge",
]


def matlab_arch(arch=None):
    """MATLAB's name for this platform's binary folder."""
    if arch:
        return arch
    if os.name == "nt":
        return "win64"
    if sys.platform == "darwin":
        return "maca64"
    return "glnxa64"


def matlab_tool_path(matlabroot, tool, arch=None):
    """Path of mlAutoMerge or mlMerge inside a MATLAB installation."""
    arch = matlab_arch(arch)
    if arch.startswith("win"):
        tool += ".bat" if tool == "mlAutoMerge" else ".exe"
    return os.path.join(matlabroot, "bin", arch, tool)


def mlautomerge_path(matlabroot, arch=None):
    return matlab_tool_path(matlabroot, "mlAutoMerge", arch)


def find_krb5_preload(matlabroot, arch=None):
    """On Linux, MATLAB's merge tools started from a plain shell (which is how
    git starts them) can mix MATLAB's Kerberos libraries with the system ones
    and crash before doing anything. On RHEL8 the error is
        libkrb5.so.3: undefined symbol: k5_buf_cstring
    Preloading MATLAB's own libkrb5support fixes it. Returns that library's
    path, or None when not on Linux or when MATLAB does not ship one."""
    arch = matlab_arch(arch)
    if not arch.startswith("glnx") or not matlabroot:
        return None
    for folder in (os.path.join(matlabroot, "bin", arch),
                   os.path.join(matlabroot, "sys", "os", arch)):
        exact = os.path.join(folder, "libkrb5support.so.0")
        if os.path.isfile(exact):
            return exact
        try:
            names = sorted(n for n in os.listdir(folder) if n.startswith("libkrb5support.so"))
        except OSError:
            continue
        for n in names:
            if os.path.isfile(os.path.join(folder, n)):
                return os.path.join(folder, n)
    return None


def with_preload(command, preload):
    """Prefix a shell command so it runs with LD_PRELOAD set. git runs merge
    drivers and mergetool commands through the shell, so this works inside
    the git config value itself; no wrapper script is needed."""
    if not preload:
        return command
    return 'env LD_PRELOAD="%s" %s' % (preload, command)


def git_config_lines(script, python, matlabroot, arch=None, preload=None):
    driver = '"%s" "%s" merge %%O %%A %%B -L %%L -P %%P' % (python, script)
    textconv = '"%s" "%s" diff' % (python, script)
    lines = [
        ("merge.maps.name", "MAPS record-level merge"),
        ("merge.maps.driver", driver),
        # for criss-cross merges git first merges the ancestors; the built-in
        # binary driver keeps one ancestor unchanged instead of writing markers
        ("merge.maps.recursive", "binary"),
        ("diff.maps.textconv", textconv),
    ]
    if matlabroot:
        auto = matlab_tool_path(matlabroot, "mlAutoMerge", arch)
        gui = matlab_tool_path(matlabroot, "mlMerge", arch)
        lines.append(("merge.mlAutoMerge.name", "MathWorks automatic model merge"))
        lines.append(("merge.mlAutoMerge.driver",
                      with_preload('"%s" %%O %%A %%B %%A' % auto, preload)))
        # the Three-Way Merge window, for model conflicts:
        #   git mergetool --tool=mlMerge -- path/to/model.mdl
        lines.append(("mergetool.mlMerge.cmd",
                      with_preload('"%s" "$BASE" "$LOCAL" "$REMOTE" "$MERGED"' % gui, preload)))
    return lines


def cmd_setup(args):
    script = os.path.abspath(__file__)
    python = args.python or sys.executable
    arch = matlab_arch(args.matlab_arch)
    preload = None
    if args.matlabroot:
        for tool in ("mlAutoMerge", "mlMerge"):
            path = matlab_tool_path(args.matlabroot, tool, arch)
            if not os.path.isfile(path):
                print("# WARNING: %s not found at %s" % (tool, path))
        if args.preload:
            preload = args.preload
            if not os.path.isfile(preload):
                print("# WARNING: preload library not found: %s" % preload)
        elif not args.no_preload:
            preload = find_krb5_preload(args.matlabroot, arch)
        if preload:
            print("# MATLAB merge tools will start with LD_PRELOAD=%s" % preload)
            print("#   (fixes 'libkrb5.so.3: undefined symbol: k5_buf_cstring' on RHEL8)")
        elif arch.startswith("glnx") and not args.no_preload:
            print("# note: no libkrb5support found in this MATLAB, tools start without a preload")
        print("")
    lines = git_config_lines(script, python, args.matlabroot, arch, preload)
    print("# 1. Attributes. For a private trial put these in .git/info/attributes")
    print("#    of your clone. For the whole team put them in .gitattributes and commit,")
    print("#    replacing (or placed after) any existing '*.MAPS binary' / '*.mdl binary' line.")
    for l in ATTR_LINES:
        print(l)
    print("")
    print("# 2. Git config, once per clone (or add --global):")
    for k, v in lines:
        print('git config %s %s' % (k, _sh_quote(v)))
    if not args.matlabroot:
        print("# (add --matlabroot /path/to/MATLAB/R2024b to also print the MDL merge lines)")
    else:
        print("# When a model conflicts, open the merge window with:")
        print("#   git mergetool --tool=mlMerge -- path/to/model.mdl")
    if not args.apply:
        return 0
    print("")
    for k, v in lines:
        cmd = ["git", "config"] + (["--global"] if args.global_ else []) + [k, v]
        code = subprocess.call(cmd)
        print("%s  git config %s" % ("ok " if code == 0 else "ERR", k))
        if code != 0:
            return 1
    if args.local_attributes:
        gitdir = None
        for flag in ("--git-common-dir", "--git-dir"):
            try:
                gitdir = subprocess.check_output(["git", "rev-parse", flag]).strip()
                if not PY2:
                    gitdir = gitdir.decode("utf-8")
                break
            except (subprocess.CalledProcessError, OSError):
                gitdir = None
        if not gitdir:
            print("ERR not inside a git repository, attributes not written")
            return 1
        info = os.path.join(gitdir, "info")
        if not os.path.isdir(info):
            os.makedirs(info)
        attr = os.path.join(info, "attributes")
        existing = read_text(attr) if os.path.exists(attr) else ""
        with open(attr, "ab") as fh:
            for l in ATTR_LINES:
                if l not in existing.split("\n"):
                    fh.write(to_bytes(l + "\n"))
        print("ok  wrote %s" % attr)
    return 0


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="maps_merge.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="maps_merge %s" % __version__)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    m = sub.add_parser("merge", help="git merge driver: merge BASE OURS THEIRS, result written over OURS")
    m.add_argument("base")
    m.add_argument("ours")
    m.add_argument("theirs")
    m.add_argument("-o", "--output", help="write the result here instead of over OURS")
    m.add_argument("-L", "--marker-size", type=int, default=7, help="conflict marker size (git passes %%L)")
    m.add_argument("-P", "--path", help="real path of the file, for messages (git passes %%P)")
    m.add_argument("--config", help="json config file with record keys")
    m.add_argument("--check-cmd", help="command to validate the merged file, {file} is replaced by its path; "
                                       "a non-zero exit turns the merge into a conflict")
    m.set_defaults(func=cmd_merge)

    d = sub.add_parser("diff", help="git textconv: print normalised records one per line")
    d.add_argument("file")
    d.set_defaults(func=cmd_diff)

    i = sub.add_parser("inspect", help="describe a MAPS file: types, arity, keys, ordering")
    i.add_argument("file")
    i.add_argument("--config")
    i.add_argument("--write-config", metavar="PATH", help="write the effective keys as a json config")
    i.set_defaults(func=cmd_inspect)

    c = sub.add_parser("check", help="validate a MAPS file")
    c.add_argument("file")
    c.add_argument("--config")
    c.add_argument("--run", metavar="CMD", help="external validator, {file} is replaced by the path")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("selftest", help="exercise the merge on scripted copies of FILE; FILE is never modified")
    s.add_argument("file")
    s.add_argument("--keep", action="store_true", help="keep the temp work dir")
    s.add_argument("--config")
    s.add_argument("--git", action="store_true",
                   help="also build a throwaway git repository and run real git merge / diff / rebase through the driver")
    s.add_argument("--matlabroot", help="also check that MathWorks mlAutoMerge exists under this MATLAB root")
    s.add_argument("--mdl", help="with --matlabroot: run mlAutoMerge on three identical copies of this model")
    s.add_argument("--preload", help="library to LD_PRELOAD for mlAutoMerge (default: found automatically)")
    s.add_argument("--matlab-arch", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_selftest)

    x = sub.add_parser("crosscheck", help="compare Simulink model structure with MAPS network records")
    x.add_argument("mdl", help="model file: .mdl (R2024b text package or classic) or .slx")
    x.add_argument("maps")
    x.add_argument("--map", choices=["dots", "slash", "leaf"], default="dots",
                   help="how a block path A/B/C becomes a MAPS object name (default dots: A.B.C)")
    x.add_argument("--strip", type=int, default=0, help="drop this many leading path components")
    x.add_argument("--prefix", default="", help="prepend this to every mapped name")
    x.add_argument("--strict", action="store_true", help="exit 1 when problems are found")
    x.add_argument("--limit", type=int, default=20, help="max items listed per category")
    x.add_argument("--show-model-only", action="store_true", help="list model blocks that have no MAPS object")
    x.set_defaults(func=cmd_crosscheck)

    u = sub.add_parser("setup", help="print or apply the git configuration for this script")
    u.add_argument("--apply", action="store_true", help="run the git config commands")
    u.add_argument("--global", dest="global_", action="store_true", help="with --apply: use git config --global")
    u.add_argument("--local-attributes", action="store_true",
                   help="with --apply: also write the attribute lines to .git/info/attributes")
    u.add_argument("--matlabroot", help="MATLAB install root, to include the MathWorks model merge tools")
    u.add_argument("--preload", help="library to LD_PRELOAD for the MATLAB tools "
                                     "(default on Linux: MATLAB's own libkrb5support, found automatically)")
    u.add_argument("--no-preload", action="store_true", help="start the MATLAB tools without a preload")
    u.add_argument("--matlab-arch", help=argparse.SUPPRESS)
    u.add_argument("--python", help="python interpreter to put in the driver command (default: this one)")
    u.set_defaults(func=cmd_setup)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
