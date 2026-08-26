# -*- coding: utf-8 -*-
"""Headless Chrome Browser Test for Optimization Output -> 3D Builder -> Input Generator flow."""

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
    def __init__(self, app, port=5150):
        super().__init__()
        self.server = make_server("127.0.0.1", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


def test_opt_to_builder_flow():
    server = ServerThread(app, port=5150)
    server.start()
    time.sleep(1.2)

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=chrome_options)
    try:
        url = "http://127.0.0.1:5150/"
        driver.get(url)
        time.sleep(2)

        # 1. Switch to Quantum Calculation Analyzer view
        driver.execute_script("window.showChemistryView('quantum', 'analyzer');")
        time.sleep(0.5)

        # 2. Simulate loading a converged Geometry Optimization output
        mock_opt_payload = """
        window.loadMockOptCalculation = function() {
            const optData = {
                ok: true,
                name: "water_opt.out",
                raw_text: "*** OPTIMIZATION RUN DONE ***\\n   THE OPTIMIZATION HAS CONVERGED\\n   FINAL ENERGY EVALUATION AT THE STATIONARY POINT",
                latest_job: {
                    name: "water_opt",
                    calculation_type: "Geometry Optimization (Opt)",
                    orca_version: "6.1.0",
                    functional: "B3LYP",
                    basis_set: "def2-TZVP",
                    chemical_formula: "H2O",
                    termination_status: "NORMAL",
                    total_energy_eh: -76.432581,
                    elements: ["O", "H", "H"],
                    coords: [
                        [0.000000, 0.000000, 0.117300],
                        [0.000000, 0.757200, -0.469200],
                        [0.000000, -0.757200, -0.469200]
                    ],
                    xyz: "3\\nWater Optimized\\nO   0.000000   0.000000   0.117300\\nH   0.000000   0.757200  -0.469200\\nH   0.000000  -0.757200  -0.469200"
                }
            };
            if (window.renderEngineResults) {
                window.renderEngineResults(optData);
            }
        };
        window.loadMockOptCalculation();
        """
        driver.execute_script(mock_opt_payload)
        time.sleep(1.0)

        # 3. Verify Optimization Callout Banner is visible
        opt_banner = driver.find_element(By.ID, "engine-opt-banner")
        banner_classes = opt_banner.get_attribute("class")
        print(f"[OK] Optimization banner classes: '{banner_classes}'")
        assert "hidden" not in banner_classes, "Optimization banner should be visible!"

        # 4. Verify New Calculation buttons
        opt_launch_btn = driver.find_element(By.ID, "engine-opt-launch-btn")
        new_calc_btn = driver.find_element(By.ID, "engine-3d-new-calc-btn")
        print("[OK] Found both optimization launch button and 3D viewer footer button.")

        # 5. Click the launch button to feed geometry into 3D builder
        driver.execute_script("arguments[0].click();", opt_launch_btn)
        time.sleep(1.2)

        # 6. Verify ORCA view is active and 3D builder modal is open
        orca_view = driver.find_element(By.ID, "orca-view")
        assert "is-active" in orca_view.get_attribute("class"), "ORCA view should be active!"

        viewport = driver.find_elements(By.ID, "builder3d-viewport")
        assert len(viewport) > 0, "3D builder viewport not mounted!"
        print("[OK] 3D builder viewport successfully mounted with optimized geometry.")

        formula_val = driver.find_element(By.ID, "builder3d-formula-val").text
        atom_count = driver.find_element(By.ID, "builder3d-atom-count").text
        print(f"[OK] Builder Molecule Formula: {formula_val}, Atom Count: {atom_count}")
        assert formula_val == "H2O"
        assert atom_count == "3"

        # 7. Click Proceed to Calculation Setup
        proceed_btn = driver.find_element(By.XPATH, "//button[contains(., 'Confirm 3D Structure')]")
        driver.execute_script("arguments[0].click();", proceed_btn)
        time.sleep(0.8)

        modal_title = driver.find_element(By.ID, "orca-modal-title").text
        print(f"[OK] Modal step after confirmation: '{modal_title}'")
        assert "Calculation" in modal_title or "calculation" in modal_title.lower()

        # 8. Check console logs
        logs = driver.get_log("browser")
        severe = [l for l in logs if l.get("level") == "SEVERE"]
        print(f"[OK] Console severe errors: {len(severe)}")
        assert len(severe) == 0

        print("\n=========================================================================")
        print("OPTIMIZED GEOMETRY -> 3D BUILDER -> INPUT GENERATOR FLOW FULLY VERIFIED!")
        print("=========================================================================\n")

    finally:
        driver.quit()
        server.shutdown()


if __name__ == "__main__":
    test_opt_to_builder_flow()
