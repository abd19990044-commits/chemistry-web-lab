"""ChemDraw parity, scientific guards, web transport and geometry file exports."""
from fractions import Fraction
import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
from xml.etree import ElementTree as ET

from PIL import Image
import pytest
from rdkit import Chem

from services import drawing_service as drawing
from services.chemdraw import advanced_chemistry_v2 as advanced
from services.chemdraw.reaction_balancer import balance_reaction_smiles


def test_source_parser_preserves_ionic_smiles_and_rational_coefficients():
    terms = drawing.split_terms('1/2 [Fe+2] + 0.5 [OH-]')
    assert [(t.coefficient, t.name) for t in terms] == [(Fraction(1, 2), '[Fe+2]'), (Fraction(1, 2), '[OH-]')]
    assert [t.name for t in drawing.split_terms('NH4+ + OH-')] == ['NH4+', 'OH-']


def test_exact_balancer_conserves_elements_and_charge_and_does_not_invent_species():
    assert balance_reaction_smiles(['[H][H]', 'O=O'], ['O']) == ([2, 1], [2])
    assert balance_reaction_smiles(['[Fe+2]'], ['[Fe+3]']) is None
    assert balance_reaction_smiles(['CCO'], ['C']) is None


def test_already_balanced_fractional_coefficients_are_preserved():
    left, right = drawing.split_terms('1/2 O2 + H2'), drawing.split_terms('H2O')
    result = drawing.automatically_balance(left, right, {'O2': 'O=O', 'H2': '[H][H]', 'H2O': 'O'})
    assert result[:2] == (left, right)
    assert 'preserved' in result[2]


def test_redox_half_reaction_and_net_ionic_parity(monkeypatch):
    monkeypatch.setattr(drawing.core, 'resolve_species', lambda name: (name, ''))
    result = drawing.advanced_reaction(dict(action='redox_acidic', reactants='[Fe+2]', products='[Fe+3]'))
    assert result['text'] == '[Fe+2] → [Fe+3] + e-'
    result = drawing.advanced_reaction(dict(action='net_ionic', reactants='Cl + [Na+].[OH-]', products='[Na+].[Cl-] + O'))
    assert result['text'] == 'H+ + OH- → H2O'
    assert set(result['spectators']) == {'Na+', 'Cl-'}
    with pytest.raises(ValueError, match='Balance'):
        drawing.advanced_reaction(dict(action='net_ionic', reactants='2 Cl + [Na+].[OH-]', products='[Na+].[Cl-] + O'))


def test_mapping_rejects_ambiguous_symmetry_and_isotope_changes():
    assert advanced.conservative_atom_map(['CCO'], ['CCO'])
    assert advanced.conservative_atom_map(['c1ccccc1'], ['c1ccccc1']) is None
    assert advanced.conservative_atom_map(['[13CH3]CO'], ['CCO']) is None


def test_mechanism_is_numbered_and_only_contains_requested_arrows():
    raw = drawing.mechanism(dict(smiles='CCO', arrows='1->2,3~>2'))
    root = ET.fromstring(raw)
    paths = [el for el in root.iter() if el.get('marker-end')]
    assert [el.get('marker-end') for el in paths] == ['url(#pairArrow)', 'url(#fishArrow)']
    assert not [el for el in ET.fromstring(drawing.mechanism(dict(smiles='CCO'))).iter() if el.get('marker-end')]
    for invalid in ('1->1', '1->99', 'bad'):
        with pytest.raises(ValueError):
            drawing.mechanism(dict(smiles='CCO', arrows=invalid))


