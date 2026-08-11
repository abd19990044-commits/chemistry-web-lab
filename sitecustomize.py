"""Production startup patches for the Chemistry Lab Space.

Python loads sitecustomize automatically at interpreter startup. The patches are
kept isolated from the main application so deployment fixes can be reviewed
and removed independently.
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────
# Kaggle production status patch
# ─────────────────────────────────────────────────────────────
try:
    import kaggle_runner as _kr

    def _production_check_job_status(kaggle_username: str, kaggle_key: str, job_id: str) -> dict:
        if not _kr.is_valid_job_id(job_id):
            raise RuntimeError(f"'{job_id}' is not a valid job id.")

        auth = _kr.resolve_kaggle_auth(kaggle_username, kaggle_key)
        with _kr._temp_kaggle_env(kaggle_username, kaggle_key) as env:
            proc = _kr._run_kaggle_cli(
                ["kaggle", "kernels", "status", f"{auth['username']}/{job_id}"],
                env=env, timeout=60,
            )
            text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
            status = _kr._classify_kernel_status(text)

            if proc.returncode != 0:
                _kr._raise_if_cli_broken(text)
                _kr._raise_if_unreachable(text)
                note = text or "The Kaggle CLI returned no output."
                low = note.lower()
                if "401" in low or "unauthorized" in low or "authentication" in low:
                    note += "\n\nCheck your Kaggle username and API key/token."
                elif "404" in low or "not found" in low or "denied" in low:
                    note += "\n\nKaggle has no notebook at this address for this account."
                return {"status": "error", "next_job_id": None,
                        "next_kaggle_url": None, "note": note}

            if status in ("complete", "error", "cancelled"):
                next_id, next_url = _kr._probe_successor(env, auth["username"], job_id)
                if next_id:
                    return {
                        "status": "restarting",
                        "next_job_id": next_id,
                        "next_kaggle_url": next_url,
                        "note": "A continuation notebook exists on Kaggle and is being followed.",
                    }

            if status in ("complete", "running", "queued", "cancelled", "error"):
                return {"status": status, "next_job_id": None,
                        "next_kaggle_url": None, "note": text}

            return _kr._ORIGINAL_CHECK_JOB_STATUS(kaggle_username, kaggle_key, job_id)

    if not hasattr(_kr, "_ORIGINAL_CHECK_JOB_STATUS"):
        _kr._ORIGINAL_CHECK_JOB_STATUS = _kr.check_job_status
        _kr.check_job_status = _production_check_job_status

    try:
        import flask as _flask
        _original_flask_init = _flask.Flask.__init__

        def _patched_flask_init(self, *args, **kwargs):
            _original_flask_init(self, *args, **kwargs)

            @self.after_request
            def _inject_runtime_fix(response):
                try:
                    if (response.status_code == 200
                            and response.content_type
                            and response.content_type.startswith("text/html")
                            and not response.direct_passthrough):
                        body = response.get_data(as_text=True)
                        marker = '/static/job_runtime_fix.js?v=20260811'
                        if marker not in body and "</body>" in body:
                            tag = f'<script src="{marker}" defer></script>'
                            response.set_data(body.replace("</body>", tag + "</body>"))
                            response.headers.pop("Content-Length", None)
                except Exception:
                    pass
                return response

        _flask.Flask.__init__ = _patched_flask_init
    except Exception:
        pass

except Exception:
    pass

# ─────────────────────────────────────────────────────────────
# RDKit publication-quality drawing
# ─────────────────────────────────────────────────────────────
try:
    import io as _io
    import logging as _logging
    from PIL import Image as _Image
    import chem_core as _chem_core

    def _balanced_draw_options(drawer):
        opts = drawer.drawOptions()
        opts.addStereoAnnotation = True
        opts.bondLineWidth = 1.7
        opts.baseFontSize = 0.70
        opts.minFontSize = 8
        opts.maxFontSize = 18
        opts.annotationFontScale = 0.60
        opts.additionalAtomLabelPadding = 0.0
        opts.padding = 0.10
        opts.legendFontSize = 16

    _chem_core._apply_common_draw_options = _balanced_draw_options

    _PUBLICATION_SIZE = (2400, 1800)
    _REFERENCE_SIZE = (560, 420)
    _PUBLICATION_DPI = 600

    def _production_render_molecule_png(smiles: str, legend: str = "", size=None) -> bytes | None:
        """Render the corrected drawing at 2400x1800 px with 600 dpi metadata.

        The frontend may display the PNG at a smaller CSS width, but the actual
        downloaded image retains publication-grade pixels. Geometry and label
        proportions are scaled with the canvas so the earlier size correction is
        preserved instead of producing a tiny molecule on the larger canvas.
        """
        if size is None or size == _chem_core.MOL_IMAGE_SIZE:
            size = _PUBLICATION_SIZE

        mol = _chem_core.Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        try:
            _chem_core.AllChem.Compute2DCoords(mol)
            _chem_core.Chem.rdDepictor.StraightenDepiction(mol)
        except Exception:  # noqa: BLE001
            pass

        drawer = _chem_core.rdMolDraw2D.MolDraw2DCairo(size[0], size[1])
        _balanced_draw_options(drawer)
        opts = drawer.drawOptions()
        scale = min(size[0] / _REFERENCE_SIZE[0], size[1] / _REFERENCE_SIZE[1])
        try:
            # 30 px / 16 pt were the corrected values at 560x420. Scale them
            # linearly so the high-resolution output has identical proportions.
            opts.fixedBondLength = 30.0 * scale
            opts.fixedFontSize = 16.0 * scale
            opts.minFontSize = 12.0 * scale
            opts.maxFontSize = 18.0 * scale
            opts.bondLineWidth = 1.7 * scale
            opts.padding = 0.05
            opts.centreMoleculesBeforeDrawing = True
            opts.prepareMolsBeforeDrawing = True
        except Exception:  # noqa: BLE001
            pass

        _chem_core.rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
        drawer.FinishDrawing()
        raw = drawer.GetDrawingText()

        try:
            # Cairo creates the full-resolution pixels. Pillow only adds the
            # explicit physical-resolution metadata required by many journals.
            image = _Image.open(_io.BytesIO(raw)).convert("RGB")
            out = _io.BytesIO()
            image.save(
                out,
                format="PNG",
                dpi=(_PUBLICATION_DPI, _PUBLICATION_DPI),
                optimize=True,
            )
            return out.getvalue()
        except Exception as exc:  # noqa: BLE001
            _logging.getLogger("chemistry_tools").warning(
                "Could not attach 600 dpi PNG metadata: %s", exc
            )
            return raw

    _chem_core.render_molecule_png = _production_render_molecule_png
except Exception:
    pass
