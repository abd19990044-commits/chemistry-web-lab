# -*- coding: utf-8 -*-
"""Tests for drawing a chemical equation with stoichiometric coefficients.

Run: `python tests/test_reaction.py`   (no network — PubChem is stubbed)

Three things have to hold, and they pull in different directions:

  * The PICTURE must read like a chemist's equation: `2 H2 + O2 -> 2 H2O`, with
    the coefficient as a numeral.
  * The FILE must be a valid MDL RXN, where the same stoichiometry can only be
    expressed by repeating the structure — the format has no numeral field.
  * Neither may quietly change the chemistry. A coefficient misread out of a
    compound name, a fraction dropped, an unbalanced equation drawn without
    comment, or `CO` taken as methanol when carbon monoxide was meant all
    produce a confident, wrong result.
"""
import base64
import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed, _failed = 0, 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s" % label)
    else:
        _failed += 1
        print("  FAIL  %s%s" % (label, ("\n        " + detail) if detail else ""))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


try:
    import chem_core as core
    import app as webapp
except ImportError as exc:                                      # pragma: no cover
    print("SKIP: %s (install flask and rdkit to run this suite)" % exc)
    sys.exit(0)

client = webapp.app.test_client()

# A tiny offline PubChem, so the suite never touches the network and the
# ambiguous-token path is exercised deterministically.
KNOWN = {
    "water": "O", "carbon monoxide": "[C-]#[O+]", "nitric oxide": "[N]=O",
    "methane": "C", "oxygen": "O=O", "hydrogen": "[H][H]",
    "carbon dioxide": "O=C=O", "ethanol": "CCO", "acetic acid": "CC(=O)O",
    "ammonia": "N", "nitrogen": "N#N", "cyanide": "[C-]#N",
}
core.pubchem_smiles_by_name = lambda name: KNOWN.get((name or "").strip().lower())
core.pubchem_reachable = lambda timeout=4.0: True


# ===========================================================================
section("1. Reading the coefficient without eating the compound name")
# ===========================================================================
CASES = [
    ("2 H2O", 2, "H2O", "the spaced form"),
    ("2H2O", 2, "H2O", "the glued form"),
    ("2Ca(OH)2", 2, "Ca(OH)2", "a glued coefficient before a parenthesised formula"),
    ("1/2 O2", Fraction(1, 2), "O2", "a fraction, as thermochemistry is written"),
    ("0.5 O2", Fraction(1, 2), "O2", "a decimal, converted exactly rather than as a float"),
    ("3 sodium chloride", 3, "sodium chloride", "a coefficient before a multi-word name"),
    ("water", 1, "water", "no coefficient means one"),
    ("2-butanone", 1, "2-butanone", "REGRESSION: a locant is not a coefficient"),
    ("1H-pyrrole", 1, "1H-pyrrole", "REGRESSION: an indicated hydrogen is not a coefficient"),
    ("13C-methanol", 1, "13C-methanol", "REGRESSION: an isotope label is not a coefficient"),
    ("4-nitrophenol", 1, "4-nitrophenol", "REGRESSION: a substituent position is not a coefficient"),
]
for text, want_coefficient, want_name, why in CASES:
    coefficient, name = core.parse_coefficient(text)
    check("%-52s (%s)" % (why, text),
          coefficient == Fraction(want_coefficient) and name == want_name,
          "got coefficient=%s name=%r" % (coefficient, name))

terms = core.split_compound_terms("2 H2 + O2 + 1/2 N2")
check("a '+'-joined list keeps every coefficient",
      [(t.coefficient, t.name) for t in terms]
      == [(Fraction(2), "H2"), (Fraction(1), "O2"), (Fraction(1, 2), "N2")],
      str([(str(t.coefficient), t.name) for t in terms]))
check("the equation is formatted the way it was written",
      core.format_equation(core.split_compound_terms("2 H2 + O2"),
                           core.split_compound_terms("2 H2O")) == "2 H2 + O2 -> 2 H2O")


# ===========================================================================
section("2. Fractions are scaled away for the file, never silently")
# ===========================================================================
factor, (left, right) = core.scale_terms_to_integers(
    core.split_compound_terms("H2 + 1/2 O2"), core.split_compound_terms("H2O"))
check("the smallest whole-number factor is chosen", factor == 2, "factor=%s" % factor)
check("...and applied to both sides",
      [str(t.coefficient) for t in left] == ["2", "1"]
      and [str(t.coefficient) for t in right] == ["2"])