@pytest.mark.parametrize('kind', ['molecule', 'reaction'])
def test_transparent_png_svg_and_tiff_retain_real_dpi(kind):
    if kind == 'molecule':
        png, svg = drawing.render_molecule_images('CC(=O)O', True)
    else:
        png, svg = drawing.render_reaction_images([(1, 'C=C'), (1, '[H][H]')], [(1, 'CC')], drawing.drawing_options({'transparent_background': True}))
    image = Image.open(io.BytesIO(png))
    assert image.mode == 'RGBA' and image.getpixel((0, 0))[3] == 0
    assert image.info['dpi'][0] == pytest.approx(600, abs=.1)
    root = ET.fromstring(svg)
    assert not [e for e in root.iter() if e.tag.endswith('rect') and ('#ffffff' in e.get('style', '').lower() or e.get('fill') == 'white')]
    raw, mime, ext = drawing.image_export(png, svg, 'tiff')
    assert (mime, ext) == ('image/tiff', 'tiff')
    tiff = Image.open(io.BytesIO(raw))
    assert tiff.size == image.size and tiff.mode == 'RGBA'
    assert float(tiff.info['dpi'][0]) == pytest.approx(600)


def test_busy_slot_is_bounded_and_released_after_errors():
    with drawing.drawing_slot():
        with pytest.raises(drawing.DrawingBusyError):
            drawing.render_molecule_images('CCO')
    with pytest.raises(ValueError):
        with drawing.drawing_slot():
            raise ValueError('test')
    with drawing.drawing_slot():
        pass


@pytest.fixture
def client(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.core, 'resolve_species', lambda name: ({'H2': '[H][H]', 'O2': 'O=O', 'H2O': 'O'}.get(name, name), ''))
    app_module.app.config['TESTING'] = True
    return app_module.app.test_client()


def test_reaction_api_returns_balanced_render_and_immutable_export_snapshot(client):
    result = client.post('/api/reaction', json=dict(reactants='H2 + O2', products='H2O', auto_balance=True, drawing_style='smart', transparent_background=True, arrow_top='Pt'))
    assert result.status_code == 200, result.json
    data = result.json
    assert data['balance']['balanced']
    assert data['equation'] == '2 H2 + O2 -> 2 H2O'
    assert data['render_request']['reactant_pairs'] == [{'coefficient': '2', 'smiles': '[H][H]'}, {'coefficient': '1', 'smiles': 'O=O'}]
    assert data['render_request']['transparent_background'] is True
    exported = client.post('/api/drawing/export', json={**data['render_request'], 'format': 'tiff'})
    assert exported.status_code == 200
    assert exported.mimetype == 'image/tiff'
    assert 'attachment' in exported.headers['Content-Disposition']


def test_advanced_routes_and_validation(client):
    assert client.post('/api/drawing/advanced', json=dict(action='redox_acidic', reactants='[Fe+2]', products='[Fe+3]')).json['text'].endswith('+ e-')
    assert client.post('/api/drawing/advanced', json=dict(action='unknown', reactants='CCO', products='CCO')).status_code == 422
    assert client.post('/api/drawing/mechanism', json=dict(smiles='CCO', arrows='1->99')).status_code == 422
    assert client.post('/api/drawing/export', json=dict(smiles='CCO', format='exe')).status_code == 422
    assert client.post('/api/drawing/export', json=dict(smiles='CCO', format='png', transparent_background='yes')).status_code == 422
    assert client.post('/api/drawing/advanced', json=['invalid']).status_code == 400


