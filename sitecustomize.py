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

    # Cosmetic production fix: the original UI timer measures browser
    # submission -> now. Once the backend says COMPLETE, the frontend can
    # replace the misleading live timer with a terminal label.
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
# RDKit publication-style drawing proportions
# ─────────────────────────────────────────────────────────────
try:
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

    def _production_render_molecule_png(smiles: str, legend: str = "", size=None) -> bytes | None:
        """Render a molecule with a stable chemical scale.

        The bond length, rather than the canvas dimensions, defines the visual
        scale. This prevents RDKit from enlarging the core skeleton whenever a
        molecule has fewer substituents and prevents OH/NH/O labels from
        becoming disproportionately large. The same principle is used by the
        reaction drawing path in the project.
        """
        if size is None:
            size = _chem_core.MOL_IMAGE_SIZE
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
        # Keep the skeleton stable across molecules. RDKit will only need to
        # compromise when a very large structure physically cannot fit.
        try:
            opts.fixedBondLength = 30.0
            opts.fixedFontSize = 16.0
            opts.minFontSize = 12.0
            opts.maxFontSize = 18.0
        except Exception:  # noqa: BLE001
            pass
        _chem_core.rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
        drawer.FinishDrawing()
        return drawer.GetDrawingText()

    _chem_core.render_molecule_png = _production_render_molecule_png
except Exception:
    pass
