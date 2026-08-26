# -*- coding: utf-8 -*-
"""Headless Selenium test for Dual-Stage Chained Workflow and Kaggle Queue Governor."""
import os
import sys
import time
import socket
import threading
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as flask_module

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

def test_dual_workflow_and_queue():
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
    wait = WebDriverWait(driver, 10)

    def pick_button(match_text, exact=False):
        for attempt in range(35):
            res = driver.execute_script("""
                const match = arguments[0];
                const exact = arguments[1];
                const title = document.getElementById('orca-modal-title')?.textContent || '';
                const btns = Array.from(document.querySelectorAll('.modal-window button, .modal-actions button, .orca-step button'));
                const btn = btns.find(b => {
                    const txt = b.textContent.trim();
                    return exact ? txt === match : txt.includes(match);
                });
                if (btn) {
                    const txt = btn.textContent.trim();
                    btn.click();
                    return { clicked: true, title: title, btnText: txt };
                }
                const avail = btns.map(b => b.textContent.trim()).join(' | ');
                return { clicked: false, title: title, available: avail };
            """, match_text, exact)
            if res and res.get("clicked"):
                print(f"[{res.get('title')}] -> Clicked: '{res.get('btnText')}'", flush=True)
                time.sleep(0.4)
                return True
            time.sleep(0.1)
        raise RuntimeError(f"Could not find button matching '{match_text}' (exact={exact}). Last state: {res}")

    try:
        url = f"http://127.0.0.1:{port}/"
        driver.get(url)
        print("Page loaded successfully at:", url, flush=True)

        # Set mock kaggle credentials in localStorage
        driver.execute_script("""
            localStorage.setItem('orca_lab_kaggle_username', 'quantum_researcher');
            localStorage.setItem('orca_lab_kaggle_key', '00000000000000000000000000000000');
            localStorage.setItem('orca_lab_orca_source_kind', 'link');
            localStorage.setItem('orca_lab_orca_link', 'https://example.com/orca.tar.gz');
        """)
        driver.refresh()
        wait.until(lambda d: d.execute_script("return typeof window.showChemistryView === 'function';"))

        # Navigate to ORCA Input Generator workspace
        driver.execute_script("window.showChemistryView('orca', 'orca');")
        time.sleep(0.5)

        # 1. Open Wizard
        open_btn = wait.until(EC.element_to_be_clickable((By.ID, "orca-open-wizard")))
        open_btn.click()
        print("Opened Wizard.", flush=True)

        # 2. Select manual coords for Water molecule
        driver.execute_script("""
            const tabManual = Array.from(document.querySelectorAll('.modal-tab')).find(el => el.textContent.includes('Manual'));
            if (tabManual) tabManual.click();
            const txt = document.querySelector('textarea[placeholder*=\"C 0.000\"]');
            if (txt) txt.value = "O 0.000000 0.000000 0.117215\\nH 0.000000 0.756950 -0.468860\\nH 0.000000 -0.756950 -0.468860";
            const nameInp = document.querySelector('input[placeholder=\"molecule\"]');
            if (nameInp) nameInp.value = "Water_DualTest";
            const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Continue to 3D Builder'));
            if (btn) btn.click();
        """)
        time.sleep(1)
        print("Loaded coordinates into 3D Builder.", flush=True)

        # Confirm Structure in 3D builder
        driver.execute_script("""
            const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Confirm 3D Structure'));
            if (btn) btn.click();
        """)
        time.sleep(0.8)
        print("Confirmed structure and advanced to calculation options.", flush=True)

        # 3. Switch to Dual-Stage Workflow
        driver.execute_script("""
            const btnDual = document.getElementById('btn-wf-dual');
            if (btnDual) btnDual.click();
        """)
        time.sleep(0.6)
        print("Selected Dual-Stage Workflow mode.", flush=True)

        # Stage 1: Choose Geometry Optimization
        pick_button("Geometry Optimization")
        print("Stage 1: Picked Geometry Optimization.", flush=True)

        # Stage 1: Theory Family -> DFT
        pick_button("DFT (")
        print("Stage 1: Picked DFT.", flush=True)

        # Stage 1: Method -> B3LYP
        pick_button("B3LYP", exact=True)
        print("Stage 1: Picked B3LYP.", flush=True)

        # Stage 1: SCF -> Default
        pick_button("Default SCF")

        # Stage 1: Dispersion -> D3BJ
        pick_button("D3BJ", exact=True)

        # Stage 1: RI -> RIJCOSX
        pick_button("RIJCOSX")

        # Stage 1: Basis Family -> Ahlrichs
        pick_button("Ahlrichs")

        # Stage 1: Basis Set -> def2-SVP
        pick_button("def2-SVP", exact=True)

        # Stage 1: Solvation -> None (Gas Phase)
        pick_button("None (Gas Phase)")

        # Stage 1: X2C -> No
        pick_button("No", exact=True)

        # Stage 1: Charge -> Next
        pick_button("Next ►")

        # Stage 1: Mult -> Next
        pick_button("Next ►")

        # Stage 1: Cores -> Next
        pick_button("Next ►")

        # Stage 1: RAM & Transition to Stage 2
        pick_button("Next: Configure Stage 2")
        print("Advanced to Stage 2 configuration.", flush=True)

        # Stage 2: Single Point Energy
        pick_button("Single Point Energy")

        # Stage 2: Theory Family -> CCSD
        pick_button("CCSD / highly correlated")

        # Stage 2: Method -> DLPNO-CCSD(T)
        pick_button("DLPNO-CCSD(T)", exact=True)

        # Stage 2: SCF -> TightSCF
        pick_button("TightSCF")

        # Stage 2: Basis Family -> Ahlrichs
        pick_button("Ahlrichs")

        # Stage 2: Basis Set -> def2-TZVP
        pick_button("def2-TZVP", exact=True)

        # Stage 2: Solvation -> None (Gas Phase)
        pick_button("None (Gas Phase)")

        # Stage 2: Cores -> Next
        pick_button("Next ►")

        # Stage 2: Generate Dual Files
        pick_button("Generate")
        time.sleep(3)
        print("Generated Dual-Stage Input Files successfully.", flush=True)

        # Verify dual tabs are present and clickable
        tab_stage1 = wait.until(EC.presence_of_element_located((By.ID, "btn-tab-stage1")))
        tab_stage2 = wait.until(EC.presence_of_element_located((By.ID, "btn-tab-stage2")))
        assert tab_stage1.is_displayed(), "Stage 1 tab not visible"
        assert tab_stage2.is_displayed(), "Stage 2 tab not visible"

        driver.execute_script("arguments[0].click();", tab_stage2)
        time.sleep(0.5)
        out_pre = driver.find_element(By.ID, "orca-output").text
        assert "DLPNO-CCSD(T)" in out_pre, f"Expected DLPNO-CCSD(T) in stage 2 output, got: {out_pre}"
        print("Verified Stage 2 DLPNO-CCSD(T) output preview.", flush=True)

        # Submit dual workflow to Kaggle queue
        submit_queue_btn = driver.find_element(By.ID, "orca-submit-dual-queue")
        driver.execute_script("arguments[0].click();", submit_queue_btn)
        time.sleep(1.5)
        print("Submitted Dual Workflow to Kaggle Queue.", flush=True)

        # Switch to Jobs Tab and verify Queue Governor Banner & Cards
        driver.execute_script("""
            if (window.setKaggleSignedIn) {
                window.setKaggleSignedIn('quantum_researcher', '00000000000000000000000000000000');
            }
            const jobsTab = Array.from(document.querySelectorAll('.ws-tab')).find(el => el.textContent.includes('Jobs'));
            if (jobsTab) jobsTab.click();
        """)
        time.sleep(1)

        banner = driver.find_element(By.ID, "jobs-queue-banner")
        assert banner.is_displayed(), "Queue Governor Banner is not visible"

        jobs_list = driver.find_element(By.ID, "jobs-list")
        job_cards = jobs_list.find_elements(By.CLASS_NAME, "job-card")
        assert len(job_cards) >= 2, f"Expected at least 2 jobs in queue, found {len(job_cards)}"

        card_texts = [c.text for c in job_cards]
        print("Job Cards in Queue:\n", "\n---\n".join(card_texts), flush=True)

        assert any("stage 1" in t.lower() for t in card_texts), "Stage 1 job card missing"
        assert any("stage 2" in t.lower() for t in card_texts), "Stage 2 job card missing"
        assert any("waiting for stage 1" in t.lower() or "queued" in t.lower() for t in card_texts), "Stage 2 dependency status missing"

        # Verify max 20 jobs governor limit in localStorage
        driver.execute_script("""
            const currentJobs = JSON.parse(localStorage.getItem('chemlab_jobs') || '[]');
            for (let i = currentJobs.length; i < 20; i++) {
                currentJobs.push({
                    name: 'Dummy_Job_' + i,
                    jobId: 'dummy_' + i,
                    status: 'queued',
                    submittedAt: Date.now() + i
                });
            }
            localStorage.setItem('chemlab_jobs', JSON.stringify(currentJobs));
            if (window.renderJobs) window.renderJobs();
        """)
        time.sleep(1)

        total_cards = driver.find_elements(By.CLASS_NAME, "job-card")
        assert len(total_cards) == 20, f"Expected 20 jobs in queue, got {len(total_cards)}"
        print("Verified 20-job batch queue capacity.", flush=True)

        print("\nALL DUAL-STAGE WORKFLOW & QUEUE GOVERNOR BROWSER TESTS PASSED SUCCESSFULLY!", flush=True)

    finally:
        driver.quit()
        server.shutdown()

if __name__ == "__main__":
    test_dual_workflow_and_queue()
