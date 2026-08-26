# -*- coding: utf-8 -*-
"""Verification tests for Copy for Word functionality in 2D molecule explorer and reaction scheme."""

import os
import re
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestWordCopyRestoration:
    """Parts 26, 27, 28, 29: Copy for Word for 2D molecular drawings and reactions."""

    def test_explorer_and_reaction_copy_word_buttons_in_html(self):
        html_path = os.path.join(REPO_ROOT, "templates", "index.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Check explorer copy for word button
        assert 'id="explorer-copy-word"' in html, "Missing #explorer-copy-word button in templates/index.html"
        assert 'Copy for Word' in html, "Missing 'Copy for Word' label in templates/index.html"

        # Check reaction copy for word button
        assert 'id="reaction-copy-word"' in html, "Missing #reaction-copy-word button in templates/index.html"

    def test_app_js_implements_copy_element_for_word(self):
        js_path = os.path.join(REPO_ROOT, "static", "js", "app.js")
        with open(js_path, "r", encoding="utf-8") as f:
            js = f.read()

        assert "copyElementForWord" in js, "app.js must implement copyElementForWord function"
        assert "explorer-copy-word" in js, "app.js must attach click listener to #explorer-copy-word"
        assert "reaction-copy-word" in js, "app.js must attach click listener to #reaction-copy-word"
        assert "ClipboardItem" in js, "app.js must construct ClipboardItem for rich Word copy"
        assert "text/html" in js, "app.js must format HTML clipboard payload with <img> for Word"

    def test_zero_em_dashes_in_templates_and_js(self):
        for path in [
            os.path.join(REPO_ROOT, "templates", "index.html"),
            os.path.join(REPO_ROOT, "static", "js", "app.js"),
        ]:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            assert "\u2014" not in content, f"Em-dash found in {path}"
            assert "\u2013" not in content, f"En-dash found in {path}"
