"""Publication-quality vector export layer.

Loaded by the Docker entrypoint instead of changing the established application
architecture. The existing Flask application remains the source of truth for
all chemistry logic and UI; this module only adds SVG export endpoints and a
small client-side enhancement that exposes vector downloads.

SVG exports are genuine vector drawings from RDKit's MolDraw2DSVG renderer;
they do not depend on the 600-DPI PNG preview.
"""
from __future__ import annotations

from flask import Response, jsonify, request

import chem_core as core
import app as application

app = application.app


def _svg_molecule(smiles: str) -> bytes | None:
    mol = core.Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    try:
        core.AllChem.Compute2DCoords(mol)
        core.Chem.rdDepictor.StraightenDepiction(mol)
    except Exception:
        pass
    drawer = core.rdMolDraw2D.MolDraw2DSVG(core.MOL_IMAGE_SIZE[0], core.MOL_IMAGE_SIZE[1])
    core._apply_common_draw_options(drawer)
    opts = drawer.drawOptions()
    try:
        opts.fixedBondLength = 90.0
        opts.fixedFontSize = 60.0
        opts.minFontSize = 48.0
        opts.maxFontSize = 66.0
        opts.padding = 0.05
    except Exception:
        pass
    try:
        drawer.SetFontSize(60)
    except Exception:
        pass
    core.rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText().encode("utf-8")


def _svg_reaction(reaction_smiles: str) -> bytes | None:
    if not reaction_smiles or ">>" not in reaction_smiles:
        return None
    left, right = reaction_smiles.split(">>", 1)
    if not left or not right:
        return None
    try:
        rxn = core.rdChemReactions.ReactionFromSmarts(f"{left}>>{right}", useSmiles=True)
        if rxn is None:
            return None
        core.rdChemReactions.Compute2DCoordsForReaction(rxn)
        drawer = core.rdMolDraw2D.MolDraw2DSVG(3600, 1800)
        core._apply_common_draw_options(drawer)
        try:
            drawer.drawOptions().bondLineWidth = core.DRAWING_BOND_LINE_WIDTH
            drawer.drawOptions().padding = 0.08
        except Exception:
            pass
        drawer.DrawReaction(rxn)
        drawer.FinishDrawing()
        return drawer.GetDrawingText().encode("utf-8")
    except Exception:
        return None


@app.get("/api/publication/molecule-svg")
def publication_molecule_svg():
    smiles = (request.args.get("smiles") or "").strip()
    svg = _svg_molecule(smiles)
    if not svg:
        return jsonify({"ok": False, "error": "Invalid SMILES."}), 422
    return Response(svg, mimetype="image/svg+xml", headers={"Content-Disposition": "attachment; filename=molecule.svg"})


@app.post("/api/publication/reaction-svg")
def publication_reaction_svg():
    data = request.get_json(force=True, silent=True) or {}
    svg = _svg_reaction((data.get("reaction_smiles") or "").strip())
    if not svg:
        return jsonify({"ok": False, "error": "Invalid reaction SMILES."}), 422
    return Response(svg, mimetype="image/svg+xml", headers={"Content-Disposition": "attachment; filename=reaction.svg"})


_PUBLICATION_JS = r"""
<script>
(() => {
  if (window.__publicationVectorExportsInstalled) return;
  window.__publicationVectorExportsInstalled = true;

  const originalFetch = window.fetch.bind(window);

  function addButton(parent, id, text, href, filename) {
    if (!parent || document.getElementById(id)) return;
    const a = document.createElement("a");
    a.id = id;
    a.className = "btn btn-ghost btn-small";
    a.textContent = text;
    a.href = href;
    a.download = filename;
    parent.appendChild(a);
  }

  async function addMoleculeVector(data) {
    if (!data || !data.ok || !data.smiles) return;
    const url = "/api/publication/molecule-svg?smiles=" + encodeURIComponent(data.smiles);
    const parent = document.querySelector("#explorer-result .result-visual");
    if (!parent) return;
    addButton(parent, "explorer-download-svg", "⬇ Download SVG (vector)", url, (data.filename || "molecule") + ".svg");
  }

  async function addReactionVector(data) {
    if (!data || !data.ok || !data.reaction_smiles) return;
    const response = await originalFetch("/api/publication/reaction-svg", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({reaction_smiles: data.reaction_smiles})
    });
    if (!response.ok) return;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const parent = document.querySelector("#reaction-result .reaction-actions");
    addButton(parent, "reaction-download-svg", "⬇ Download SVG (vector)", url, "reaction.svg");
  }

  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    try {
      const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
      if (url.endsWith("/api/compound")) {
        response.clone().json().then(addMoleculeVector).catch(() => {});
      } else if (url.endsWith("/api/reaction")) {
        response.clone().json().then(addReactionVector).catch(() => {});
      }
    } catch (_) {}
    return response;
  };
})();
</script>
"""


@app.after_request
def _inject_publication_controls(response):
    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type:
        return response
    try:
        body = response.get_data(as_text=True)
        if "__publicationVectorExportsInstalled" not in body and "</body>" in body:
            body = body.replace("</body>", _PUBLICATION_JS + "\n</body>", 1)
            response.set_data(body)
    except Exception:
        pass
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860)
