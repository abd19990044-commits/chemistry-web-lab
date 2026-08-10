#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A scripted stand-in for the ORCA binary.

It exists so `tests/test_end_to_end_chain.py` can drive the REAL in-kernel
runner — the whole `KAGGLE_RUNNER_BODY`, unmodified except for the two absolute
Kaggle paths — through many session windows without needing a licensed ORCA or
a Kaggle account. Everything that decides whether the chain makes progress
(reading the .inp, writing the artefacts, printing the banners) is exercised for
real; only the quantum chemistry is faked.

The behaviour is driven by a JSON scenario at $FAKE_ORCA_SCENARIO:

    {"kind": "opt",            # opt | opt_freq | optts | numfreq | scan |
                               # neb | neb_ts | md | sp | tddft_opt | irc
     "converge_after": 12,     # total optimisation steps needed to converge
     "steps_per_window": 5,    # how many it manages before its budget runs out
     "scan_points": 20,        # total points a scan needs
     "points_per_window": 6,
     "fail": null,             # null | "scf" | "input" | "hang"
     "natoms": 3}

State that must persist across windows (how many steps this molecule has taken
in total) lives in $FAKE_ORCA_STATE, outside the working directory — exactly
like a real optimisation's progress lives in the molecule rather than in the
files, so the harness can tell a chain that is advancing from one that is
silently redoing the same window.
"""
import json
import math
import os
import re
import sys

SCEN = json.load(open(os.environ["FAKE_ORCA_SCENARIO"]))
STATE_PATH = os.environ["FAKE_ORCA_STATE"]


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"steps": 0, "scan_points": 0, "windows": 0, "md_steps": 0}


def save_state(st):
    with open(STATE_PATH, "w") as fh:
        json.dump(st, fh)


def geometry(natoms, step):
    """A molecule creeping toward a minimum, so successive windows are
    distinguishable and a restart that loses progress is detectable."""
    d = 1.6 - 0.5 * (1.0 - math.exp(-0.25 * step))
    rows = ["O   0.000000   0.000000   0.000000"]
    for i in range(1, natoms):
        rows.append("H   %.6f   %.6f   0.000000" % (d * i, 0.35 * i))
    return rows[:natoms]


def xyz_frame(natoms, step, comment):
    return "%d\n%s\n%s\n" % (natoms, comment, "\n".join(geometry(natoms, step)))


def main():
    inp_path = sys.argv[1]
    work = os.path.dirname(os.path.abspath(inp_path))
    base = os.path.splitext(os.path.basename(inp_path))[0]
    text = open(inp_path, errors="replace").read()
    clean = re.sub(r"(?m)#.*$", "", text)
    kw = " ".join(l for l in clean.splitlines() if l.lstrip().startswith("!")).lower()

    st = load_state()
    st["windows"] += 1
    kind = SCEN.get("kind", "opt")
    natoms = int(SCEN.get("natoms", 3))
    out = []

    def w(line=""):
        out.append(line)

    w("     " + "*" * 60)
    w("     * O   R   C   A *")
    w("     " + "*" * 60)
    w("")
    w("INPUT FILE")
    for ln in text.splitlines():
        w("| " + ln)
    w("")

    # --- honour the budgets the runner injected --------------------------
    def block_value(block, key):
        m = re.search(r"(?is)%\s*" + block + r"\b(.*?)\bend\b", clean)
        if not m:
            return None
        km = re.search(r"(?i)\b" + key + r"\s+(\d+)", m.group(1))
        return int(km.group(1)) if km else None

    geom_maxiter = block_value("geom", "maxiter") or 50
    neb_maxiter = block_value("neb", "maxiter") or 500
    # MaxIter is the binding constraint, exactly as in ORCA: the optimiser stops
    # either because it converged or because it ran out of cycles. A restart
    # that does not raise the budget therefore cannot make more progress than
    # the window it replaces -- which is what makes the escalation testable.
    budget = min(int(SCEN.get("steps_per_window", 10 ** 6)), geom_maxiter)

    if SCEN.get("sleep_seconds"):
        # Long enough for the runner's session-time watchdog to kill it, so the
        # time-limit restart path is exercised for real, signals and all.
        import time as _t
        _t.sleep(float(SCEN["sleep_seconds"]))

    if SCEN.get("fail") == "input":  # noqa: E501
        w("UNRECOGNIZED OR DUPLICATED KEYWORD IN SIMPLE INPUT LINE")
        w("ORCA finished by error termination in ORCA_MAIN")
        open(os.path.join(work, base + ".out"), "w").write("\n".join(out))
        save_state(st)
        return 1

    if SCEN.get("fail") == "scf":
        w("SCF NOT CONVERGED AFTER  125 CYCLES")
        if block_value("scf", "maxiter") and block_value("scf", "maxiter") >= 500:
            w("FINAL SINGLE POINT ENERGY     -76.412345678")
            w("                             ****ORCA TERMINATED NORMALLY****")
            open(os.path.join(work, base + ".out"), "w").write("\n".join(out))
            save_state(st)
            return 0
        w("ORCA finished by error termination in SCF")
        open(os.path.join(work, base + ".out"), "w").write("\n".join(out))
        save_state(st)
        return 1

    # --- the coordinate block the runner installed -----------------------
    m = re.search(r"\*\s*xyzfile\s+-?\d+\s+-?\d+\s+(\S+)", clean, re.IGNORECASE)
    if m:
        path = os.path.join(work, m.group(1))
        if not os.path.exists(path):
            w("ORCA finished by error termination: cannot open %s" % m.group(1))
            open(os.path.join(work, base + ".out"), "w").write("\n".join(out))
            save_state(st)
            return 1
    charge_mult = re.search(r"\*\s*xyz(?:file)?\s+(-?\d+)\s+(-?\d+)", clean, re.IGNORECASE)
    w("Total Charge           Charge          ....    %s"
      % (charge_mult.group(1) if charge_mult else "0"))
    w(" Multiplicity           Mult            ....    %s"
      % (charge_mult.group(2) if charge_mult else "1"))
    w("")

    # ---------------------------------------------------------------- scan
    if kind == "scan":
        total = int(SCEN.get("scan_points", 10))
        per = int(SCEN.get("points_per_window", 4))
        sm = re.search(r"(?i)Scan\s+[BADC][\d\s]*?=\s*(-?[\d.eE+-]+)\s*,\s*"
                       r"(-?[\d.eE+-]+)\s*,\s*(\d+)", clean)
        want_here = int(sm.group(3)) if sm else total
        did = min(per, want_here)
        for i in range(did):
            idx = st["scan_points"] + i + 1
            w("        *************************************************************")
            w("        *               RELAXED SURFACE SCAN STEP %3d               *" % idx)
            w("        *************************************************************")
            w("")
            w("                    ***********************HURRAY********************")
            w("                    ***        THE OPTIMIZATION HAS CONVERGED     ***")
            w("                    ************************************************")
            w("")
            w("                             *** OPTIMIZATION RUN DONE ***")
            w("FINAL SINGLE POINT ENERGY     -76.%09d" % (400000 + idx))
            with open(os.path.join(work, "%s.%03d.xyz" % (base, i + 1)), "w") as fh:
                fh.write(xyz_frame(natoms, idx, "scan point %d" % idx))
        st["scan_points"] += did
        with open(os.path.join(work, base + "_trj.xyz"), "a") as fh:
            for i in range(did):
                fh.write(xyz_frame(natoms, st["scan_points"] - did + i + 1, "scan"))
        if st["scan_points"] >= total:
            w("        *************************************************************")
            w("        *              RELAXED SURFACE SCAN RESULTS                 *")
            w("        *************************************************************")
        else:
            w("The optimization did not converge but reached the maximum number of")
            w("optimization cycles.")
        w("                             ****ORCA TERMINATED NORMALLY****")

    # ----------------------------------------------------------------- md
    elif kind == "md":
        restart_used = bool(re.search(r"(?i)\brestart\b", clean))
        keeps_velocities = bool(re.search(r"(?i)No_Overwrite", clean))
        if restart_used and os.path.exists(os.path.join(work, base + ".mdrestart")):
            st["md_steps"] += int(SCEN.get("steps_per_window", 500))
            w("MD: restarting from %s.mdrestart" % base)
            if not keeps_velocities:
                w("MD: WARNING velocities reinitialised (Initvel overwrote the restart)")
                st["velocity_resets"] = st.get("velocity_resets", 0) + 1
        else:
            st["md_steps"] = int(SCEN.get("steps_per_window", 500))
        with open(os.path.join(work, base + ".mdrestart"), "w") as fh:
            fh.write("step %d\n" % st["md_steps"])
        with open(os.path.join(work, base + "_trj.xyz"), "a") as fh:
            fh.write(xyz_frame(natoms, st["md_steps"] // 100, "md"))
        w("FINAL SINGLE POINT ENERGY     -76.400000000")
        w("                             ****ORCA TERMINATED NORMALLY****")

    # ---------------------------------------------------------------- neb
    elif kind in ("neb", "neb_ts"):
        st["steps"] += budget
        with open(os.path.join(work, base + "_MEP.allxyz"), "w") as fh:
            fh.write((">\n".join(xyz_frame(natoms, st["steps"], "image %d" % i)
                                 for i in range(4))))
        converged = st["steps"] >= int(SCEN.get("converge_after", 12)) or \
            neb_maxiter >= 1000
        if converged:
            w("                  *** THE NEB OPTIMIZATION HAS CONVERGED ***")
            if kind == "neb_ts":
                if SCEN.get("ts_converges", True):
                    w("                    ***********************HURRAY********************")
                    w("                    ***        THE OPTIMIZATION HAS CONVERGED     ***")
                else:
                    w("The optimization did not converge but reached the maximum number of")
                    w("optimization cycles.")
                if "freq" in kw and SCEN.get("ts_converges", True):
                    w("-----------------------")
                    w("VIBRATIONAL FREQUENCIES")
                    w("-----------------------")
        else:
            w("The NEB optimization has not converged")
        w("FINAL SINGLE POINT ENERGY     -76.400000000")
        w("                             ****ORCA TERMINATED NORMALLY****")

    # ---------------------------------------------------------------- irc
    elif kind == "irc":
        st["steps"] += budget
        with open(os.path.join(work, base + "_IRC_Full_trj.xyz"), "a") as fh:
            for i in range(budget):
                fh.write(xyz_frame(natoms, st["steps"] - budget + i, "irc"))
        if st["steps"] >= int(SCEN.get("converge_after", 12)):
            w("                        *** THE IRC HAS CONVERGED ***")
        else:
            w("The IRC did not converge but reached the maximum number of")
            w("optimization cycles.")
        w("FINAL SINGLE POINT ENERGY     -76.400000000")
        w("                             ****ORCA TERMINATED NORMALLY****")

    # ------------------------------------------------------------- sp/tddft
    elif kind in ("sp", "tddft_sp"):
        w("FINAL SINGLE POINT ENERGY     -76.412345678")
        w("                             ****ORCA TERMINATED NORMALLY****")

    # ------------------------------------ opt / opt_freq / optts / numfreq
    else:
        need = int(SCEN.get("converge_after", 12))
        wants_opt = bool(re.search(r"(?i)\bopt(ts)?\b", kw))
        did = min(budget, max(0, need - st["steps"])) if wants_opt else 0
        with open(os.path.join(work, base + "_trj.xyz"), "a") as fh:
            for i in range(did):
                fh.write(xyz_frame(natoms, st["steps"] + i + 1,
                                   "Coordinates from step %d" % (st["steps"] + i + 1)))
        st["steps"] += did
        converged = (not wants_opt) or st["steps"] >= need
        for i in range(did):
            w("                       *** OPTIMIZATION CYCLE %3d ***" % (st["steps"] - did + i + 1))
            w("FINAL SINGLE POINT ENERGY     -76.%09d" % (400000 + st["steps"] - did + i))
        if wants_opt and converged:
            w("                    ***********************HURRAY********************")
            w("                    ***        THE OPTIMIZATION HAS CONVERGED     ***")
            w("                    ************************************************")
            w("                             *** OPTIMIZATION RUN DONE ***")
        elif wants_opt:
            # The exact pair of lines the real bug hinged on: ORCA gives up on
            # its own cycle budget, says so, prints RUN DONE, and terminates
            # NORMALLY.
            w("The optimization did not converge but reached the maximum number of")
            w("optimization cycles.")
            w("                             *** OPTIMIZATION RUN DONE ***")
        if converged:
            with open(os.path.join(work, base + ".xyz"), "w") as fh:
                fh.write(xyz_frame(natoms, st["steps"], "converged geometry"))
        if "freq" in kw and converged:
            n = 3 * natoms
            with open(os.path.join(work, base + ".hess"), "w") as fh:
                fh.write("$orca_hessian_file\n\n$hessian\n%d\n" % n)
                for blk in range(0, n, 5):
                    cols = list(range(blk, min(blk + 5, n)))
                    fh.write("  " + "  ".join(str(c) for c in cols) + "\n")
                    for r in range(n):
                        fh.write("  %d " % r + " ".join("0.010000" for _ in cols) + "\n")
                fh.write("\n$end\n")
            w("-----------------------")
            w("VIBRATIONAL FREQUENCIES")
            w("-----------------------")
            w("   0:         0.00 cm**-1")
        w("FINAL SINGLE POINT ENERGY     -76.%09d" % (400000 + st["steps"]))
        w("                             ****ORCA TERMINATED NORMALLY****")

    open(os.path.join(work, base + ".out"), "w").write("\n".join(out) + "\n")
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