check("an already-integer equation is left alone",
      core.scale_terms_to_integers(core.split_compound_terms("2 H2 + O2"),
                                   core.split_compound_terms("2 H2O"))[0] == 1)
factor, _ = core.scale_terms_to_integers(core.split_compound_terms("1/3 A + 1/2 B"),
                                         core.split_compound_terms("C"))
check("mixed denominators use their least common multiple", factor == 6, "factor=%s" % factor)


# ===========================================================================
section("3. Balance is reported, with the specific surplus")
# ===========================================================================
v = core.check_reaction_balance([(2, "[H][H]"), (1, "O=O")], [(2, "O")])
check("a balanced equation is recognised", v["balanced"] is True, v["message"])
v = core.check_reaction_balance([(1, "[H][H]"), (1, "O=O")], [(1, "O")])
check("REGRESSION: an unbalanced equation is caught", v["balanced"] is False)
check("...and names the element, the amount and the side",
      v["excess_amount"].get("O") == "1" and v["excess_side"].get("O") == "reactant"
      and "reactant side" in v["message"], v["message"])
check("...and the signed difference is reported under a name that says so",
      v["element_difference"].get("O") == "-1", str(v.get("element_difference")))
check("implicit hydrogens are counted",
      core.check_reaction_balance([(1, "C")], [(1, "[C]")])["balanced"] is False,
      "CH4 -> C is not balanced")
v = core.check_reaction_balance([(1, "[Na+]"), (1, "[Cl-]")], [(1, "[Na]"), (1, "[Cl]")])
check("charge is checked as well as mass", v["balanced"] is False and v["charge_difference"] == "0"
      or "charge" in v["message"], v["message"])
v = core.check_reaction_balance([(1, "[Na+]")], [(1, "[Na]")])
check("...and a charge-only imbalance is reported as such",
      v["balanced"] is False and "charge" in v["message"], v["message"])
v = core.check_reaction_balance([(1, "CCO"), (1, "CC(=O)O")], [(1, "CCOC(C)=O"), (1, "O")])
check("a real esterification balances", v["balanced"] is True, v["message"])
v = core.check_reaction_balance([(1, "N#N"), (3, "[H][H]")], [(2, "N")])
check("the Haber process balances", v["balanced"] is True, v["message"])
v = core.check_reaction_balance([(1, "C"), (2, "O=O")], [(1, "O=C=O"), (2, "O")])
check("methane combustion balances", v["balanced"] is True, v["message"])
check("an unreadable structure does not claim a verdict",
      core.check_reaction_balance([(1, "not-a-smiles")], [(1, "O")])["checked"] is False)


# ===========================================================================
section("4. The RXN file: stoichiometry by repetition, and it must be valid")
# ===========================================================================
blob = core.generate_rxn_file_bytes([(2, "[H][H]"), (1, "O=O")], [(2, "O")],
                                    title="2 H2 + O2 -> 2 H2O")
text = blob.decode()
report = core.validate_rxn_block(text)
check("the file passes its own structural validation", report["valid"] is True,
      str(report.get("problems")))
check("it is MDL RXN V2000 — the format ChemDraw imports",
      report["format"] == "MDL RXN V2000")
check("REGRESSION: a coefficient of 2 puts the structure in the file twice",
      report["reactants"] == 3 and report["products"] == 2,
      "R=%s P=%s (2 H2 + O2 = 3 components; 2 H2O = 2)"
      % (report["reactants"], report["products"]))
check("the counts line matches the number of $MOL blocks",
      text.count("$MOL") == report["reactants"] + report["products"])
check("every component terminates with 'M  END'",
      text.count("M  END") == report["reactants"] + report["products"])
check("the equation travels in the file header", text.splitlines()[1] == "2 H2 + O2 -> 2 H2O")

# The strongest evidence available without ChemDraw: a different, independent
# MDL reader parses the file back and finds the same stoichiometry.
from rdkit.Chem import rdChemReactions                          # noqa: E402

reparsed = rdChemReactions.ReactionFromRxnBlock(text)
check("REGRESSION: an independent MDL reader parses the file back",
      reparsed is not None)
check("...and recovers the same component counts",
      reparsed is not None and reparsed.GetNumReactantTemplates() == 3
      and reparsed.GetNumProductTemplates() == 2)

check("a fractional coefficient is refused rather than rounded",
      core.generate_rxn_file_bytes([(Fraction(1, 2), "O=O")], [(1, "O")]) is None,
      "the format cannot express half a structure; the caller must scale first")

