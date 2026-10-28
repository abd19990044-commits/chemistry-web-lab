"""Web adapters for the MIT ChemDraw drawing bot's chemistry algorithms.

No Telegram handlers, credentials, notifications or per-user bot state are used.
"""
from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from fractions import Fraction
from xml.etree import ElementTree as ET

from PIL import Image
from rdkit import Chem

import chem_core as core
from services.chemdraw import advanced_chemistry_v2 as advanced
from services.chemdraw import publication_renderer as molecular
from services.chemdraw import publication_renderer_v3 as reaction
from services.chemdraw.reaction_balancer import balance_reaction_smiles

# Like the source bot, bound CPU/memory use. HTTP clients get a retry response
# instead of joining an unbounded rendering queue in a web worker.
_RENDER_SLOT = threading.BoundedSemaphore(1)


class DrawingBusyError(RuntimeError):
    pass


@contextmanager
def drawing_slot():
    if not _RENDER_SLOT.acquire(blocking=False):
        raise DrawingBusyError('The chemistry drawing service is busy. Please retry shortly.')
    try:
        yield
    finally:
        _RENDER_SLOT.release()


def text_field(data, key, maximum=2000, required=False):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{key} must be text of at most {maximum} characters.')
    value = value.strip()
    if required and not value:
        raise ValueError(f'Please enter {key}.')
    return value


def drawing_options(data):
    style = data.get('drawing_style', 'smart')
    arrow = data.get('arrow_style', 'forward')
    transparent = data.get('transparent_background', False)
    if style not in ('smart', 'skeletal'):
        raise ValueError('Choose smart or skeletal drawing style.')
    if arrow not in ('forward', 'equilibrium', 'reversible'):
        raise ValueError('Choose a forward, equilibrium or reversible arrow.')
    if not isinstance(transparent, bool):
        raise ValueError('transparent_background must be true or false.')
    return dict(small_as_formula=style == 'smart', arrow_style=arrow,
                transparent_background=transparent,
                arrow_top=text_field(data, 'arrow_top', 240),
                arrow_bottom=text_field(data, 'arrow_bottom', 240))


def validate_smiles(smiles):
    if not isinstance(smiles, str) or not smiles or len(smiles) > 4096:
        raise ValueError('Use a valid SMILES of at most 4096 characters.')
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or not 0 < mol.GetNumAtoms() <= 200:
        raise ValueError('Each drawing must contain 1 to 200 atoms per species.')
    return mol


def validate_pairs(left, right):
    if not left or not right or len(left) + len(right) > 10:
        raise ValueError('Use at least one species per side and at most 10 species in total.')
    total = 0
    for coefficient, smiles in left + right:
        if not 0 < coefficient <= 100:
            raise ValueError('Drawing coefficients must be greater than zero and at most 100.')
        total += validate_smiles(smiles).GetNumAtoms()
    if total > 400:
        raise ValueError('A reaction drawing is limited to 400 atoms in total.')


def resolve_species(name):
    if name.startswith('smiles:'):
        raw = name[7:].strip()
        return Chem.MolToSmiles(validate_smiles(raw)), ''
    formula = core.smiles_from_common_formula(name)
    if formula:
        return formula, ''
    if not (name == name.upper() and name in core.AMBIGUOUS_TOKENS):
        mol = Chem.MolFromSmiles(name)
        if mol is not None:
            return Chem.MolToSmiles(mol), ''
    return core.resolve_species(name)


def explicit_export_pairs(data):
    sides = []
    for key in ('reactant_pairs', 'product_pairs'):
        entries = data.get(key)
        if not isinstance(entries, list) or not 0 < len(entries) <= 10:
            raise ValueError('Provide valid explicit reaction species for export.')
        pairs = []
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError('Each export species must contain coefficient and SMILES.')
            value = item.get('coefficient', '1')
            if isinstance(value, bool) or not isinstance(value, (str, int)) or len(str(value)) > 32:
                raise ValueError('Invalid export coefficient.')
            try:
                coefficient = Fraction(value)
            except (ValueError, ZeroDivisionError):
                raise ValueError('Invalid export coefficient.') from None
            pairs.append((coefficient, item.get('smiles')))
        sides.append(pairs)
    validate_pairs(*sides)
    return sides


def split_terms(text):
    return [core.ReactionTerm(term.coefficient, term.name)
            for term in molecular.split_reaction_side(text)]


def resolve_terms(data):
    left = split_terms(text_field(data, 'reactants', required=True))
    right = split_terms(text_field(data, 'products', required=True))
    if not left or not right or len(left) + len(right) > 10:
        raise ValueError('Use at least one species per side and at most 10 species in total.')
    resolved = {}
    for term in left + right:
        if term.name not in resolved:
            smiles, _ = resolve_species(term.name)
            if not smiles:
                raise ValueError(f'Could not resolve {term.name}. Try an explicit SMILES.')
            resolved[term.name] = smiles
    pairs = [[(t.coefficient, resolved[t.name]) for t in side] for side in (left, right)]
    validate_pairs(*pairs)
    return left, right, pairs


