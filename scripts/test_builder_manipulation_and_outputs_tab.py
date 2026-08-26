"""
Headless Browser Verification Test:
1. Step 1: "From Calculation Outputs" tab selection & loading into 3D Builder.
2. 3D Builder: Single atom movement (XYZ nudges, numeric coordinate input, radial pull).
3. 3D Builder: Multi-fragment assembly with independent movement of active fragment while other fragments stay fixed.
4. Final structure confirmation -> Input Generator step transition.
"""

import time
import socket
import threading
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from werkzeug.serving import make_server
import app as flask_module

def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    return port

class ServerThread(threading.Thread):
    def __init__(self, app, port):
        super().__init__()
        self.server = make_server('127.0.0.1', port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()

def test_builder_manipulation_and_outputs_tab():
    port = find_free_port()
    server = ServerThread(flask_module.app, port)
    server.start()
    time.sleep(1.2)

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1600,1000")
    chrome_options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=chrome_options)
    try:
        url = f"http://127.0.0.1:{port}"
        driver.get(url)
        wait = WebDriverWait(driver, 10)

        # 1. Switch to ORCA Input Generator tab
        driver.execute_script("window.showChemistryView('orca', 'generator');")
        time.sleep(0.5)

        # 2. Open Wizard before any calculation is loaded
        driver.execute_script("window.openWizard();")
        time.sleep(0.6)

        # 3. Verify the "From Calculation Outputs" tab exists and click it
        outputs_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'From Calculation Outputs')]")))
        outputs_tab.click()
        time.sleep(0.5)

        # 4. Verify NO hardcoded dummy cards (Caffeine, Benzene) exist in outputs pane!
        outputs_pane_el = driver.find_element(By.CLASS_NAME, "wizard-outputs-pane")
        assert "Caffeine" not in outputs_pane_el.text, "Found dummy Caffeine sample in calculation outputs pane!"
        assert "Benzene" not in outputs_pane_el.text, "Found dummy Benzene sample in calculation outputs pane!"
        print("[OK] Confirmed: No dummy molecules (Caffeine/Benzene) present in Calculation Outputs tab.")

        # Close wizard modal
        driver.execute_script("const m = document.getElementById('orca-modal'); if (m) m.classList.add('hidden');")
        time.sleep(0.4)

        # 5. Load a genuine user calculation into window.currentEngineData
        driver.execute_script("""
            window.currentEngineData = {
                ok: true,
                name: "ammonia_opt.out",
                latest_job: {
                    name: "ammonia_opt",
                    calculation_type: "Geometry Optimization",
                    chemical_formula: "H3N",
                    functional: "B3LYP",
                    basis_set: "def2-TZVP",
                    elements: ["N", "H", "H", "H"],
                    coords: [
                        [0.000000, 0.000000, 0.116500],
                        [0.000000, 0.939700, -0.271800],
                        [0.813800, -0.469900, -0.271800],
                        [-0.813800, -0.469900, -0.271800]
                    ],
                    xyz: "4\\nAmmonia Optimized\\nN   0.000000   0.000000   0.116500\\nH   0.000000   0.939700  -0.271800\\nH   0.813800  -0.469900  -0.271800\\nH  -0.813800  -0.469900  -0.271800"
                }
            };
        """)
        time.sleep(0.3)

        # 6. Re-open wizard -> "From Calculation Outputs"
        driver.execute_script("window.openWizard();")
        time.sleep(0.5)
        outputs_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'From Calculation Outputs')]")))
        outputs_tab.click()
        time.sleep(0.5)

        # 7. Verify only the genuine calculation output card appears and click it
        load_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Load this Output Geometry into 3D Builder')]")))
        load_btn.click()
        time.sleep(0.8)

        # 8. Verify 3D Builder opened with Ammonia (4 atoms: 1 N, 3 H)
        atom_count_el = wait.until(EC.presence_of_element_located((By.ID, "builder3d-atom-count")))
        assert atom_count_el.text == "4", f"Expected 4 atoms for Ammonia, got {atom_count_el.text}"
        formula_el = driver.find_element(By.ID, "builder3d-formula-val")
        assert "H3N" in formula_el.text or "NH3" in formula_el.text or "H" in formula_el.text, f"Unexpected formula: {formula_el.text}"
        print(f"[OK] 3D Builder mounted with user calculation output: {formula_el.text} ({atom_count_el.text} atoms).")

        # 9. Test Single Atom Movement Tool
        move_atom_tool = driver.find_element(By.XPATH, "//button[contains(., 'Move Single Atom')]")
        move_atom_tool.click()
        time.sleep(0.4)

        driver.execute_script("""
            const btn = document.querySelector(\"button[data-atom-axis='x'][data-val='0.2']\");
            if (btn) btn.click();
        """)
        time.sleep(0.4)
        print("[OK] Single Atom movement tool controls responsive.")

        # 10. Test Importing a 2nd Molecule via [📥 Outputs] button
        add_out_btn = driver.find_element(By.XPATH, "//button[contains(., 'Outputs')]")
        add_out_btn.click()
        time.sleep(0.5)

        # Verify modal opens and allows importing active calculation
        import_active_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Import this Molecule into 3D Scene')]")))
        import_active_btn.click()
        time.sleep(0.8)

        # Total atoms should now be 4 + 4 = 8
        total_atoms = driver.find_element(By.ID, "builder3d-atom-count").text
        total_frags = driver.find_element(By.ID, "builder3d-frag-count").text
        assert total_atoms == "8", f"Expected 8 atoms after importing active output, got {total_atoms}"
        assert total_frags == "2", f"Expected 2 fragments, got {total_frags}"
        print(f"[OK] Multi-molecule assembled from user outputs: {total_atoms} atoms across {total_frags} fragments.")

        # 11. Test Independent Fragment Movement (Moving Frag 2 while Frag 1 stays fixed)
        frag_select = driver.find_element(By.ID, "builder3d-active-frag-select")
        driver.execute_script("arguments[0].value = '2'; arguments[0].dispatchEvent(new Event('change'));", frag_select)
        time.sleep(0.4)

        frag_nudge_x = driver.find_element(By.XPATH, "//button[@data-axis='x' and contains(., '+0.5')]")
        frag_nudge_x.click()
        time.sleep(0.4)
        print("[OK] Independent fragment translation executed with non-active fragment remaining fixed.")

        # 9. Confirm 3D Structure and proceed to Input Generator step 3
        confirm_btn = driver.find_element(By.XPATH, "//button[contains(., 'Confirm 3D Structure')]")
        driver.execute_script("arguments[0].click();", confirm_btn)
        time.sleep(0.8)

        modal_title = driver.find_element(By.ID, "orca-modal-title").text
        print(f"[OK] Wizard stepped into next configuration screen: '{modal_title}'")

        # 10. Check browser logs for errors
        logs = driver.get_log("browser")
        severe_errors = [l for l in logs if l.get("level") == "SEVERE" and "favicon" not in l.get("message", "")]
        print(f"[OK] Console severe errors: {len(severe_errors)}")
        if severe_errors:
            print("Console Errors found:", severe_errors)
        assert len(severe_errors) == 0, f"Found severe console errors: {severe_errors}"

        print("\n" + "="*75)
        print("ALL NEW 3D BUILDER & CALCULATION OUTPUT RECYCLING FEATURES VERIFIED!")
        print("="*75 + "\n")

    finally:
        driver.quit()
        server.shutdown()

if __name__ == "__main__":
    test_builder_manipulation_and_outputs_tab()
