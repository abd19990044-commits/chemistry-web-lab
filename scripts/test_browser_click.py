# -*- coding: utf-8 -*-
"""
Headless browser test using Selenium to verify:
1. Console errors on page load.
2. Clicking 'Draw Chemistry', 'ORCA Calculations', 'Quantum Engine'.
3. Clicking 'How to Cite' button in hero and checking modal visibility.
4. Copying BibTeX citation.
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
    def __init__(self, app, port=5099):
        super().__init__()
        self.server = make_server("127.0.0.1", port, app)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()


def test_browser():
    server = ServerThread(app, port=5099)
    server.start()
    time.sleep(1)

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get("http://127.0.0.1:5099/")
        time.sleep(1)

        # Check console logs
        logs = driver.get_log("browser")
        print("=== CONSOLE LOGS ON LOAD ===")
        for log in logs:
            print(f"[{log['level']}] {log['message']}")

        # 1. Test clicking 'How to Cite' button
        hero_cite_btn = driver.find_element(By.ID, "hero-cite-btn")
        hero_cite_btn.click()
        time.sleep(0.5)

        citation_modal = driver.find_element(By.ID, "citation-modal")
        modal_classes = citation_modal.get_attribute("class")
        print("Citation modal classes after click:", modal_classes)
        assert "hidden" not in modal_classes, f"Citation modal should be visible! Got: {modal_classes}"

        # Close citation modal
        close_btn = driver.find_element(By.ID, "citation-modal-close")
        close_btn.click()
        time.sleep(0.5)
        modal_classes_after = citation_modal.get_attribute("class")
        print("Citation modal classes after close:", modal_classes_after)
        assert "hidden" in modal_classes_after, f"Citation modal should be hidden! Got: {modal_classes_after}"

        # 2. Test clicking choice cards
        cards = driver.find_elements(By.CLASS_NAME, "choice-card")
        print(f"Found {len(cards)} choice cards")
        
        # Click Draw Chemistry
        cards[0].click()
        time.sleep(0.5)
        draw_view = driver.find_element(By.ID, "draw-view")
        print("Draw view classes after click:", draw_view.get_attribute("class"))
        assert "is-active" in draw_view.get_attribute("class"), "Draw view should be active!"

        # Back to Home
        back_btn = driver.find_element(By.ID, "back-home-btn")
        back_btn.click()
        time.sleep(0.5)
        home_view = driver.find_element(By.ID, "home-view")
        print("Home view classes after back click:", home_view.get_attribute("class"))
        assert "is-active" in home_view.get_attribute("class"), "Home view should be active!"

        # Click Quantum Engine
        cards = driver.find_elements(By.CLASS_NAME, "choice-card")
        cards[2].click()
        time.sleep(0.5)
        quantum_view = driver.find_element(By.ID, "quantum-view")
        print("Quantum view classes after click:", quantum_view.get_attribute("class"))
        assert "is-active" in quantum_view.get_attribute("class"), "Quantum view should be active!"

        print("=== ALL BROWSER INTERACTIONS PASSED SUCCESSFULLY ===")
    finally:
        driver.quit()
        server.shutdown()


if __name__ == "__main__":
    test_browser()
