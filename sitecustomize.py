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
        if size is None or size == _chem_core.MOL_IMAGE_SIZE:
            size = _PUBLICATION_SIZE
        mol = _chem_core.Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        try:
            _chem_core.AllChem.Compute2DCoords(mol)
            _chem_core.Chem.rdDepictor.StraightenDepiction(mol)
        except Exception:
            pass
        drawer = _chem_core.rdMolDraw2D.MolDraw2DCairo(size[0], size[1])
        _balanced_draw_options(drawer)
        opts = drawer.drawOptions()
        scale = min(size[0] / _REFERENCE_SIZE[0], size[1] / _REFERENCE_SIZE[1])
        try:
            opts.fixedBondLength = 30.0 * scale
            opts.fixedFontSize = 16.0 * scale
            opts.minFontSize = 12.0 * scale
            opts.maxFontSize = 18.0 * scale
            opts.bondLineWidth = 1.7 * scale
            opts.padding = 0.05
            opts.centreMoleculesBeforeDrawing = True
            opts.prepareMolsBeforeDrawing = True
        except Exception:
            pass
        _chem_core.rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
        drawer.FinishDrawing()
        raw = drawer.GetDrawingText()
        try:
            image = _Image.open(_io.BytesIO(raw)).convert("RGB")
            out = _io.BytesIO()
            image.save(out, format="PNG", dpi=(_PUBLICATION_DPI, _PUBLICATION_DPI), optimize=True)
            return out.getvalue()
        except Exception as exc:
            _logging.getLogger("chemistry_tools").warning("Could not attach 600 dpi PNG metadata: %s", exc)
            return raw

    _chem_core.render_molecule_png = _production_render_molecule_png
except Exception:
    pass

# ─────────────────────────────────────────────────────────────
# Kaggle → Hugging Face keepalive
# ─────────────────────────────────────────────────────────────
# usercustomize installs the existing five-minute ORCA heartbeat after Python
# imports this module. Wait until that patch has finished, then augment only
# the post-heartbeat reschedule point with a best-effort request to this Space.
# A failed ping is logged but can never interrupt or restart ORCA.
try:
    import sys as _sys
    import threading as _threading
    import time as _time

    _HF_KEEPALIVE_URL = "https://mc2hf1999-orcaweb.hf.space/health"

    def _install_hf_keepalive() -> None:
        deadline = _time.monotonic() + 30.0
        while _time.monotonic() < deadline:
            module = _sys.modules.get("kaggle_runner")
            body = getattr(module, "KAGGLE_RUNNER_BODY", None) if module else None
            if isinstance(body, str) and "[heartbeat] ORCA still running" in body:
                if "[hf-keepalive]" in body:
                    return
                marker = "            next_heartbeat = time.monotonic() + heartbeat_every"
                if marker not in body:
                    return
                ping = '''            try:
                _urllib = __import__("urllib.request", fromlist=["Request", "urlopen"])
                _request = _urllib.Request(
                    "https://mc2hf1999-orcaweb.hf.space/health",
                    headers={"User-Agent": "orcaweb-kaggle-keepalive/1.0"},
                )
                with _urllib.urlopen(_request, timeout=20) as _response:
                    _status = getattr(_response, "status", "ok")
                log("[hf-keepalive] Hugging Face ping ok | status=%s" % _status)
            except Exception as _hf_exc:
                log("[hf-keepalive] Hugging Face ping failed: %s" % type(_hf_exc).__name__)
            next_heartbeat = time.monotonic() + heartbeat_every'''
                head, tail = body.rsplit(marker, 1)
                module.KAGGLE_RUNNER_BODY = head + ping + tail
                return
            _time.sleep(0.02)

    _threading.Thread(target=_install_hf_keepalive,
                      name="orcaweb-hf-keepalive-patch",
                      daemon=True).start()
except Exception:
    pass
