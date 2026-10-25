"""The cold browser-engine download is compressed without changing its bytes."""
import gzip
from pathlib import Path

import pytest

import app as web
import fetch_browser_engine as engine


@pytest.mark.parametrize('name,mime',[('openbabel.wasm','application/wasm'),('openbabel.data','application/octet-stream'),('openbabel.js','application/javascript')])
def test_static_engine_negotiates_gzip_and_preserves_original_bytes(tmp_path, monkeypatch, name, mime):
    root=tmp_path/'vendor'/'openbabel'
    root.mkdir(parents=True)
    data=(b'known-engine-bytes'*500)
    (root/name).write_bytes(data)
    compressed=gzip.compress(data,mtime=0)
    (root/(name+'.gz')).write_bytes(compressed)
    monkeypatch.setattr(web,'STATIC_DIR',str(tmp_path))
    with web.app.test_client() as client:
        response=client.get('/static/vendor/openbabel/'+name,headers={'Accept-Encoding':'gzip'})
        assert response.status_code==200
        assert response.headers['Content-Encoding']=='gzip'
        assert response.mimetype==mime
        assert 'Accept-Encoding' in response.vary
        assert len(response.data)<len(data)
        assert gzip.decompress(response.data)==data
        plain=client.get('/static/vendor/openbabel/'+name,headers={'Accept-Encoding':'gzip;q=0'})
        assert 'Content-Encoding' not in plain.headers
        assert plain.data==data
        assert 'Accept-Encoding' in plain.vary
        partial=client.get('/static/vendor/openbabel/'+name,headers={'Accept-Encoding':'gzip','Range':'bytes=0-9'})
        assert partial.status_code==206
        assert 'Content-Encoding' not in partial.headers
        assert partial.data==data[:10]


def test_installer_precompresses_existing_verified_assets_without_network(tmp_path):
    import hashlib
    payload=b'verified native asset'*1000
    (tmp_path/'engine.wasm').write_bytes(payload)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(engine,'ASSETS',{'engine.wasm':hashlib.sha256(payload).hexdigest()})
        monkeypatch.setattr(engine.urllib.request,'urlopen',lambda *a,**k:pytest.fail('Already verified files must not be downloaded again'))
        engine.install(tmp_path)
        first=(tmp_path/'engine.wasm.gz').read_bytes()
        engine.install(tmp_path)
        assert (tmp_path/'engine.wasm.gz').read_bytes()==first
        assert gzip.decompress(first)==payload
