# -*- coding: utf-8 -*-
"""Verification script for dynamic engine version in Calculation Analyzer and Publication Exporter."""

import os
import sys
import time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import app
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from werkzeug.serving import make_server


class ServerThread(threading.Thread):
    def __init__(self, app, port=5142):
        super().__init__()
        self.server = make_server("127.0.0.1", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


def test_engine_version():
    server = ServerThread(app, port=5142)
    server.start()
    time.sleep(1.0)

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=chrome_options)
    try:
        url = "http://127.0.0.1:5142/"
        driver.get(url)
        time.sleep(2)

        # Switch to quantum view
        driver.execute_script("window.showChemistryView('quantum-view');")
        time.sleep(0.5)

        # Test with an ORCA 5.0.4 output
        mock504 = """
          * O   R   C   A *
          Program Version 5.0.4 - RELEASE  -
          FINAL SINGLE POINT ENERGY -76.432100000000
          Total Charge           Charge          ....    0
          Multiplicity           Mult            ....    1
          Number of Atoms        NAtoms          ....    3
          ORCA TERMINATED NORMALLY
        """
        driver.execute_script(f"document.getElementById('engine-paste-text').value = `{mock504}`;")
        time.sleep(0.2)
        driver.execute_script("document.getElementById('engine-submit-btn').click();")
        time.sleep(1.5)

        # Check the overview badge
        version_badge_504 = driver.find_element(By.ID, "engine-version-badge")
        print(f"ORCA 5.0.4 Overview Badge: '{version_badge_504.text}'")
        assert "5.0.4" in version_badge_504.text

        # Test image export engine label
        img_label_504 = driver.execute_script("""
            const job = currentEngineData.latest_job || {};
            let orcaVer = job.orca_version || currentEngineData?.orca_version || (currentEngineData?.latest_job && currentEngineData?.latest_job?.orca_version);
            if (!orcaVer || orcaVer === "Unknown" || orcaVer === "undefined") {
              const raw = currentEngineData?.raw_text || currentEngineData?.content || currentEngineData?.output || "";
              const vMatch = raw.match(/Program\\s+Version\\s+([\\d.]+)/i) || raw.match(/ORCA[^\\d\\n]*([\\d.]+)/i);
              if (vMatch) orcaVer = vMatch[1];
            }
            return orcaVer.toLowerCase().startsWith("orca") ? orcaVer : `ORCA ${orcaVer}`;
        """)
        print(f"ORCA 5.0.4 Image Label: '{img_label_504}'")
        assert img_label_504 == "ORCA 5.0.4"

        # Now test with an ORCA 6.1.0 output
        mock610 = """
          * O   R   C   A *
          Program Version 6.1.0  - RELEASE -
          FINAL SINGLE POINT ENERGY -76.432100000000
          Total Charge           Charge          ....    0
          Multiplicity           Mult            ....    1
          Number of Atoms        NAtoms          ....    3
          ORCA TERMINATED NORMALLY
        """
        driver.execute_script(f"document.getElementById('engine-paste-text').value = `{mock610}`;")
        time.sleep(0.2)
        driver.execute_script("document.getElementById('engine-submit-btn').click();")
        time.sleep(1.5)

        version_badge_610 = driver.find_element(By.ID, "engine-version-badge")
        print(f"ORCA 6.1.0 Overview Badge: '{version_badge_610.text}'")
        assert "6.1.0" in version_badge_610.text

        img_label_610 = driver.execute_script("""
            const job = currentEngineData.latest_job || {};
            let orcaVer = job.orca_version || currentEngineData?.orca_version || (currentEngineData?.latest_job && currentEngineData?.latest_job?.orca_version);
            if (!orcaVer || orcaVer === "Unknown" || orcaVer === "undefined") {
              const raw = currentEngineData?.raw_text || currentEngineData?.content || currentEngineData?.output || "";
              const vMatch = raw.match(/Program\\s+Version\\s+([\\d.]+)/i) || raw.match(/ORCA[^\\d\\n]*([\\d.]+)/i);
              if (vMatch) orcaVer = vMatch[1];
            }
            return orcaVer.toLowerCase().startsWith("orca") ? orcaVer : `ORCA ${orcaVer}`;
        """)
        print(f"ORCA 6.1.0 Image Label: '{img_label_610}'")
        assert img_label_610 == "ORCA 6.1.0"

        print("\nALL DYNAMIC ENGINE VERSION TESTS PASSED SUCCESSFULLY!")

    finally:
        driver.quit()
        server.shutdown()


if __name__ == "__main__":
    test_engine_version()
