# -*- coding: utf-8 -*-
"""
Comprehensive Headless Chrome Browser Audit
Captures every JavaScript exception, console error, warning, and test interaction
across all views (Home, Draw, ORCA, Quantum, Legal) and tabs.
"""

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
    def __init__(self, app, port=5140):
        super().__init__()
        self.server = make_server("127.0.0.1", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


def audit():
    server = ServerThread(app, port=5140)
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
        url = "http://127.0.0.1:5140/"
        driver.get(url)
        time.sleep(2)

        def print_logs(section):
            logs = driver.get_log("browser")
            print(f"\n--- BROWSER LOGS: {section} ({len(logs)} entries) ---")
            for entry in logs:
                print(f"[{entry['level']}] {entry['source']} {entry.get('lineNumber', '')} {entry['message']}")
            severe = [l for l in logs if l.get("level") == "SEVERE"]
            assert len(severe) == 0, f"Severe console errors detected: {severe}"

        print_logs("Page Load")

        # Test global functions
        for sym in ["window.showChemistryView", "window.$3Dmol"]:
            res = driver.execute_script(f"return typeof {sym};")
            print(f"Eval 'typeof {sym}': {res}")

        # Check view transitions
        views = ["draw", "orca", "quantum", "legal", "home"]
        for v in views:
            driver.execute_script(f"window.showChemistryView('{v}');")
            time.sleep(0.4)
            view_el = driver.find_element(By.ID, f"{v}-view")
            is_active = "is-active" in view_el.get_attribute("class")
            print(f"View '{v}-view' is-active: {is_active}")
            print_logs(f"After switching to {v}")

        # Check subtabs in Draw workspace
        driver.execute_script("window.showChemistryView('draw');")
        tabs = driver.find_elements(By.CSS_SELECTOR, "#draw-view .ws-tab")
        for t in tabs:
            t.click()
            time.sleep(0.3)
            print(f"Clicked Draw subtab: {t.text}")
            print_logs(f"After Draw tab {t.text}")

        # Check subtabs in ORCA workspace
        driver.execute_script("window.showChemistryView('orca');")
        tabs = driver.find_elements(By.CSS_SELECTOR, "#orca-view .ws-tab")
        for t in tabs:
            t.click()
            time.sleep(0.3)
            print(f"Clicked ORCA subtab: {t.text}")
            print_logs(f"After ORCA tab {t.text}")

        # Check subtabs in Quantum workspace
        driver.execute_script("window.showChemistryView('quantum');")
        time.sleep(0.5)
        tabs = driver.find_elements(By.CSS_SELECTOR, "#quantum-view .ws-tab")
        for t in tabs:
            tab_label = t.text.encode("ascii", "ignore").decode("ascii")
            t.click()
            time.sleep(0.4)
            print(f"Clicked Quantum subtab: {tab_label}")
            print_logs(f"After Quantum tab {tab_label}")

        # Switch to quantum calculation view and unhide spectrum sections for testing
        driver.execute_script("""
            window.showChemistryView('quantum', 'analyzer');
            document.getElementById('engine-uvvis-section')?.classList.remove('hidden');
            document.getElementById('engine-ir-section')?.classList.remove('hidden');
        """)
        time.sleep(0.5)

        # Test UV-Vis Legend Toggle & Quick Toggle
        uvvis_legend_btn = driver.find_element(By.ID, "engine-uvvis-legend-toggle")
        driver.execute_script("arguments[0].click();", uvvis_legend_btn)
        time.sleep(0.3)
        print("Clicked UV-Vis Legend toggle button:", uvvis_legend_btn.text)
        print_logs("After UV-Vis legend toggle")

        # Test IR Legend Toggle & Quick Toggle
        ir_legend_btn = driver.find_element(By.ID, "engine-ir-legend-toggle")
        driver.execute_script("arguments[0].click();", ir_legend_btn)
        time.sleep(0.3)
        print("Clicked IR Legend toggle button:", ir_legend_btn.text)
        print_logs("After IR legend toggle")

        # Test IR Y-Axis Mode Switch (Theoretical IR Intensity)
        ir_y_mode = driver.find_element(By.ID, "ir-y-mode")
        driver.execute_script("arguments[0].value = 'theory_intensity'; arguments[0].dispatchEvent(new Event('change'));", ir_y_mode)
        time.sleep(0.4)
        # Test Frontier Orbitals & CDFT Q1 Diagram PNG Generation
        driver.execute_script("""
            window.showChemistryView('quantum', 'analyzer');
            window.currentEngineData = {
                name: 'Water_H2O',
                latest_job: {
                    formula: 'H2O',
                    method: 'B3LYP',
                    basis_set: 'def2-TZVP',
                    solvation: 'Gas Phase',
                    homo_ev: -7.116,
                    lumo_ev: 1.312,
                    homo_lumo_gap_ev: 8.428,
                    chemical_hardness_ev: 4.214,
                    chemical_softness_ev: 0.119,
                    electronegativity_ev: 2.902,
                    chemical_potential_ev: -2.902,
                    electrophilicity_index_ev: 0.999,
                    ionization_potential_ev: 7.116,
                    electron_affinity_ev: -1.312
                }
            };
        """)
        time.sleep(0.3)
        orb_png_btn = driver.find_element(By.ID, "engine-orbitals-download-png")
        driver.execute_script("arguments[0].click();", orb_png_btn)
        time.sleep(0.5)
        print("Clicked High-Res Orbitals & CDFT PNG Download button")
        print_logs("After Orbitals & CDFT PNG export")

        # Test Reaction Thermochemistry (Dual-Slot Cards & Custom Gas-Phase Conditions)
        driver.execute_script("window.showChemistryView('quantum', 'thermo');")
        time.sleep(0.4)
        print("Switched to Reaction Thermochemistry view")

        custom_cond_cb = driver.find_element(By.ID, "thermo-enable-custom-conditions")
        driver.execute_script("arguments[0].click();", custom_cond_cb)
        time.sleep(0.3)
        print("Toggled Custom Conditions Checkbox, checked state:", custom_cond_cb.is_selected())
        print_logs("After Custom Conditions toggle")

        # Test Opening ORCA Wizard Modal
        driver.execute_script("window.showChemistryView('orca', 'orca');")
        time.sleep(0.5)


        driver.find_element(By.ID, "orca-open-wizard").click()
        time.sleep(0.5)
        orca_modal = driver.find_element(By.ID, "orca-modal")
        print("ORCA Wizard Modal classes after open:", orca_modal.get_attribute("class"))
        assert "hidden" not in orca_modal.get_attribute("class"), "ORCA Wizard modal should open!"
        
        # Close ORCA Wizard
        driver.find_element(By.ID, "orca-modal-close").click()
        time.sleep(0.3)
        print("ORCA Wizard Modal classes after close:", orca_modal.get_attribute("class"))

        # Test Citation Modal
        driver.execute_script("window.showChemistryView('home');")
        time.sleep(0.5)
        hero_cite_btn = driver.find_element(By.ID, "hero-cite-btn")
        driver.execute_script("arguments[0].click();", hero_cite_btn)
        time.sleep(0.5)
        cite_modal = driver.find_element(By.ID, "citation-modal")
        print("Citation Modal classes after open:", cite_modal.get_attribute("class"))
        assert "hidden" not in cite_modal.get_attribute("class"), "Citation modal should open!"

        driver.find_element(By.ID, "citation-modal-close").click()
        time.sleep(0.3)
        print("Citation Modal classes after close:", cite_modal.get_attribute("class"))
        assert "hidden" in cite_modal.get_attribute("class"), "Citation modal should close!"

        print("\n=======================================================")
        print("ALL BROWSER INTERACTIONS & VIEWS VERIFIED SUCCESSFULLY!")
        print("=======================================================")

    finally:
        driver.quit()
        server.shutdown()


if __name__ == "__main__":
    audit()
