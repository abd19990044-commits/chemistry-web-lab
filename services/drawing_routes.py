"""Flask transport for the transferred ChemDraw features."""
from __future__ import annotations

import base64
import io

from flask import Blueprint, jsonify, request, send_file

from services import drawing_service as drawing


def register_drawing_routes(app):
    routes = Blueprint('chemdraw', __name__, url_prefix='/api/drawing')

    @routes.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(ok=False, error=str(exc), code='INVALID_DRAWING'), 422

    @routes.errorhandler(drawing.DrawingBusyError)
    def busy(exc):
        return jsonify(ok=False, error=str(exc), code='DRAWING_BUSY'), 429, {'Retry-After': '2'}

    @routes.post('/advanced')
    def advanced():
        result = drawing.advanced_reaction(request.get_json(silent=True) or {})
        return jsonify(ok=True, **result)

    @routes.post('/mechanism')
    def mechanism():
        svg = drawing.mechanism(request.get_json(silent=True) or {})
        return jsonify(ok=True, image_svg_base64=base64.b64encode(svg).decode('ascii'))

    @routes.post('/export')
    def export():
        data = request.get_json(silent=True) or {}
        format = drawing.text_field(data, 'format', 8, required=True)
        if format not in ('png', 'svg', 'tiff'):
            raise ValueError('Choose PNG, SVG or TIFF.')
        kind = data.get('kind', 'molecule')
        options = drawing.drawing_options(data)
        if kind == 'molecule':
            smiles = drawing.text_field(data, 'smiles', 4096, required=True)
            png, svg = drawing.render_molecule_images(smiles, options['transparent_background'])
        elif kind == 'reaction':
            if 'reactant_pairs' in data or 'product_pairs' in data:
                left, right = drawing.explicit_export_pairs(data)
                lt = rt = None
            else:
                lt, rt, (left, right) = drawing.resolve_terms(data)
            if lt is not None and data.get('auto_balance') is True:
                resolved = {t.name: s for terms, pairs in ((lt, left), (rt, right)) for t, (_, s) in zip(terms, pairs)}
                lt, rt, _ = drawing.automatically_balance(lt, rt, resolved)
                left, right = [[(t.coefficient, resolved[t.name]) for t in terms] for terms in (lt, rt)]
            png, svg = drawing.render_reaction_images(left, right, options)
        else:
            raise ValueError('Unknown drawing kind.')
        raw, mime, extension = drawing.image_export(png, svg, format)
        response = send_file(io.BytesIO(raw), mimetype=mime, as_attachment=True,
                             download_name=f'{kind}-drawing.{extension}', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response

    app.register_blueprint(routes)