for broken, why in (
    ("not a reaction file", "a file that is not RXN at all"),
    ("$RXN\n\n\n\n  9  9\n", "a counts line that promises components the file lacks"),
    ("$RXN\n\n\n\n  1  1\n$MOL\nx\n$MOL\ny\n", "components with no V2000 line or M  END"),
):
    check("the validator rejects %s" % why, core.validate_rxn_block(broken)["valid"] is False)


# ===========================================================================
section("5. Drawing: the numeral appears, and the scale is uniform")
# ===========================================================================
png = core.render_reaction_png([(2, "[H][H]"), (1, "O=O")], [(2, "O")])
check("a scheme is produced", bool(png) and png[:4] == b"\x89PNG")
from PIL import Image                                           # noqa: E402
import io as _io                                                # noqa: E402

img = Image.open(_io.BytesIO(png))
check("...at a sane size", img.width > 300 and 60 < img.height < 800, str(img.size))
plain = core.render_reaction_png([(1, "[H][H]"), (1, "O=O")], [(1, "O")])
check("a coefficient of 2 makes the drawing wider than the same equation without one",
      Image.open(_io.BytesIO(png)).width > Image.open(_io.BytesIO(plain)).width,
      "the numeral is drawn, so it takes space")
check("a species that cannot be parsed yields no drawing rather than a wrong one",
      core.render_reaction_png([(1, "not-a-smiles")], [(1, "O")]) is None)


# ===========================================================================
section("5b. Small molecules are written, not drawn")
# ===========================================================================
WRITTEN = [("O=O", "O2"), ("O", "H2O"), ("O=C=O", "CO2"), ("[H][H]", "H2"),
           ("N#N", "N2"), ("N", "NH3"), ("C", "CH4"), ("[C-]#[O+]", "CO"),
           ("Cl", "HCl"), ("O=S=O", "SO2"), ("OS(=O)(=O)O", "H2SO4"),
           ("[Na+].[Cl-]", "NaCl"), ("ClC(Cl)Cl", "CHCl3"), ("[OH-]", "OH-")]
for smiles, formula in WRITTEN:
    check("%-14s is written as %s" % (smiles, formula),
          core.formula_for_display(smiles) == formula,
          "got %r" % core.formula_for_display(smiles))

DRAWN = [
    ("CCO", "ethanol shares C2H6O with dimethyl ether, so the formula would lose the structure"),
    ("COC", "dimethyl ether, the other half of that ambiguity"),
    ("c1ccccc1", "benzene is a structure, not a formula"),
    ("CO", "methanol is written CH3OH, not the Hill CH4O, so it is drawn"),
    ("CC(=O)O", "acetic acid"),
]
for smiles, why in DRAWN:
    check("%-10s is drawn as a structure (%s)" % (smiles, why),
          core.formula_for_display(smiles) is None,
          "got %r" % core.formula_for_display(smiles))

check("REGRESSION: Hill order is not trusted for carbon-free species",
      core.formula_for_display("N") == "NH3" and core.formula_for_display("OS(=O)(=O)O") == "H2SO4",
      "RDKit's Hill formula gives H3N and H2O4S, which no chemist writes")

tokens = core._formula_tokens("H2SO4")
check("a digit after an element becomes a subscript",
      ("2", "sub") in tokens and ("4", "sub") in tokens, str(tokens))
check("a trailing charge becomes a superscript",
      ("+", "sup") in core._formula_tokens("NH4+"), str(core._formula_tokens("NH4+")))

with_formulas = core.render_reaction_png([(2, "[H][H]"), (1, "O=O")], [(2, "O")])
all_drawn = core.render_reaction_png([(2, "[H][H]"), (1, "O=O")], [(2, "O")],
                                     small_as_formula=False)
check("the two rendering modes produce different pictures", with_formulas != all_drawn)
check("...and both are valid PNGs",
      with_formulas[:4] == b"\x89PNG" and all_drawn[:4] == b"\x89PNG")

r = client.post("/api/reaction", json={"reactants": "2 hydrogen + oxygen",
                                       "products": "2 water"})
formula_png = r.get_json()["image_png_base64"]
r = client.post("/api/reaction", json={"reactants": "2 hydrogen + oxygen",
                                       "products": "2 water", "small_as_formula": False})
check("the route honours small_as_formula=False",
      r.get_json()["image_png_base64"] != formula_png)


# ===========================================================================
section("6. Ambiguous tokens are resolved deliberately, and disclosed")
# ===========================================================================
smiles, note = core.resolve_species("CO")
check("REGRESSION: 'CO' is read as carbon monoxide, not methanol",
      smiles == "[C-]#[O+]", "got %r" % smiles)
