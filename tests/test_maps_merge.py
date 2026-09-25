# -*- coding: utf-8 -*-
"""Tests for maps_merge.py. Run:  python -m unittest discover -s tests -v"""
from __future__ import print_function
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import maps_merge as mm          # noqa: E402
import gen_maps                  # noqa: E402

SCRIPT = os.path.join(ROOT, "maps_merge.py")
PY = sys.executable


def lines_of(text):
    return text.split("\n")


def replace_line(text, old, new):
    ls = lines_of(text)
    assert old in ls, "line not found: %r" % old
    return "\n".join(new if l == old else l for l in ls)


def remove_line(text, old):
    ls = lines_of(text)
    assert old in ls
    return "\n".join(l for l in ls if l != old)


def merge_texts(base, ours, theirs, cfg=None, **kw):
    cfg = cfg or mm.load_config(os.devnull)
    return mm.merge_files(mm.MapsFile(text=base), mm.MapsFile(text=ours), mm.MapsFile(text=theirs), cfg, **kw)


def rec_by_prefix(mf, rtype, *prefix):
    for r in mf.blocks[rtype]:
        if r.fields[1:1 + len(prefix)] == prefix:
            return r
    raise KeyError(prefix)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.text = gen_maps.gen()
        self.mf = mm.MapsFile(text=self.text)

    def test_header_with_leading_space(self):
        self.assertEqual(self.mf.header, gen_maps.HEADER)
        self.assertEqual(self.mf.version, 71219)

    def test_counts(self):
        self.assertEqual(self.mf.nrecords, self.mf.nlines - 1)
        self.assertEqual(len(self.mf.blocks), 23)

    def test_spacing_variants_parse_identically(self):
        f = ("parameter_value", "A.B", "gain_0[0]", "", "", "Nominal", "Default", "1.5")
        for sep in (", ", ",", ",  ", " , ", "\t,\t"):
            line = '"' + ('"' + sep + '"').join(f) + '"'
            recs = mm.parse_record_lines([line])
            self.assertEqual(recs[0].fields, f, sep)
            self.assertEqual(recs[0].line, line)

    def test_escape_token_and_embedded_commas_kept(self):
        line = '"history", "2008-06-12  19:41:40", "cward", "Update x, y, and __DOUBLE_QUOTE__To default__DOUBLE_QUOTE__ parameters"'
        r = mm.parse_record_lines([line])[0]
        self.assertEqual(len(r.fields), 4)
        self.assertIn("__DOUBLE_QUOTE__", r.fields[3])
        self.assertIn(", y, and", r.fields[3])

    def test_unbalanced_quote_is_error(self):
        with self.assertRaises(mm.ParseError):
            mm.parse_record_lines(['"a", "b'])

    def test_empty_type_is_error(self):
        with self.assertRaises(mm.ParseError):
            mm.parse_record_lines(['"", "b"'])

    def test_conflict_marker_is_error(self):
        with self.assertRaises(mm.ParseError):
            mm.MapsFile(text=self.text + "<<<<<<< ours\n")
        with self.assertRaises(mm.ParseError):
            mm.MapsFile(text=self.text.replace("\n", "\r\n") + "=======\r\n")

    def test_corrupt_lines_rejected(self):
        bad = ['"a", "b" junk', '"a", "b",', '"a", "b"c"d", "e"', '"a", "b""c"', '"a" "b"', '"a", b', 'x"a", "b"']
        for line in bad:
            with self.assertRaises(mm.ParseError):
                mm.parse_record_lines([line])
        good = ['"a"', '"a", ""', '  "a" ,  "b"  ', '"a","b, c",  "d"', '"a", "__DOUBLE_QUOTE__x__DOUBLE_QUOTE__"']
        for line in good:
            mm.parse_record_lines([line])

    def test_trailing_newline_tracked(self):
        self.assertTrue(self.mf.trailing_newline)
        self.assertFalse(mm.MapsFile(text=self.text.rstrip("\n")).trailing_newline)

    def test_crlf_lines_kept_verbatim(self):
        crlf = self.text.replace("\n", "\r\n")
        mf = mm.MapsFile(text=crlf)
        self.assertEqual(mf.nrecords, self.mf.nrecords)
        r = mf.blocks["parameter_value"][0]
        self.assertTrue(r.line.endswith("\r"))
        self.assertEqual(r.fields, self.mf.blocks["parameter_value"][0].fields)
        res = merge_texts(crlf, crlf, crlf)
        self.assertEqual(res.text, crlf)

    def test_non_ascii_bytes_roundtrip(self):
        if mm.PY2:
            weird = '"history", "2008-06-12  19:41:40", "cward", "caf\xc3\xa9 \xff"'
        else:
            weird = '"history", "2008-06-12  19:41:40", "cward", "caf\xc3\xa9 \xff"'
        text = self.text.replace('"checkers", "38", "Objects"', weird)
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "x.MAPS")
            mm.write_text(p, text)
            self.assertEqual(mm.read_text(p), text)
            with open(p, "rb") as fh:
                raw = fh.read()
            self.assertIn(b"caf\xc3\xa9 \xff" if not mm.PY2 else "caf\xc3\xa9 \xff", raw)
        finally:
            shutil.rmtree(d)

    def test_dominant_sep(self):
        self.assertEqual(self.mf.dominant_sep(), ", ")

    def test_preamble_and_trailer_lines(self):
        text = gen_maps.HEADER + "\n# a comment\n\n" + "\n".join(lines_of(self.text)[1:]) + "\n# tail\n"
        mf = mm.MapsFile(text=text)
        self.assertEqual(mf.preamble, ["# a comment", ""])
        self.assertEqual(mf.trailers["variable_properties"], ["", "# tail"])
        res = merge_texts(text, text, text)
        self.assertEqual(res.text, text)