def automatically_balance(left_terms, right_terms, resolved):
    """Preserve already balanced coefficients; never invent missing species."""
    left = [(t.coefficient, resolved[t.name]) for t in left_terms]
    right = [(t.coefficient, resolved[t.name]) for t in right_terms]
    validate_pairs(left, right)
    original = core.check_reaction_balance(left, right)
    if original.get('balanced'):
        return left_terms, right_terms, 'Equation already balanced; entered coefficients preserved.'
    with drawing_slot():
        solution = balance_reaction_smiles([s for _, s in left], [s for _, s in right])
    if solution is None:
        return left_terms, right_terms, 'Automatic balancing could not find a positive solution using only these species.'
    balanced_terms = [[core.ReactionTerm(Fraction(c), term.name) for c, term in zip(coeffs, terms)]
                      for coeffs, terms in zip(solution, (left_terms, right_terms))]
    balanced_pairs = [[(t.coefficient, resolved[t.name]) for t in side] for side in balanced_terms]
    validate_pairs(*balanced_pairs)
    if not core.check_reaction_balance(*balanced_pairs).get('balanced'):
        raise ValueError('Automatic balancing did not pass element and charge verification.')
    return *balanced_terms, 'Automatically balanced with the smallest positive integer coefficients found; elements and charge verified.'


def transparent_svg(raw):
    """Remove white canvas rectangles, including backgrounds inside RDKit fragments."""
    root = ET.fromstring(raw)
    for parent in root.iter():
        for child in list(parent):
            if child.tag.rsplit('}', 1)[-1] != 'rect':
                continue
            style = child.get('style', '').replace(' ', '').lower()
            if child.get('fill', '').lower() in ('white', '#ffffff', '#fff') or 'fill:#ffffff' in style:
                parent.remove(child)
    return ET.tostring(root, encoding='utf-8')


def render_molecule_images(smiles, transparent=False):
    validate_smiles(smiles)
    with drawing_slot():
        image = molecular._molecule_png(smiles, bond_px=62)
        fragment = molecular._molecule_svg_fragment(smiles, bond_px=62)
        if image is None or fragment is None:
            raise ValueError('Could not render this molecule.')
        if transparent:
            image = reaction._transparentize(image)
        out = io.BytesIO()
        image.save(out, format='PNG', dpi=(600, 600))
        content, width, height = fragment
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
               f'viewBox="0 0 {width} {height}">{content}</svg>').encode()
        return out.getvalue(), transparent_svg(svg) if transparent else svg


def render_reaction_images(left, right, options):
    validate_pairs(left, right)
    with drawing_slot():
        png = reaction.render_reaction_png(left, right, **options)
        svg = reaction.render_reaction_svg(left, right, **options)
        if png is None or svg is None:
            raise ValueError('Could not render this reaction.')
        if options['transparent_background']:
            svg = transparent_svg(svg)
        return png, svg


def image_export(png, svg, format):
    if format == 'svg':
        return svg, 'image/svg+xml', 'svg'
    if format == 'png':
        return png, 'image/png', 'png'
    if format != 'tiff':
        raise ValueError('Choose PNG, SVG or TIFF.')
    with drawing_slot():
        out = io.BytesIO()
        with Image.open(io.BytesIO(png)) as image:
            image.save(out, format='TIFF', compression='tiff_lzw', dpi=(600, 600))
        return out.getvalue(), 'image/tiff', 'tiff'


def advanced_reaction(data):
    action = text_field(data, 'action', 32, required=True)
    if action not in ('redox_acidic', 'redox_basic', 'net_ionic', 'atom_map'):
        raise ValueError('Unknown advanced chemistry action.')
    terms_left, terms_right, (left, right) = resolve_terms(data)
    with drawing_slot():
        if action.startswith('redox_'):
            medium = action.removeprefix('redox_')
            result = advanced.balance_redox([s for _, s in left], [s for _, s in right], medium)
            if result is None:
                raise ValueError('No unique conservative redox balance was found for these species and this medium.')
            def side(species, terms):
                return ' + '.join(('' if s.coefficient == 1 else f'{s.coefficient} ') +
                                  (terms[i].name if i < len(terms) else s.label)
                                  for i, s in enumerate(species))
            return dict(text=f'{side(result.reactants, terms_left)} → {side(result.products, terms_right)}',
                        note=f'{medium.capitalize()} medium. Only water, the medium ion and electrons may be added. This is stoichiometric balancing, not a prediction of reaction feasibility.')
        if action == 'net_ionic':
            if not core.check_reaction_balance(left, right).get('balanced'):
                raise ValueError('Balance the entered equation before reducing it to a net ionic equation.')
            result = advanced.net_ionic_equation(left, right)
            if result is None:
                raise ValueError('No net ionic equation remains after spectator cancellation, or the electrolytes are unsupported.')
            return dict(text=advanced.format_net_ionic(result), spectators=list(result.cancelled),
                        note='Aqueous strong electrolytes and explicitly ionic salts only. Check solubility and phases before applying this representation.')
        if any(c != 1 for c, _ in left + right):
            raise ValueError('Atom mapping requires unit stoichiometric coefficients.')
        result = advanced.conservative_atom_map([s for _, s in left], [s for _, s in right])
        if result is None:
            raise ValueError('Mapping requires one reactant and one product with a unique common-substructure orientation. Ambiguous symmetric matches are rejected.')
        return dict(text=result, note='Partial common-substructure atom mapping. Unmatched atoms get separate numbers; this does not infer a mechanism.')


def mechanism(data):
    smiles = text_field(data, 'smiles', 4096, required=True)
    validate_smiles(smiles)
    spec = text_field(data, 'arrows', 512)
    arrows = advanced.parse_mechanism_arrows(spec) if spec else []
    if arrows is None or len(arrows) > 32:
        raise ValueError('Use at most 32 atom-to-atom arrows, e.g. 1->2,3~>2.')
    with drawing_slot():
        svg = advanced.render_mechanism_svg(smiles, arrows)
    if not svg:
        raise ValueError('Arrow indices must refer to distinct existing atoms. Preview atom numbers first.')
    return svg