check("...and the reading is stated so it can be overridden",
      "carbon monoxide" in note, note)
smiles, note = core.resolve_species("CCO")
check("REGRESSION: a plain SMILES with no digit still resolves", smiles == "CCO")
smiles, note = core.resolve_species("water")
check("a name resolves with no note", smiles == "O" and note == "")

# REGRESSION: the help panel tells people they can type a formula. Before this,
# `O2` was not valid SMILES and had no local resolution at all, so the three
# most common species in any equation failed unless PubChem happened to answer.
_no_network = core.pubchem_smiles_by_name
core.pubchem_smiles_by_name = lambda name: None
try:
    for formula, expected in (("O2", "O=O"), ("H2O", "O"), ("CO2", "O=C=O"),
                              ("NH3", "N"), ("CH4", "C"), ("N2", "N#N"),
                              ("H2", "[H][H]"), ("H2SO4", "OS(=O)(=O)O"),
                              ("SO2", "O=S=O"), ("HCl", "Cl")):
        got, _ = core.resolve_species(formula)
        want = core.Chem.MolToSmiles(core.Chem.MolFromSmiles(expected))
        check("REGRESSION: '%s' resolves with no network at all" % formula, got == want,
              "got %r, wanted %r" % (got, want))
    check("...and the lookup is case-insensitive",
          core.resolve_species("h2o")[0] == core.resolve_species("H2O")[0])
finally:
    core.pubchem_smiles_by_name = _no_network

r = client.post("/api/reaction", json={"reactants": "2 H2 + O2", "products": "2 H2O"})
d = r.get_json()
check("REGRESSION: an equation written entirely in formulas works end to end",
      r.status_code == 200 and d["balance"]["balanced"] is True,
      json.dumps(d.get("balance") or d)[:200])
check("...and the file is valid", d["file_report"]["valid"] is True)


# ===========================================================================
section("7. The route ties it together")
# ===========================================================================
r = client.post("/api/reaction", json={"reactants": "2 hydrogen + oxygen",
                                       "products": "2 water"})
d = r.get_json()
check("the route answers", r.status_code == 200 and d["ok"] is True, json.dumps(d)[:200])
check("the equation is echoed as parsed", d["equation"] == "2 hydrogen + oxygen -> 2 water",
      d.get("equation"))
check("the balance verdict travels with it", d["balance"]["balanced"] is True)
check("so does the file report", d["file_report"]["valid"] is True)
check("the file in the response is the validated one",
      core.validate_rxn_block(base64.b64decode(d["rxn_file_base64"]).decode())["valid"])
check("the repetition convention is explained to the user",
      any("appears that many times" in n for n in d["notes"]), str(d["notes"])[:200])

r = client.post("/api/reaction", json={"reactants": "hydrogen + 1/2 oxygen",
                                       "products": "water"})
d = r.get_json()
check("a fractional coefficient is accepted", r.status_code == 200)
check("...and the scaling applied to the file is disclosed",
      any("multiplied by 2" in n for n in d["notes"]), str(d["notes"])[:220])
check("...while the drawing keeps the equation as written",
      d["equation"] == "hydrogen + 1/2 oxygen -> water", d.get("equation"))

r = client.post("/api/reaction", json={"reactants": "methane + oxygen",
                                       "products": "carbon dioxide + water"})
d = r.get_json()
check("REGRESSION: an unbalanced equation is drawn but flagged",
      r.status_code == 200 and d["balance"]["balanced"] is False, d["balance"]["message"])
check("...and says what is missing", "O" in d["balance"]["excess_amount"],
      json.dumps(d["balance"])[:200])

r = client.post("/api/reaction", json={"reactants": "0 water", "products": "water"})
check("a coefficient of zero is refused", r.status_code == 400 and "greater than zero"
      in r.get_json()["error"], json.dumps(r.get_json())[:150])
r = client.post("/api/reaction", json={"reactants": "9999 water", "products": "water"})
check("an absurd coefficient is refused with the reason",
      r.status_code == 400 and "repeating the structure" in r.get_json()["error"],
      json.dumps(r.get_json())[:200])
r = client.post("/api/reaction", json={"reactants": "", "products": "water"})
check("an empty side is refused", r.status_code == 400)
r = client.post("/api/reaction", json={"reactants": "unobtainium", "products": "water"})
check("an unknown species is named in the error",
      r.status_code == 404 and "unobtainium" in r.get_json()["error"])


print("\n" + "=" * 70)
print("REACTION: %d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
