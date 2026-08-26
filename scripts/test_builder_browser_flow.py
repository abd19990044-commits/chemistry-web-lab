# -*- coding: utf-8 -*-
"""Headless Chrome Browser Test for 3D Molecular Builder & Multi-Molecule Assembly."""

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
    def __init__(self, app, port=5145):
        super().__init__()
        self.server = make_server("127.0.0.1", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


def test_builder_flow():
    server = ServerThread(app, port=5145)
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
        url = "http://127.0.0.1:5145/"
        driver.get(url)
        time.sleep(2)

        # Open ORCA workspace
        driver.execute_script("window.showChemistryView('orca');")
        time.sleep(0.5)

        # Click Open Wizard
        open_wizard_btn = driver.find_element(By.ID, "orca-open-wizard")
        open_wizard_btn.click()
        time.sleep(0.5)

        # Switch to Manual tab (3rd tab)
        modal_tabs = driver.find_elements(By.CSS_SELECTOR, "#orca-modal .modal-tab")
        print(f"[OK] Found {len(modal_tabs)} wizard input tabs.")
        assert len(modal_tabs) >= 3
        modal_tabs[2].click()
        time.sleep(0.3)

        # Enter manual XYZ coordinates
        textarea = driver.find_element(By.CSS_SELECTOR, "#orca-modal textarea")
        textarea.clear()
        textarea.send_keys("O 0.0000 0.0000 0.1173\nH 0.0000 0.7572 -0.4692\nH 0.0000 -0.7572 -0.4692")
        time.sleep(0.2)

        # Click Continue to 3D Builder in tabManual
        manual_continue_btn = driver.find_element(By.XPATH, "//button[contains(., 'Continue to 3D Builder')]")
        driver.execute_script("arguments[0].click();", manual_continue_btn)
        time.sleep(1.2)

        # Verify 3D builder UI elements
        viewport = driver.find_elements(By.ID, "builder3d-viewport")
        assert len(viewport) > 0, "3D builder viewport not found!"
        print("[OK] 3D builder viewport successfully mounted in DOM.")

        formula_val = driver.find_element(By.ID, "builder3d-formula-val").text
        atom_count = driver.find_element(By.ID, "builder3d-atom-count").text
        print(f"[OK] 3D Builder Formula: {formula_val}, Atom Count: {atom_count}")
        assert formula_val == "H2O"
        assert atom_count == "3"

        # Verify toolbar tools
        tool_btns = driver.find_elements(By.CLASS_NAME, "builder3d-toolbtn")
        print(f"[OK] Found {len(tool_btns)} 3D builder toolbar tools.")
        assert len(tool_btns) >= 5

        # Click Proceed to Calculation step
        proceed_btn = driver.find_element(By.XPATH, "//button[contains(., 'Confirm 3D Structure')]")
        proceed_btn.click()
        time.sleep(0.8)

        # Verify transition to Step 3 (Calculation Options)
        modal_title = driver.find_element(By.ID, "orca-modal-title").text
        print(f"[OK] Modal step after confirming 3D builder: '{modal_title}'")
        assert "Calculation" in modal_title or "calculation" in modal_title.lower()

        # Check console logs
        logs = driver.get_log("browser")
        severe = [l for l in logs if l.get("level") == "SEVERE"]
        print(f"[OK] Browser console severe errors count: {len(severe)}")
        assert len(severe) == 0

        print("\n=======================================================")
        print("3D BUILDER BROWSER WORKFLOW VERIFIED 100% SUCCESSFULLY!")
        print("=======================================================\n")

    finally:
        driver.quit()
        server.shutdown()


if __name__ == "__main__":
    test_builder_flow()
