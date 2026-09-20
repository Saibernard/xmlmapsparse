# -*- coding: utf-8 -*-
"""Synthetic MAPS file generator mimicking the observed real format."""
from __future__ import print_function
import random

HEADER = " # MAPS-file version 71219, do not edit manually."
SEPS = ['", "'] * 20 + ['","'] * 2 + ['",  "']


def line(fields, rng=None):
    sep = rng.choice(SEPS) if rng else '", "'
    return '"' + sep.join(fields) + '"'


def gen(n_objects=12, params_per_object=5, seed=1, unsorted_types=(), extra_types=None, history=30):
    """Return the text of a synthetic MAPS file.
    Blocks alphabetical by type, rows sorted by fields, mixed separator spacing."""
    rng = random.Random(seed)
    objects = []
    for i in range(n_objects):
        sysname = "ActSys" if i % 2 == 0 else "MeasSys"
        objects.append("%s.Obj_%03d" % (sysname, i))
    objects.append("ActSys.PFC_Ramp.Ramp5.SWITCH_2F")
    objects.append("MeasSys.ss2ls_rx_siob")
    objects = sorted(objects)
    rows = {}

    def add(t, fields):
        rows.setdefault(t, []).append(tuple([t] + list(fields)))

    for o in objects:
        add("base_network_object", [o])
        add("object_type_swid", [o, "PGWBx%s:param_set_t" % o.split(".")[-1].upper(), ""])
        add("conf_group_object", ["GRP_%d" % (len(o) % 3), o])
        add("object_and_dataset", [o, "Nominal", "Default"])
        for mode in ("SS2BF_BASEFRAME", "SCAN"):
            add("control_mode", [mode, o, "Nominal"])
        for p in range(params_per_object):
            for ds in ("Nominal", "Scan"):
                pname = "gain_%d[%d]" % (p, p % 2)
                add("parameter_value", [o, pname, "", "", ds, "Default", "%g" % (rng.random() * 10)])
                add("parameter_attributes", [o, pname, "", "", ds, "Default"] + ["attr%d" % k for k in range(12)])
        add("open_property", [o, "noquote_parent.time_critical", "", "", "", "Default", "FALSE"])
        add("open_property", [o, "quote_EAP", "", "RSCHUCK_DAMAGE_PROTECTION", "NONE", "Default", "NOT_USED"])
        add("open_property", [o, "quote_EAP", "", "RSCHUCK_DAMAGE_PROTECTION", "VERSION_1", "Default", "EA_RS_SS2LOS_LOG"])
        add("output_port", [o, "out_%d" % (len(o) % 4)])
        add("base_network_property", [o, "priority", "%d" % (len(o) % 7)])
    for i in range(len(objects) - 1):
        add("base_network_connection", [objects[i], "ODT_%d" % i, objects[i + 1], "IDT_%d" % i])
    for i in range(3):
        add("base_network_alias", ["alias_%d" % i, objects[i], "%d" % i])
    for i in range(history):
        stamp = "20%02d-06-%02d  19:%02d:40" % (8 + i // 12, 1 + i % 28, i % 60)
        comment = ["Added SS2LS_INIT control mode and associated pid_lp objects.",
                   "Update all PID+LP defaults, Units, and __DOUBLE_QUOTE__To default__DOUBLE_QUOTE__ parameters",
                   "Import NETW with Var gain Z-axis. Add Nominal and Scan datasets for var gain x, y, and z.",
                   "Changed sample period to 0.0004 (2.5kHz)"][i % 4]
        add("history", [stamp, ["cward", "pstraver", "carterm"][i % 3], comment])
    add("checkers", ["38", "Objects"])
    add("checkers", ["39", "Connections"])
    add("open_input", [objects[0], "in1", "", "", "", "Default", "0"])
    add("conf_group_cm_block_inst", ["GRP_0", "SS2BF_BASEFRAME", objects[0], "1"])
    add("network_properties", ["name", "RQxSV"])
    add("network_properties", ["version_number", "17"])
    add("maps_general", ["a", "b", "c", "d"])
    add("fileSettings", ["Show descriptions", "No"])
    add("extMMDC", ["x", "y"])
    add("data_instance", ["Default"])
    add("patch_parameters", ["p", "q", "r"])
    add("variable_properties", ["v", "w"])
    if extra_types:
        for t, fl in extra_types:
            for f in fl:
                add(t, f)
    out = [HEADER]
    for t in sorted(rows):
        recs = rows[t]
        if t in unsorted_types:
            rng.shuffle(recs)
        else:
            recs = sorted(recs)
        for f in recs:
            out.append(line(f, rng))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.write(gen())
