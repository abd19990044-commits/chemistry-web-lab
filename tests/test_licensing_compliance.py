"""The MIT application license preserves independent third-party terms."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_third_party_inventory_and_orca_boundary():
    text = (ROOT / 'THIRD_PARTY_LICENSES.md').read_text(encoding='utf-8')
    for component in ('Flask', 'RDKit', 'Requests', '3Dmol.js', 'FastAPI', 'fpdf2', 'LGPL-3.0-only'):
        assert component in text
    assert 'BSD-3-Clause' in text
    assert 'Apache-2.0' in text
    assert 'ORCA Quantum Chemistry Software Boundary' in text
    assert 'NOT Bundled' in text
    assert 'Zero Rights Grant' in text
    assert 'User Scientific Outputs' in text


def test_third_party_api_keeps_orca_independent():
    from app import app
    with app.test_client() as client:
        payload = client.get('/api/third-party-licenses').get_json()
    assert payload['orca_boundary']['bundled'] is False
    assert 'Proprietary' in payload['orca_boundary']['license_type']
    assert payload['python_dependencies']


def test_runtime_parser_remains_usable():
    from orca_engine import OrcaParser
    jobs = OrcaParser(iter(['Program Version 6.1.0',
                            'FINAL SINGLE POINT ENERGY -76.360770002990',
                            'ORCA TERMINATED NORMALLY']), source_name='sample.out').parse()
    assert len(jobs) == 1
    assert abs(jobs[0].e_elec_eh + 76.360770002990) < 1e-10
