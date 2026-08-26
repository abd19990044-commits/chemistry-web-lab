# -*- coding: utf-8 -*-
import os
import sys
import json
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app

c = app.test_client()
r = c.get('/')
html = r.data.decode('utf-8')

print("HTTP Status:", r.status_code)
idx1 = html.find('id="orca-config"')
if idx1 != -1:
    idx2 = html.find('</script>', idx1)
    txt = html[html.find('>', idx1)+1:idx2].strip()
    try:
        cfg = json.loads(txt)
        print("orca-config is VALID JSON! Keys:", list(cfg.keys()))
    except Exception as e:
        print("orca-config JSON ERROR:", e)
        print("Content:", txt)
else:
    print("orca-config NOT FOUND!")