class TestKeys(unittest.TestCase):
    def setUp(self):
        self.cfg = mm.load_config(os.devnull)
        self.mf = mm.MapsFile(text=gen_maps.gen())

    def test_configured_widths(self):
        self.assertEqual(mm.key_width(self.cfg, "parameter_value", 8), 6)
        self.assertEqual(mm.key_width(self.cfg, "history", 4), 3)
        self.assertEqual(mm.key_width(self.cfg, "base_network_connection", 5), 4)
        self.assertEqual(mm.key_width(self.cfg, "unknown_type", 5), 3)   # all_but_last
        self.assertEqual(mm.key_width(self.cfg, "unknown_type", 2), 0)
        self.assertEqual(mm.key_width(self.cfg, "control_mode", 4), 2)

    def test_widening_when_not_unique(self):
        cfg = mm.load_config(os.devnull)
        cfg["keys"]["conf_group_object"] = 1            # (group, object): group alone is not unique
        recs = self.mf.blocks["conf_group_object"]
        n, full, widened = mm.resolve_width(cfg, "conf_group_object", [recs])
        self.assertTrue(widened)
        self.assertEqual(n, 2)
        # with the shipped default ("all") nothing needs widening
        n, full, widened = mm.resolve_width(self.cfg, "conf_group_object", [recs])
        self.assertFalse(widened)

    def test_identical_duplicate_records_survive(self):
        text = gen_maps.gen()
        l = mm.make_line(("checkers", "38", "Objects"))
        text2 = replace_line(text, l, l + "\n" + l)
        res = merge_texts(text2, text2, text2)
        self.assertEqual(res.text.count(l), 2)

    def test_config_file_overrides(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "c.json")
            with open(p, "w") as fh:
                fh.write('{"keys": {"parameter_value": 2, "newtype": "all"}, "monotonic": []}')
            cfg = mm.load_config(p)
            self.assertEqual(cfg["keys"]["parameter_value"], 2)
            self.assertEqual(cfg["keys"]["newtype"], "all")
            self.assertEqual(cfg["keys"]["history"], "all")
            self.assertEqual(cfg["monotonic"], [])
        finally:
            shutil.rmtree(d)


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.base = gen_maps.gen()
        self.mf = mm.MapsFile(text=self.base)
        self.cfg = mm.load_config(os.devnull)

    def pv(self, i):
        return self.mf.blocks["parameter_value"][i]

    def edited(self, rec, value):
        f = list(rec.fields)
        f[-1] = value
        return mm.make_line(tuple(f)), tuple(f)

    def test_identity(self):
        res = merge_texts(self.base, self.base, self.base)
        self.assertEqual(res.text, self.base)
        self.assertEqual(res.ours_changes, 0)
        self.assertEqual(res.theirs_changes, 0)

    def test_disjoint_edits_in_same_type(self):
        l1, f1 = self.edited(self.pv(3), "111")
        l2, f2 = self.edited(self.pv(40), "222")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, l1),
                          replace_line(self.base, self.pv(40).line, l2))
        self.assertEqual(res.conflicts, [])
        self.assertIn(l1, lines_of(res.text))
        self.assertIn(l2, lines_of(res.text))
        self.assertEqual(res.ours_changes, 1)
        self.assertEqual(res.theirs_changes, 1)
        expect = replace_line(replace_line(self.base, self.pv(3).line, l1), self.pv(40).line, l2)
        self.assertEqual(res.text, expect)   # sorted position unchanged, so byte-identical to expectation

    def test_disjoint_edits_in_different_types(self):
        cm = self.mf.blocks["control_mode"][2]
        lo, _ = self.edited(cm, "Scan")
        op = self.mf.blocks["open_property"][5]
        lt, _ = self.edited(op, "TRUE")
        res = merge_texts(self.base, replace_line(self.base, cm.line, lo), replace_line(self.base, op.line, lt))
        self.assertEqual(res.conflicts, [])
        self.assertIn(lo, lines_of(res.text))
        self.assertIn(lt, lines_of(res.text))

    def test_conflict_same_record(self):
        lo, _ = self.edited(self.pv(3), "111")
        lt, _ = self.edited(self.pv(3), "222")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, lo),
                          replace_line(self.base, self.pv(3).line, lt), marker_size=9)
        self.assertEqual(len(res.conflicts), 1)
        ls = lines_of(res.text)
        i = ls.index("<<<<<<<<< ours")
        self.assertEqual(ls[i + 1], lo)
        self.assertEqual(ls[i + 2], "||||||||| base")
        self.assertEqual(ls[i + 3], self.pv(3).line)
        self.assertEqual(ls[i + 4], "=========")
        self.assertEqual(ls[i + 5], lt)
        self.assertEqual(ls[i + 6], ">>>>>>>>> theirs")
        desc = mm.describe_conflict(*res.conflicts[0])
        self.assertIn("parameter_value", desc)
        self.assertIn('ours="111"', desc)
        self.assertIn('theirs="222"', desc)

    def test_conflict_position_is_sorted(self):
        lo, _ = self.edited(self.pv(3), "111")
        lt, _ = self.edited(self.pv(3), "222")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, lo),
                          replace_line(self.base, self.pv(3).line, lt))
        ls = lines_of(res.text)
        i = ls.index("<<<<<<< ours")
        self.assertEqual(ls[i - 1], self.pv(2).line)
        self.assertEqual(ls[i + 7], self.pv(4).line)

    def test_delete_vs_modify_conflict(self):
        lt, _ = self.edited(self.pv(3), "222")
        res = merge_texts(self.base, remove_line(self.base, self.pv(3).line),
                          replace_line(self.base, self.pv(3).line, lt))
        self.assertEqual(len(res.conflicts), 1)
        self.assertIn("<deleted>", mm.describe_conflict(*res.conflicts[0]))

    def test_delete_both_sides(self):
        res = merge_texts(self.base, remove_line(self.base, self.pv(3).line),
                          remove_line(self.base, self.pv(3).line))
        self.assertEqual(res.conflicts, [])
        self.assertNotIn(self.pv(3).line, lines_of(res.text))
        self.assertEqual(res.text, remove_line(self.base, self.pv(3).line))

    def test_whitespace_only_change_ignored(self):
        f = self.pv(3).fields
        respaced = mm.make_line(f, ",")
        lo, _ = self.edited(self.pv(3), "111")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, lo),
                          replace_line(self.base, self.pv(3).line, respaced))
        self.assertEqual(res.conflicts, [])
        self.assertIn(lo, lines_of(res.text))
        # respacing alone on one side: taken as-is (ours line preferred when equal)
        res = merge_texts(self.base, self.base, replace_line(self.base, self.pv(3).line, respaced))
        self.assertEqual(res.conflicts, [])
        self.assertEqual(res.theirs_changes, 0)
        self.assertIn(self.pv(3).line, lines_of(res.text))   # equal fields: ours (=base) line kept

    def test_add_new_records_both_sides_sorted_in(self):
        o = self.mf.blocks["base_network_object"][0]
        new_o = mm.make_line(("base_network_object", "ActSys.Obj_000_A"))
        new_t = mm.make_line(("base_network_object", "ActSys.Obj_000_B"))
        res = merge_texts(self.base, replace_line(self.base, o.line, o.line + "\n" + new_o),
                          replace_line(self.base, o.line, new_t + "\n" + o.line))
        self.assertEqual(res.conflicts, [])
        out = mm.MapsFile(text=res.text)
        names = [r.fields[1] for r in out.blocks["base_network_object"]]
        self.assertEqual(names, sorted(names))
        self.assertIn("ActSys.Obj_000_A", names)
        self.assertIn("ActSys.Obj_000_B", names)

    def test_unsorted_block_keeps_base_order_and_appends(self):
        base = gen_maps.gen(unsorted_types=("open_property",))
        mf = mm.MapsFile(text=base)
        self.assertFalse(mm.block_sorted(mf.blocks["open_property"]))
        last = mf.blocks["open_property"][-1]
        new = mm.make_line(("open_property", "ZZZ.new", "quote_EAP", "", "", "", "Default", "1"))
        first = mf.blocks["open_property"][0]
        ours = replace_line(base, first.line, new + "\n" + first.line)   # inserted at the top
        res = merge_texts(base, ours, base)
        out = mm.MapsFile(text=res.text)
        order = [r.line for r in out.blocks["open_property"]]
        self.assertEqual(order[:-1], [r.line for r in mf.blocks["open_property"]])
        self.assertEqual(order[-1], new)
        # identity on an unsorted file is byte-identical too
        self.assertEqual(merge_texts(base, base, base).text, base)

    def test_new_type_on_one_side_goes_in_sorted_position(self):
        new = mm.make_line(("aaa_new_type", "x", "y"))
        ours = replace_line(self.base, gen_maps.HEADER, gen_maps.HEADER + "\n" + new)
        res = merge_texts(self.base, ours, self.base)
        ls = lines_of(res.text)
        self.assertEqual(ls[1], new)
        new2 = mm.make_line(("zzz_new_type", "x"))
        theirs = self.base.rstrip("\n") + "\n" + new2 + "\n"
        res = merge_texts(self.base, ours, theirs)
        ls = lines_of(res.text)
        self.assertEqual(ls[1], new)
        self.assertEqual(ls[-2], new2)
        self.assertEqual(res.conflicts, [])

    def test_whole_type_removed_on_one_side(self):
        ls = [l for l in lines_of(self.base) if not l.startswith('"checkers"')]
        ours = "\n".join(ls)
        res = merge_texts(self.base, ours, self.base)
        self.assertEqual(res.conflicts, [])
        self.assertNotIn('"checkers"', res.text)

    def test_history_union_and_order(self):
        h = self.mf.blocks["history"][-1]
        ho = mm.make_line(("history", "2020-01-01  10:00:00", "me", "ours, with comma"))
        ht = mm.make_line(("history", "2019-01-01  10:00:00", "you", "theirs __DOUBLE_QUOTE__x__DOUBLE_QUOTE__"))
        res = merge_texts(self.base, replace_line(self.base, h.line, h.line + "\n" + ho),
                          replace_line(self.base, h.line, h.line + "\n" + ht))
        self.assertEqual(res.conflicts, [])
        out = mm.MapsFile(text=res.text)
        stamps = [r.fields[1] for r in out.blocks["history"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertIn(ho, lines_of(res.text))
        self.assertIn(ht, lines_of(res.text))
        self.assertEqual(len(out.blocks["history"]), len(self.mf.blocks["history"]) + 2)

    def test_identical_history_entry_both_sides_once(self):
        h = self.mf.blocks["history"][-1]
        ho = mm.make_line(("history", "2020-01-01  10:00:00", "me", "same"))
        ours = replace_line(self.base, h.line, h.line + "\n" + ho)
        res = merge_texts(self.base, ours, ours)
        self.assertEqual(res.text.count(ho), 1)

    def test_version_counter_max(self):
        v = rec_by_prefix(self.mf, "network_properties", "version_number")
        lo = mm.make_line(("network_properties", "version_number", "18"))
        lt = mm.make_line(("network_properties", "version_number", "25"))
        res = merge_texts(self.base, replace_line(self.base, v.line, lo), replace_line(self.base, v.line, lt))
        self.assertEqual(res.conflicts, [])
        self.assertIn(lt, lines_of(res.text))
        self.assertNotIn(lo, lines_of(res.text))
        # non numeric values of a monotonic type still conflict
        n = rec_by_prefix(self.mf, "network_properties", "name")
        res = merge_texts(self.base, replace_line(self.base, n.line, mm.make_line(("network_properties", "name", "A"))),
                          replace_line(self.base, n.line, mm.make_line(("network_properties", "name", "B"))))
        self.assertEqual(len(res.conflicts), 1)

    def test_header_version_max_and_one_sided(self):
        ho = gen_maps.HEADER.replace("71219", "71220")
        ht = gen_maps.HEADER.replace("71219", "71300")
        res = merge_texts(self.base, replace_line(self.base, gen_maps.HEADER, ho),
                          replace_line(self.base, gen_maps.HEADER, ht))
        self.assertEqual(lines_of(res.text)[0], ht)
        res = merge_texts(self.base, replace_line(self.base, gen_maps.HEADER, ho), self.base)
        self.assertEqual(lines_of(res.text)[0], ho)
        res = merge_texts(self.base, self.base, replace_line(self.base, gen_maps.HEADER, ho))
        self.assertEqual(lines_of(res.text)[0], ho)

    def test_rename_on_one_side(self):
        o = rec_by_prefix(self.mf, "base_network_object", "ActSys.Obj_000")
        renamed = mm.make_line(("base_network_object", "ActSys.Obj_000_R"))
        res = merge_texts(self.base, replace_line(self.base, o.line, renamed), self.base)
        self.assertEqual(res.conflicts, [])
        self.assertIn(renamed, lines_of(res.text))
        self.assertNotIn(o.line, lines_of(res.text))

    def test_rewrite_guard_same_column_both_sides(self):
        # key column changed differently on both sides: same record rewritten twice -> conflict
        o = rec_by_prefix(self.mf, "object_type_swid", "ActSys.Obj_000")
        f = list(o.fields)
        fo = list(f); fo[1] = "ActSys.Obj_000_X"; ro = mm.make_line(tuple(fo))
        ft = list(f); ft[1] = "ActSys.Obj_000_Y"; rt = mm.make_line(tuple(ft))
        res = merge_texts(self.base, replace_line(self.base, o.line, ro), replace_line(self.base, o.line, rt))
        self.assertEqual(len(res.conflicts), 1)
        self.assertIn("<<<<<<< ours", res.text)
        self.assertIn(ro, lines_of(res.text))
        self.assertIn(rt, lines_of(res.text))
        self.assertIn("ActSys.Obj_000_X", mm.describe_conflict(*res.conflicts[0]))
        # ... but the same rewrite on both sides is fine
        res = merge_texts(self.base, replace_line(self.base, o.line, ro), replace_line(self.base, o.line, ro))
        self.assertEqual(res.conflicts, [])
        self.assertEqual(res.text.count(ro), 1)
        # ... and a rename on one side plus an unrelated addition on the other is fine
        f2 = list(f); f2[1] = "ZZZ.brand_new"; add = mm.make_line(tuple(f2))
        res = merge_texts(self.base, replace_line(self.base, o.line, ro),
                          replace_line(self.base, o.line, o.line + "\n" + add))
        self.assertEqual(res.conflicts, [])
        self.assertIn(ro, lines_of(res.text))
        self.assertIn(add, lines_of(res.text))

    def test_rewrite_guard_different_columns_is_a_conflict(self):
        # the same connection edited in different columns on each side
        c = self.mf.blocks["base_network_connection"][0]
        f = list(c.fields)
        fo = list(f); fo[2] = "ODT_X"
        ft = list(f); ft[4] = "IDT_Y"
        res = merge_texts(self.base, replace_line(self.base, c.line, mm.make_line(tuple(fo))),
                          replace_line(self.base, c.line, mm.make_line(tuple(ft))))
        self.assertEqual(len(res.conflicts), 1)
        self.assertIn(mm.make_line(tuple(fo)), lines_of(res.text))
        self.assertIn(mm.make_line(tuple(ft)), lines_of(res.text))
        self.assertNotIn(c.line, [l for l in lines_of(res.text) if not l.startswith("|||||||")][:0])

    def test_edit_vs_delete_under_set_semantics(self):
        c = self.mf.blocks["base_network_connection"][0]
        f = list(c.fields); f[2] = "ODT_EDITED"
        res = merge_texts(self.base, replace_line(self.base, c.line, mm.make_line(tuple(f))),
                          remove_line(self.base, c.line))
        self.assertEqual(len(res.conflicts), 1)
        desc = mm.describe_conflict(*res.conflicts[0])
        self.assertIn("ODT_EDITED", desc)
        self.assertIn("<deleted>", desc)
        # and mirrored
        res = merge_texts(self.base, remove_line(self.base, c.line),
                          replace_line(self.base, c.line, mm.make_line(tuple(f))))
        self.assertEqual(len(res.conflicts), 1)

    def test_widening_does_not_hide_edit_vs_delete(self):
        # a stray duplicate row in theirs forces the key to widen; an edit on ours
        # versus a delete on theirs must still be a conflict
        pv = self.pv(3)
        lo, _ = self.edited(pv, "9")
        dup = self.pv(10)
        f = list(dup.fields); f[-1] = "3"
        theirs = remove_line(self.base, pv.line)
        theirs = replace_line(theirs, dup.line, dup.line + "\n" + mm.make_line(tuple(f)))
        res = merge_texts(self.base, replace_line(self.base, pv.line, lo), theirs)
        self.assertTrue(res.widened)
        self.assertEqual(len(res.conflicts), 1)

    def test_two_column_types_exempt_from_guard(self):
        # documented: a one-data-column record has no way to tell a rename from a new record
        o = rec_by_prefix(self.mf, "base_network_object", "ActSys.Obj_000")
        ro = mm.make_line(("base_network_object", "ActSys.Obj_000_X"))
        rt = mm.make_line(("base_network_object", "ActSys.Obj_000_Y"))
        res = merge_texts(self.base, replace_line(self.base, o.line, ro), replace_line(self.base, o.line, rt))
        self.assertEqual(res.conflicts, [])
        self.assertIn(ro, lines_of(res.text))
        self.assertIn(rt, lines_of(res.text))

    def test_guard_keeps_duplicate_rows(self):
        o = rec_by_prefix(self.mf, "object_type_swid", "ActSys.Obj_000")
        f = list(o.fields)
        fo = list(f); fo[1] = "ActSys.Obj_000_X"; ro = mm.make_line(tuple(fo))
        ft = list(f); ft[1] = "ActSys.Obj_000_Y"; rt = mm.make_line(tuple(ft))
        ours = replace_line(self.base, o.line, ro + "\n" + ro)     # duplicated row
        res = merge_texts(self.base, ours, replace_line(self.base, o.line, rt))
        self.assertEqual(len(res.conflicts), 1)
        self.assertEqual(res.text.count(ro), 2)

    def test_monotonic_only_for_named_counter(self):
        n = rec_by_prefix(self.mf, "network_properties", "name")
        # a numeric value for another network_properties record must still conflict
        lo = mm.make_line(("network_properties", "name", "100"))
        lt = mm.make_line(("network_properties", "name", "200"))
        res = merge_texts(self.base, replace_line(self.base, n.line, lo), replace_line(self.base, n.line, lt))
        self.assertEqual(len(res.conflicts), 1)
        # legacy list form of the config still works
        cfg = mm.load_config(os.devnull)
        cfg["monotonic"] = ["network_properties:name"]
        res = merge_texts(self.base, replace_line(self.base, n.line, lo), replace_line(self.base, n.line, lt), cfg=cfg)
        self.assertEqual(res.conflicts, [])
        self.assertIn(lt, lines_of(res.text))

    def test_mixed_arity_across_sides_conflicts(self):
        base = gen_maps.HEADER + '\n"t", "a", "b", "v"\n'
        ours = gen_maps.HEADER + '\n"t", "a", "b", "v2"\n'
        theirs = gen_maps.HEADER + '\n"t", "a", "b", "c", "v"\n'
        res = merge_texts(base, ours, theirs)
        self.assertEqual(len(res.conflicts), 1)
        self.assertTrue(any("between 4 and 5 fields" in x for x in res.notes))

    def test_over_keyed_type_still_conflicts(self):
        # configure parameter_value with the whole record as key (over-keyed):
        # the guard must still catch a double rewrite of the value column
        cfg = mm.load_config(os.devnull)
        cfg["keys"]["parameter_value"] = "all"
        lo, _ = self.edited(self.pv(3), "111")
        lt, _ = self.edited(self.pv(3), "222")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, lo),
                          replace_line(self.base, self.pv(3).line, lt), cfg=cfg)
        self.assertEqual(len(res.conflicts), 1)

    def test_add_history_entry_option(self):
        cfg = mm.load_config(os.devnull)
        cfg["add_history_entry"] = True
        lo, _ = self.edited(self.pv(3), "111")
        res = merge_texts(self.base, replace_line(self.base, self.pv(3).line, lo), self.base, cfg=cfg)
        out = mm.MapsFile(text=res.text)
        last = out.blocks["history"][-1]
        self.assertEqual(len(last.fields), 4)
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}  \d{2}:\d{2}:\d{2}$", last.fields[1]), last.fields[1])
        self.assertIn("maps_merge", last.fields[3])
        self.assertTrue(mm.block_sorted(out.blocks["history"]))
        # not added when nothing changed, nor on conflict
        res = merge_texts(self.base, self.base, self.base, cfg=cfg)
        self.assertEqual(res.text, self.base)

    def test_symmetry_and_determinism(self):
        lo, _ = self.edited(self.pv(3), "111")
        lt, _ = self.edited(self.pv(40), "222")
        ours = replace_line(self.base, self.pv(3).line, lo)
        theirs = replace_line(self.base, self.pv(40).line, lt)
        a = merge_texts(self.base, ours, theirs).text
        b = merge_texts(self.base, theirs, ours).text
        self.assertEqual(a, b)
        self.assertEqual(a, merge_texts(self.base, ours, theirs).text)

    def test_empty_base_file(self):
        base = gen_maps.HEADER + "\n"
        res = merge_texts(base, self.base, base)
        self.assertEqual(res.conflicts, [])
        self.assertEqual(res.text, self.base)

    def test_no_trailing_newline_preserved(self):
        base = self.base.rstrip("\n")
        res = merge_texts(base, base, base)
        self.assertEqual(res.text, base)


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.base = gen_maps.gen()
        self.mf = mm.MapsFile(text=self.base)

    def tearDown(self):
        shutil.rmtree(self.d)

    def path(self, name, text):
        p = os.path.join(self.d, name)
        mm.write_text(p, text)
        return p

    def run_cli(self, *args):
        proc = subprocess.Popen([PY, SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
        return proc.returncode, out.decode("latin-1"), err.decode("latin-1")

    def test_merge_exit_codes_and_messages(self):
        pv = self.mf.blocks["parameter_value"][3]
        f = list(pv.fields); f[-1] = "111"
        lo = mm.make_line(tuple(f))
        f[-1] = "222"
        lt = mm.make_line(tuple(f))
        b = self.path("base", self.base)
        o = self.path("ours", replace_line(self.base, pv.line, lo))
        t = self.path("theirs", replace_line(self.base, pv.line, lt))
        code, out, err = self.run_cli("merge", b, o, t, "-P", "RQxSV.MAPS")
        self.assertEqual(code, 1)
        self.assertIn("RQxSV.MAPS: CONFLICT parameter_value", err)
        self.assertIn("1 conflict(s)", err)
        self.assertIn("<<<<<<< ours", mm.read_text(o))
        o2 = self.path("ours2", replace_line(self.base, pv.line, lo))
        code, out, err = self.run_cli("merge", b, o2, b, "-o", os.path.join(self.d, "out"))
        self.assertEqual(code, 0)
        self.assertEqual(mm.read_text(os.path.join(self.d, "out")), replace_line(self.base, pv.line, lo))
        self.assertEqual(mm.read_text(o2), replace_line(self.base, pv.line, lo))  # -o leaves OURS alone

    def test_merge_check_cmd(self):
        b = self.path("base", self.base)
        o = self.path("ours", self.base)
        code, out, err = self.run_cli("merge", b, o, b, "--check-cmd", "test -s {file}")
        self.assertEqual(code, 0)
        self.assertIn("check command passed", err)
        code, out, err = self.run_cli("merge", b, o, b, "--check-cmd", "sh -c 'echo bad {file}; exit 3'")
        self.assertEqual(code, 1)
        self.assertIn("check command failed (exit 3)", err)
        self.assertIn("bad ", err)

    def test_diff_output(self):
        p = self.path("x.MAPS", self.base)
        code, out, err = self.run_cli("diff", p)
        self.assertEqual(code, 0)
        ls = out.rstrip("\n").split("\n")
        self.assertEqual(ls[0], gen_maps.HEADER.strip())
        self.assertEqual(len(ls), self.mf.nrecords + 1)
        self.assertTrue(all('", "' in l or l.count('"') == 2 for l in ls[1:]))
        types = [mm.parse_record_lines([l])[0].fields[0] for l in ls[1:]]
        self.assertEqual(types, sorted(types))

    def test_check_detects_problems(self):
        p = self.path("x.MAPS", self.base)
        code, out, err = self.run_cli("check", p)
        self.assertEqual(code, 0, out)
        p2 = self.path("bad.MAPS", self.base + '"parameter_value", "only", "three"\n')
        code, out, err = self.run_cli("check", p2)
        self.assertEqual(code, 1)
        self.assertIn("inconsistent field count", out)
        p3 = self.path("markers.MAPS", self.base + "=======\n")
        code, out, err = self.run_cli("check", p3)
        self.assertEqual(code, 1)
        self.assertIn("conflict marker", out)
        p4 = self.path("nohdr.MAPS", "\n".join(lines_of(self.base)[1:]))
        code, out, err = self.run_cli("check", p4)
        self.assertEqual(code, 1)
        self.assertIn("header", out)

    def test_inspect_and_write_config(self):
        p = self.path("x.MAPS", self.base)
        cfgp = os.path.join(self.d, "keys.json")
        code, out, err = self.run_cli("inspect", p, "--write-config", cfgp)
        self.assertEqual(code, 0, err)
        self.assertIn("parameter_value", out)
        self.assertIn("version 71219", out)
        import json
        with open(cfgp) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["keys"]["parameter_value"], 6)
        self.assertEqual(cfg["keys"]["conf_group_object"], 2)
        # the written config is accepted back
        code, out, err = self.run_cli("check", p, "--config", cfgp)
        self.assertEqual(code, 0)
        self.assertNotIn("widened", out)
        code, out, err = self.run_cli("check", p)
        self.assertNotIn("widened", out)

    def test_selftest_passes(self):
        p = self.path("x.MAPS", self.base)
        before = mm.read_text(p)
        code, out, err = self.run_cli("selftest", p)
        self.assertEqual(code, 0, out + err)
        self.assertIn("20 of 20 checks passed", out)
        self.assertEqual(mm.read_text(p), before)

    def test_setup_prints_lines(self):
        code, out, err = self.run_cli("setup", "--matlabroot", "/opt/MATLAB/R2024b")
        self.assertEqual(code, 0)
        self.assertIn("*.MAPS merge=maps diff=maps", out)
        self.assertIn("git config merge.maps.driver", out)
        self.assertIn("git config merge.maps.recursive 'binary'", out)
        self.assertIn("mlAutoMerge", out)
        self.assertIn("%O %A %B %A", out)

    def test_version_flag(self):
        code, out, err = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn("maps_merge", out + err)


def git(*args, **kw):
    cwd = kw.get("cwd")
    proc = subprocess.Popen(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = proc.communicate()[0].decode("latin-1")
    return proc.returncode, out


class TestGitEndToEnd(unittest.TestCase):
    """The real thing: git merge with the driver registered via setup --apply."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.repo = os.path.join(self.d, "repo")
        os.makedirs(self.repo)
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "tester", cwd=self.repo)
        self.base = gen_maps.gen()
        self.mf = mm.MapsFile(text=self.base)
        self.f = os.path.join(self.repo, "RQxSV.MAPS")
        mm.write_text(self.f, self.base)
        with open(os.path.join(self.repo, ".gitattributes"), "w") as fh:
            fh.write("*.MAPS binary\n")     # what the company repo has today
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "base", cwd=self.repo)
        # the private trial: setup --apply --local-attributes
        proc = subprocess.Popen([PY, SCRIPT, "setup", "--apply", "--local-attributes"], cwd=self.repo,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.setup_out = proc.communicate()[0].decode("latin-1")
        self.assertEqual(proc.returncode, 0, self.setup_out)

    def tearDown(self):
        shutil.rmtree(self.d)

    def commit_branch(self, name, text):
        git("checkout", "-q", "-b", name, "main", cwd=self.repo)
        mm.write_text(self.f, text)
        git("commit", "-q", "-am", name, cwd=self.repo)

    def edited(self, i, value):
        r = self.mf.blocks["parameter_value"][i]
        f = list(r.fields); f[-1] = value
        return r.line, mm.make_line(tuple(f))

    def test_setup_wrote_config_and_attributes(self):
        code, out = git("config", "merge.maps.driver", cwd=self.repo)
        self.assertEqual(code, 0)
        self.assertIn("maps_merge.py", out)
        self.assertIn("merge %O %A %B -L %L -P %P", out)
        code, out = git("config", "merge.maps.recursive", cwd=self.repo)
        self.assertEqual(out.strip(), "binary")
        attr = mm.read_text(os.path.join(self.repo, ".git", "info", "attributes"))
        self.assertIn("*.MAPS merge=maps diff=maps", attr)
        # git resolves the attribute for the file to our driver, overriding the committed 'binary'
        code, out = git("check-attr", "merge", "diff", "--", "RQxSV.MAPS", cwd=self.repo)
        self.assertIn("merge: maps", out)
        self.assertIn("diff: maps", out)

    def test_clean_merge_on_pull_like_merge(self):
        old1, new1 = self.edited(3, "111")
        old2, new2 = self.edited(40, "222")
        self.commit_branch("alice", replace_line(self.base, old1, new1))
        self.commit_branch("bob", replace_line(self.base, old2, new2))
        code, out = git("merge", "--no-edit", "alice", cwd=self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("1 change(s) from ours, 1 from theirs, 0 conflict(s)", out)
        merged = mm.read_text(self.f)
        self.assertEqual(merged, replace_line(replace_line(self.base, old1, new1), old2, new2))
        code, out = git("status", "--porcelain", cwd=self.repo)
        self.assertEqual(out.strip(), "")
        # the merge commit exists and has two parents
        code, out = git("log", "--pretty=%P", "-1", cwd=self.repo)
        self.assertEqual(len(out.split()), 2)

    def test_conflict_merge(self):
        old1, new1 = self.edited(3, "111")
        _, new1b = self.edited(3, "222")
        self.commit_branch("alice", replace_line(self.base, old1, new1))
        self.commit_branch("bob", replace_line(self.base, old1, new1b))
        code, out = git("merge", "--no-edit", "alice", cwd=self.repo)
        self.assertNotEqual(code, 0)
        self.assertIn("CONFLICT parameter_value", out)
        self.assertIn("Automatic merge failed", out)
        code, out = git("status", "--porcelain", cwd=self.repo)
        self.assertIn("UU RQxSV.MAPS", out)
        text = mm.read_text(self.f)
        self.assertIn("<<<<<<< ours", text)
        self.assertIn(new1, text)
        self.assertIn(new1b, text)
        # resolve like an engineer would: keep alice's line, drop markers
        resolved = []
        skip = False
        for l in lines_of(text):
            if l.startswith("<<<<<<< ours"):
                skip = "ours"; continue
            if l.startswith("||||||| base"):
                skip = "base"; continue
            if l.startswith("======="):
                skip = "theirs"; continue
            if l.startswith(">>>>>>> theirs"):
                skip = False; continue
            if skip in ("base", "ours"):
                continue
            resolved.append(l)
        mm.write_text(self.f, "\n".join(resolved))
        code, out = git("add", "RQxSV.MAPS", cwd=self.repo)
        code, out = git("commit", "-q", "-m", "resolved", cwd=self.repo)
        self.assertEqual(code, 0, out)
        final = mm.read_text(self.f)
        self.assertEqual(final, replace_line(self.base, old1, new1))   # theirs == alice == new1

    def test_textconv_diff_is_readable(self):
        old1, new1 = self.edited(3, "111")
        self.commit_branch("alice", replace_line(self.base, old1, new1))
        code, out = git("diff", "main", "alice", "--", "RQxSV.MAPS", cwd=self.repo)
        self.assertEqual(code, 0)
        self.assertNotIn("Binary files", out)
        self.assertIn("-" + old1, out)
        self.assertIn("+" + new1, out)

    def test_criss_cross_merge(self):
        # A and B both changed record 3 differently, each merged the other and
        # resolved to the same value, B then changed record 40. Merging B into A
        # has two merge bases; git merges the bases first (with the "binary"
        # recursive driver), then the branches. Must be clean.
        old3, a3 = self.edited(3, "1")
        _, b3 = self.edited(3, "2")
        _, both3 = self.edited(3, "3")
        old40, b40 = self.edited(40, "9")
        self.commit_branch("A", replace_line(self.base, old3, a3))
        self.commit_branch("B", replace_line(self.base, old3, b3))
        git("checkout", "-q", "A", cwd=self.repo)
        code, out = git("merge", "--no-edit", "B", cwd=self.repo)
        self.assertNotEqual(code, 0)
        mm.write_text(self.f, replace_line(self.base, old3, both3))
        git("add", "RQxSV.MAPS", cwd=self.repo)
        git("commit", "-q", "-m", "A resolves", cwd=self.repo)
        git("checkout", "-q", "B", cwd=self.repo)
        code, out = git("merge", "--no-edit", "A~1", cwd=self.repo)   # the original A commit
        self.assertNotEqual(code, 0)
        mm.write_text(self.f, replace_line(self.base, old3, both3))
        git("add", "RQxSV.MAPS", cwd=self.repo)
        git("commit", "-q", "-m", "B resolves", cwd=self.repo)
        mm.write_text(self.f, replace_line(replace_line(self.base, old3, both3), old40, b40))
        git("commit", "-q", "-am", "B edits 40", cwd=self.repo)
        git("checkout", "-q", "A", cwd=self.repo)
        code, out = git("merge", "--no-edit", "B", cwd=self.repo)
        self.assertEqual(code, 0, out)
        self.assertNotIn("cannot merge", out)
        final = mm.read_text(self.f)
        self.assertEqual(final, replace_line(replace_line(self.base, old3, both3), old40, b40))

    def test_rebase_uses_driver_too(self):
        old1, new1 = self.edited(3, "111")
        old2, new2 = self.edited(40, "222")
        self.commit_branch("alice", replace_line(self.base, old1, new1))
        self.commit_branch("bob", replace_line(self.base, old2, new2))
        code, out = git("rebase", "alice", cwd=self.repo)
        self.assertEqual(code, 0, out)
        self.assertEqual(mm.read_text(self.f), replace_line(replace_line(self.base, old1, new1), old2, new2))


class TestPerformance(unittest.TestCase):
    def test_large_file_merge_time(self):
        base = gen_maps.gen(n_objects=400, params_per_object=45, history=650)
        mf = mm.MapsFile(text=base)
        self.assertGreater(mf.nrecords, 70000)
        pv = mf.blocks["parameter_value"]
        r1, r2 = pv[10], pv[-10]
        f = list(r1.fields); f[-1] = "111"; l1 = mm.make_line(tuple(f))
        f = list(r2.fields); f[-1] = "222"; l2 = mm.make_line(tuple(f))
        ours = replace_line(base, r1.line, l1)
        theirs = replace_line(base, r2.line, l2)
        t0 = time.time()
        res = merge_texts(base, ours, theirs)
        dt = time.time() - t0
        self.assertEqual(res.conflicts, [])
        self.assertIn(l1, lines_of(res.text))
        self.assertIn(l2, lines_of(res.text))
        self.assertLess(dt, 30, "merge of %d records took %.1fs" % (mf.nrecords, dt))
        print("\n  [perf] %d records x3 merged in %.2fs" % (mf.nrecords, dt))


# ---------------------------------------------------------------------------
# crosscheck
# ---------------------------------------------------------------------------

SYS_ROOT = """<?xml version="1.0" encoding="utf-8"?>
<System>
  <P Name="Location">[0, 0, 100, 100]</P>
  <Block BlockType="SubSystem" Name="ActSys" SID="2">
    <P Name="Ports">[1, 1]</P>
    <System Ref="system_2"/>
  </Block>
  <Block BlockType="SubSystem" Name="MeasSys" SID="3">
    <System Ref="system_3"/>
  </Block>
  <Block BlockType="Terminator" Name="Term1" SID="4"/>
  <Line>
    <P Name="Src">2#out:1</P>
    <P Name="Dst">3#in:1</P>
    <Branch>
      <P Name="Dst">4#in:1</P>
    </Branch>
  </Line>
</System>
"""

SYS_2 = """<?xml version="1.0" encoding="utf-8"?>
<System>
  <Block BlockType="Inport" Name="In1" SID="5"/>
  <Block BlockType="Gain" Name="Obj_000" SID="6"><P Name="Gain">2</P></Block>
  <Block BlockType="SubSystem" Name="PFC_Ramp" SID="7"><System Ref="system_7"/></Block>
  <Block BlockType="Outport" Name="Out1" SID="8"/>
  <Line><P Name="Src">5#out:1</P><P Name="Dst">6#in:1</P></Line>
  <Line><P Name="Src">6#out:1</P><P Name="Dst">7#in:1</P></Line>
  <Line><P Name="Src">7#out:1</P><P Name="Dst">8#in:1</P></Line>
</System>
"""

SYS_7 = """<?xml version="1.0" encoding="utf-8"?>
<System>
  <Block BlockType="SubSystem" Name="Ramp5" SID="9"><System Ref="system_9"/></Block>
</System>
"""

SYS_9 = """<?xml version="1.0" encoding="utf-8"?>
<System>
  <Block BlockType="Switch" Name="SWITCH_2F" SID="10"/>
  <Block BlockType="Gain" Name="Gain&#xA;Two" SID="11"/>
</System>
"""

SYS_3 = """<?xml version="1.0" encoding="utf-8"?>
<System>
  <Block BlockType="Gain" Name="ss2ls_rx_siob" SID="12"/>
</System>
"""


def opc_package(parts):
    out = ["# MathWorks OPC Text Package", "Model {", "  Version 24.2",
           '  Description "Simulink model saved in R2024b"', "}", "__MWOPC_PACKAGE_BEGIN__ R2024b"]
    for path, content in parts.items():
        out.append("__MWOPC_PART_BEGIN__ " + path)
        out.append(content.rstrip("\n"))
    out.append("__MWOPC_PACKAGE_END__")
    return "\n".join(out) + "\n"


CLASSIC_MDL = '''Model {
  Name "RQxSV"
  System {
    Name "RQxSV"
    Block {
      BlockType SubSystem
      Name "ActSys"
      System {
        Name "ActSys"
        Block {
          BlockType Gain
          Name "Obj_000"
          Gain "2"
        }
        Block {
          BlockType Inport
          Name "In1"
        }
        Line {
          SrcBlock "In1"
          SrcPort 1
          DstBlock "Obj_000"
          DstPort 1
        }
      }
    }
    Block {
      BlockType Terminator
      Name "Term1"
    }
    Line {
      SrcBlock "ActSys"
      SrcPort 1
      DstBlock "Term1"
      DstPort 1
    }
  }
}
'''


class TestCrosscheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        parts = [("/[Content_Types].xml", "<Types/>"), ("/_rels/.rels", "<Relationships/>"),
                 ("/simulink/systems/system_root.xml", SYS_ROOT),
                 ("/simulink/systems/system_2.xml", SYS_2),
                 ("/simulink/systems/system_3.xml", SYS_3),
                 ("/simulink/systems/system_7.xml", SYS_7),
                 ("/simulink/systems/system_9.xml", SYS_9),
                 ("/simulink/bdmxdata/LibrarySourceProduct_1.mxarray", "AAAAQUJD\nZGVm")]
        from collections import OrderedDict
        self.parts = OrderedDict(parts)
        self.mdl = os.path.join(self.d, "RQxSV.mdl")
        mm.write_text(self.mdl, opc_package(self.parts))
        self.maps_ok = os.path.join(self.d, "ok.MAPS")
        rows = [gen_maps.HEADER,
                mm.make_line(("base_network_connection", "ActSys.Obj_000", "out", "ActSys.PFC_Ramp", "in")),
                mm.make_line(("base_network_connection", "ActSys.Obj_000", "out", "MeasSys.ss2ls_rx_siob", "in")),
                mm.make_line(("base_network_object", "ActSys.Obj_000")),
                mm.make_line(("base_network_object", "ActSys.PFC_Ramp.Ramp5.SWITCH_2F")),
                mm.make_line(("base_network_object", "MeasSys.ss2ls_rx_siob"))]
        mm.write_text(self.maps_ok, "\n".join(rows) + "\n")
        self.maps_bad = os.path.join(self.d, "bad.MAPS")
        rows.append(mm.make_line(("base_network_object", "ActSys.DOES_NOT_EXIST")))
        rows.append(mm.make_line(("base_network_connection", "ActSys.Obj_000", "out", "MeasSys.GHOST", "in")))
        mm.write_text(self.maps_bad, "\n".join(rows) + "\n")

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_cli(self, *args):
        proc = subprocess.Popen([PY, SCRIPT] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
        return proc.returncode, out.decode("latin-1"), err.decode("latin-1")

    def test_parse_text_package_structure(self):
        struct, kind = mm.load_model_structure(self.mdl)
        self.assertEqual(kind, "mdl-text-package")
        self.assertEqual(struct.blocks["ActSys/PFC_Ramp/Ramp5/SWITCH_2F"], "Switch")
        self.assertEqual(struct.blocks["ActSys/PFC_Ramp/Ramp5/Gain Two"], "Gain")
        self.assertEqual(struct.blocks["MeasSys/ss2ls_rx_siob"], "Gain")
        self.assertIn(("ActSys", "MeasSys"), struct.lines)
        self.assertIn(("ActSys", "Term1"), struct.lines)      # branch resolved
        self.assertIn(("ActSys/Obj_000", "ActSys/PFC_Ramp"), struct.lines)
        self.assertEqual(struct.unresolved, 0)
        self.assertEqual(len(struct.blocks), 11)

    def test_parse_classic_mdl(self):
        p = os.path.join(self.d, "classic.mdl")
        mm.write_text(p, CLASSIC_MDL)
        struct, kind = mm.load_model_structure(p)
        self.assertEqual(kind, "mdl-classic")
        self.assertEqual(struct.blocks["ActSys/Obj_000"], "Gain")
        self.assertEqual(struct.blocks["Term1"], "Terminator")
        self.assertIn(("ActSys/In1", "ActSys/Obj_000"), struct.lines)
        self.assertIn(("ActSys", "Term1"), struct.lines)

    def test_parse_slx_zip(self):
        p = os.path.join(self.d, "m.slx")
        with zipfile.ZipFile(p, "w") as zf:
            for path, content in self.parts.items():
                zf.writestr(path.lstrip("/"), content)
        struct, kind = mm.load_model_structure(p)
        self.assertEqual(kind, "slx")
        self.assertIn("ActSys/PFC_Ramp/Ramp5/SWITCH_2F", struct.blocks)

    def test_crosscheck_ok(self):
        code, out, err = self.run_cli("crosscheck", self.mdl, self.maps_ok, "--strict")
        self.assertEqual(code, 0, out + err)
        self.assertIn("ok: every MAPS object", out)
        self.assertIn("MAPS objects not present in the model: 0", out)

    def test_crosscheck_finds_orphans(self):
        code, out, err = self.run_cli("crosscheck", self.mdl, self.maps_bad, "--strict")
        self.assertEqual(code, 1, out + err)
        self.assertIn("MAPS objects not present in the model: 1", out)
        self.assertIn("ActSys.DOES_NOT_EXIST", out)
        self.assertIn("endpoints not present in the model: 1", out)
        self.assertIn("MeasSys.GHOST", out)
        code, out, err = self.run_cli("crosscheck", self.mdl, self.maps_bad)
        self.assertEqual(code, 0)   # not strict: report only

    def test_naming_rules(self):
        self.assertEqual(mm.map_block_name("A/B/C"), "A.B.C")
        self.assertEqual(mm.map_block_name("A/B/C", "leaf"), "C")
        self.assertEqual(mm.map_block_name("A/B/C", "slash"), "A/B/C")
        self.assertEqual(mm.map_block_name("A/B/C", "dots", strip=1), "B.C")
        self.assertEqual(mm.map_block_name("A/B/C", "dots", prefix="X."), "X.A.B.C")
        # strip=1 makes every object unresolvable -> reported, hint printed
        code, out, err = self.run_cli("crosscheck", self.mdl, self.maps_ok, "--strip", "1")
        self.assertIn("naming rule is probably wrong", out)

    def test_bad_model_file(self):
        p = os.path.join(self.d, "junk.mdl")
        mm.write_text(p, "this is not a model\n")
        code, out, err = self.run_cli("crosscheck", p, self.maps_ok)
        self.assertEqual(code, 2)
        self.assertIn("cannot", err)



# ---------------------------------------------------------------------------
# MATLAB wiring: Kerberos preload and the merge window (stand-in MATLAB tools)
# ---------------------------------------------------------------------------

FAKE_AUTO = """#!/bin/sh
echo "auto LD_PRELOAD=$LD_PRELOAD argc=$#" >> "%(log)s"
exit ${FAKE_AUTOMERGE_EXIT:-0}
"""

FAKE_GUI = """#!/bin/sh
echo "gui LD_PRELOAD=$LD_PRELOAD argc=$# merged=$4" >> "%(log)s"
sleep 1
echo "resolved by fake mlMerge" >> "$4"
exit 0
"""


def make_fake_matlab(root, arch="glnxa64", lib="libkrb5support.so.0", libdir=("bin",), tools=True):
    """A folder that looks like a MATLAB install to maps_merge.py. The tools log
    how they were started to calls.log."""
    bindir = os.path.join(root, "bin", arch)
    os.makedirs(bindir)
    if lib:
        d = os.path.join(root, *(libdir + (arch,)))
        if not os.path.isdir(d):
            os.makedirs(d)
        open(os.path.join(d, lib), "w").close()
    log = os.path.join(root, "calls.log")
    if tools:
        for name, body in (("mlAutoMerge", FAKE_AUTO), ("mlMerge", FAKE_GUI)):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write(body % {"log": log})
            os.chmod(path, 0o755)
    return log


class TestMatlabWiring(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.ml = os.path.join(self.d, "MATLAB", "R2024b")

    def tearDown(self):
        shutil.rmtree(self.d)

    def cli(self, args, cwd=None, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        proc = subprocess.Popen([PY, SCRIPT] + args, cwd=cwd, env=full_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.communicate()[0].decode("latin-1")
        return proc.returncode, out

    # -- finding MATLAB's libkrb5support ----------------------------------------
    def test_preload_found_in_bin(self):
        make_fake_matlab(self.ml)
        lib = os.path.join(self.ml, "bin", "glnxa64", "libkrb5support.so.0")
        self.assertEqual(mm.find_krb5_preload(self.ml, "glnxa64"), lib)

    def test_preload_found_in_sys_os(self):
        make_fake_matlab(self.ml, lib="libkrb5support.so.0.1", libdir=("sys", "os"))
        self.assertEqual(mm.find_krb5_preload(self.ml, "glnxa64"),
                         os.path.join(self.ml, "sys", "os", "glnxa64", "libkrb5support.so.0.1"))

    def test_no_preload_off_linux_or_when_missing(self):
        make_fake_matlab(self.ml)
        self.assertIsNone(mm.find_krb5_preload(self.ml, "win64"))
        self.assertIsNone(mm.find_krb5_preload(self.ml, "maca64"))
        other = os.path.join(self.d, "other")
        make_fake_matlab(other, lib=None)
        self.assertIsNone(mm.find_krb5_preload(other, "glnxa64"))

    # -- the git config lines ---------------------------------------------------
    def test_config_lines_linux_with_preload(self):
        lines = dict(mm.git_config_lines("/s/maps_merge.py", "/usr/bin/python", "/ml", "glnxa64", "/ml/k.so"))
        self.assertEqual(lines["merge.mlAutoMerge.driver"],
                         'env LD_PRELOAD="/ml/k.so" "/ml/bin/glnxa64/mlAutoMerge" %O %A %B %A')
        self.assertEqual(lines["mergetool.mlMerge.cmd"],
                         'env LD_PRELOAD="/ml/k.so" "/ml/bin/glnxa64/mlMerge" "$BASE" "$LOCAL" "$REMOTE" "$MERGED"')
        self.assertNotIn("LD_PRELOAD", lines["merge.maps.driver"])       # MAPS never needs it
        self.assertNotIn("LD_PRELOAD", lines["diff.maps.textconv"])

    def test_config_lines_without_preload_and_on_windows(self):
        lines = dict(mm.git_config_lines("/s/m.py", "py", "/ml", "glnxa64", None))
        self.assertEqual(lines["merge.mlAutoMerge.driver"], '"/ml/bin/glnxa64/mlAutoMerge" %O %A %B %A')
        w = dict(mm.git_config_lines("/s/m.py", "py", "/ml", "win64", None))
        self.assertIn("mlAutoMerge.bat", w["merge.mlAutoMerge.driver"])
        self.assertIn("mlMerge.exe", w["mergetool.mlMerge.cmd"])
        none = dict(mm.git_config_lines("/s/m.py", "py", None))
        self.assertNotIn("merge.mlAutoMerge.driver", none)
        self.assertNotIn("mergetool.mlMerge.cmd", none)

    # -- the setup command --------------------------------------------------------
    def test_setup_reports_preload(self):
        make_fake_matlab(self.ml)
        lib = os.path.join(self.ml, "bin", "glnxa64", "libkrb5support.so.0")
        code, out = self.cli(["setup", "--matlabroot", self.ml, "--matlab-arch", "glnxa64"])
        self.assertEqual(code, 0, out)
        self.assertIn("will start with LD_PRELOAD=%s" % lib, out)
        self.assertIn("git config mergetool.mlMerge.cmd", out)
        self.assertIn("git mergetool --tool=mlMerge", out)
        self.assertNotIn("WARNING", out)
        code, out = self.cli(["setup", "--matlabroot", self.ml, "--matlab-arch", "glnxa64", "--no-preload"])
        self.assertNotIn("LD_PRELOAD", out)
        code, out = self.cli(["setup", "--matlabroot", self.ml, "--matlab-arch", "glnxa64",
                              "--preload", "/does/not/exist.so"])
        self.assertIn("WARNING: preload library not found", out)

    def test_setup_warns_about_missing_tools(self):
        make_fake_matlab(self.ml, tools=False)
        code, out = self.cli(["setup", "--matlabroot", self.ml, "--matlab-arch", "glnxa64"])
        self.assertIn("WARNING: mlAutoMerge not found", out)
        self.assertIn("WARNING: mlMerge not found", out)

    # -- selftest starts mlAutoMerge the way git will -------------------------------
    def test_selftest_uses_preload(self):
        log = make_fake_matlab(self.ml)
        lib = os.path.join(self.ml, "bin", "glnxa64", "libkrb5support.so.0")
        maps = os.path.join(self.d, "x.MAPS")
        mm.write_text(maps, gen_maps.gen())
        mdl = os.path.join(self.d, "m.mdl")
        mm.write_text(mdl, "Model {\n}\n")
        code, out = self.cli(["selftest", maps, "--matlabroot", self.ml, "--matlab-arch", "glnxa64", "--mdl", mdl])
        self.assertEqual(code, 0, out)
        self.assertIn("auto LD_PRELOAD=%s argc=4" % lib, mm.read_text(log))

    # -- the real thing: git merge and git mergetool through the config -------------
    def test_git_merge_and_mergetool_start_matlab_tools_with_preload(self):
        log = make_fake_matlab(self.ml)
        lib = os.path.join(self.ml, "bin", "glnxa64", "libkrb5support.so.0")
        repo = os.path.join(self.d, "repo")
        os.makedirs(repo)

        def g(*args, **kw):
            env = dict(os.environ)
            env.update(kw.get("env", {}))
            with open(os.devnull) as devnull:
                proc = subprocess.Popen(["git"] + list(args), cwd=repo, env=env, stdin=devnull,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                out = proc.communicate()[0].decode("latin-1")
            return proc.returncode, out

        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "tester")
        g("config", "commit.gpgsign", "false")
        code, out = self.cli(["setup", "--apply", "--local-attributes", "--matlabroot", self.ml,
                              "--matlab-arch", "glnxa64"], cwd=repo)
        self.assertEqual(code, 0, out)
        code, out = g("check-attr", "merge", "--", "RQxSV.mdl")
        self.assertIn("merge: mlAutoMerge", out)

        mdl = os.path.join(repo, "RQxSV.mdl")
        mm.write_text(mdl, "base\n")
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        g("checkout", "-q", "-b", "temp")
        mm.write_text(mdl, "temp change\n")
        g("commit", "-q", "-am", "temp")
        g("checkout", "-q", "-")
        g("checkout", "-q", "-b", "mergetest")
        mm.write_text(mdl, "my change\n")
        g("commit", "-q", "-am", "mine")

        # 1. automatic merge succeeds: git started mlAutoMerge with the preload
        code, out = g("merge", "--no-edit", "temp")
        self.assertEqual(code, 0, out)
        self.assertIn("auto LD_PRELOAD=%s argc=4" % lib, mm.read_text(log))
        g("reset", "-q", "--hard", "ORIG_HEAD")

        # 2. automatic merge gives up: conflict, then the merge window resolves it
        code, out = g("merge", "--no-edit", "temp", env={"FAKE_AUTOMERGE_EXIT": "1"})
        self.assertNotEqual(code, 0, out)
        code, status = g("status", "--porcelain")
        self.assertIn("RQxSV.mdl", status)
        code, out = g("mergetool", "--tool=mlMerge", "-y", "--", "RQxSV.mdl")
        self.assertEqual(code, 0, out)
        gui = [l for l in mm.read_text(log).split("\n") if l.startswith("gui ")]
        self.assertEqual(len(gui), 1, mm.read_text(log))
        self.assertIn("LD_PRELOAD=%s argc=4 merged=RQxSV.mdl" % lib, gui[0])
        code, status = g("status", "--porcelain")
        self.assertIn("M  RQxSV.mdl", status)          # resolved and staged
        self.assertIn("resolved by fake mlMerge", mm.read_text(mdl))


if __name__ == "__main__":
    unittest.main()