def test_geometry_downloads_are_distinct_formats_and_preserve_topology(tmp_path):
    if not shutil.which('node'):
        pytest.skip('Node required')
    root = Path(__file__).resolve().parents[1]
    source = r'''
require('./static/js/molecule-graph.js');
const atoms=[{id:19,elem:'N',charge:1,x:1.234567891234,y:-2,z:3},{id:42,elem:'C',x:2,y:-2,z:3},{id:88,elem:'O',x:3,y:-2,z:3},{id:99,elem:'Cl',charge:-1,x:8,y:0,z:0}];
const bonds=[{a1:19,a2:42,order:1},{a1:42,a2:88,order:2}];
const before=JSON.stringify({atoms,bonds}),files={};
for(const f of ['mol','sdf','xyz'])files[f]=MoleculeGraph.serialize(atoms,bonds,f,'Test molecule','source');
require('node:assert/strict').equal(JSON.stringify({atoms,bonds}),before);
require('node:assert/strict').throws(()=>MoleculeGraph.serialize(atoms,bonds,'exe'));
require('node:assert/strict').throws(()=>MoleculeGraph.serialize([{id:1,elem:'C',x:1e10,y:0,z:0}],[],'mol'));
console.log(JSON.stringify(files));
'''
    result = subprocess.run(['node', '-e', source], cwd=root, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    files = json.loads(result.stdout)
    assert files['mol']['text'].endswith('M  END\n') and '$$$$' not in files['mol']['text']
    assert files['sdf']['text'].endswith('$$$$\n') and '<TOPOLOGY_STATUS>\nsource' in files['sdf']['text']
    assert files['xyz']['text'].splitlines()[0] == '4'
    assert '1.234567891234' in files['xyz']['text']
    molecule = Chem.MolFromMolBlock(files['mol']['text'], sanitize=False, removeHs=False)
    assert molecule.GetNumAtoms() == 4
    assert molecule.GetAtomWithIdx(0).GetFormalCharge() == 1
    assert molecule.GetAtomWithIdx(3).GetFormalCharge() == -1
    assert molecule.GetBondWithIdx(1).GetBondTypeAsDouble() == 2
    assert molecule.GetConformer().Is3D()
    assert molecule.GetConformer().GetAtomPosition(0).x == pytest.approx(1.2346)


def test_editor_installer_checks_integrity_and_produces_deterministic_compression(tmp_path, monkeypatch):
    import hashlib
    import zipfile
    import fetch_ketcher_editor as installer
    archive = tmp_path / 'editor.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('index.html', '<html>editor</html>')
        z.writestr('static/js/main.fixture.js', '/* fixture */')
        z.writestr('static/js/main.fixture.js.LICENSE.txt', 'license fixture')
        z.writestr('static/css/main.fixture.css', 'body{margin:0}')
        z.writestr('popup.html', '<html>not needed</html>')
    destination = tmp_path / 'editor'
    with pytest.raises(ValueError, match='Integrity'):
        installer.install(destination, archive)
    assert not destination.exists()
    monkeypatch.setattr(installer, 'ARCHIVE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    installer.install(destination, archive)
    assert not (destination / 'popup.html').exists()
    assert gzip.decompress((destination / 'static/js/main.fixture.js.gz').read_bytes()) == b'/* fixture */'
    first = (destination / 'static/js/main.fixture.js.gz').read_bytes()
    installer.install(destination, archive)
    assert first == (destination / 'static/js/main.fixture.js.gz').read_bytes()
    assert (destination / 'LICENSE').read_text().startswith('                                 Apache License')
    assert json.loads((destination / 'installed.json').read_text())['version'] == '3.18.0'


def test_only_editor_document_allows_same_origin_framing_and_gzip(client, monkeypatch, tmp_path):
    import app as module
    monkeypatch.setattr(module, '_IS_HF_SPACE', False)
    monkeypatch.setattr(module, 'STATIC_DIR', str(tmp_path))
    destination = tmp_path / 'vendor/ketcher'
    destination.mkdir(parents=True)
    raw = b'<html>editor fixture</html>'
    (destination / 'index.html').write_bytes(raw)
    (destination / 'index.html.gz').write_bytes(gzip.compress(raw))
    response = client.get('/static/vendor/ketcher/index.html', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200
    assert gzip.decompress(response.data) == raw
    assert response.headers['Content-Encoding'] == 'gzip'
    assert response.headers['X-Frame-Options'] == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in response.headers['Content-Security-Policy']
    page = client.get('/lab')
    assert page.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in page.headers['Content-Security-Policy']


def test_explicit_exports_preserve_smiles_that_are_ambiguous_as_formulas(client, monkeypatch):
    import services.drawing_routes as routes
    seen = []
    def render(left, right, options):
        seen.append((left, right))
        return b'png', b'<svg/>'
    monkeypatch.setattr(routes.drawing, 'render_reaction_images', render)
    result = client.post('/api/drawing/export', json=dict(kind='reaction', format='svg', reactant_pairs=[dict(coefficient='1', smiles='CO')], product_pairs=[dict(coefficient='1', smiles='CO')]))
    assert result.status_code == 200
    assert seen == [([(Fraction(1), 'CO')], [(Fraction(1), 'CO')])]
    assert drawing.resolve_species('smiles:CO')[0] == 'CO'
