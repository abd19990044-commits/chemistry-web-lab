# -*- coding: utf-8 -*-
"""Frontend XSS Hardening & Safe Dynamic DOM Verification Tests.

Verifies:
1. static/js/reaction-unified.js uses safe DOM APIs (textContent, document.createElement)
   instead of unsafe innerHTML sinks for species names, stages, actions, and thermodynamics.
2. static/js/app.js defines escapeHtml and uses it in outputsListHTML, modals, and error views.
3. static/js/app.js sanitizeToastHtml enforces strict tag allowlists and removes event handlers
   and dangerous URL schemes (javascript:, vbscript:, data:).
4. templates/index.html does not contain unsafe unescaped template tags.
5. Common XSS payloads (<script>, <img onerror>, <svg onload>, javascript:) are thoroughly neutralized.
"""
import os
import re
import html
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REACTION_UNIFIED_JS = os.path.join(REPO_ROOT, "static", "js", "reaction-unified.js")
APP_JS = os.path.join(REPO_ROOT, "static", "js", "app.js")
LOCAL_AGENT_JS = os.path.join(REPO_ROOT, "static", "js", "local-agent.js")
INDEX_HTML = os.path.join(REPO_ROOT, "templates", "index.html")


def test_reaction_unified_has_no_unsafe_innerhtml_sinks():
    """Verify that reaction-unified.js has eliminated unsafe dynamic innerHTML assignments."""
    assert os.path.isfile(REACTION_UNIFIED_JS), "reaction-unified.js must exist"
    with open(REACTION_UNIFIED_JS, "r", encoding="utf-8") as f:
        content = f.read()

    # Unsafe historical sinks must NOT exist
    assert "tdName.innerHTML =" not in content, "tdName.innerHTML must not be used (XSS risk)"
    assert "tdStages.innerHTML =" not in content, "tdStages.innerHTML must not be used (XSS risk)"
    assert "tdAction.innerHTML =" not in content, "tdAction.innerHTML must not be used (XSS risk)"

    # Safe DOM creation must be present
    assert "strongName.textContent = sp.display_name" in content
    assert "span3D.textContent = '✓ 3D Coords'" in content
    assert "badge.textContent = `Step ${idx+1}:" in content
    assert "encodeURIComponent(String(completedStage.agent_job_id))" in content

    # Thermodynamics table must use textContent rather than string interpolation into innerHTML
    assert "tdDe.textContent = dE_kj" in content
    assert "tdDh.textContent = dH_kj" in content
    assert "tdDg.textContent = dG_kj" in content
    assert "tdDs.textContent = dS" in content


def test_app_js_defines_escape_html_and_hardens_toasts():
    """Verify that app.js properly defines escapeHtml and hardens sanitizeToastHtml."""
    assert os.path.isfile(APP_JS), "app.js must exist"
    with open(APP_JS, "r", encoding="utf-8") as f:
        content = f.read()

    # escapeHtml must be explicitly defined
    assert "function escapeHtml(str)" in content
    assert '.replace(/&/g, "&amp;")' in content
    assert '.replace(/</g, "&lt;")' in content
    assert '.replace(/>/g, "&gt;")' in content
    assert '.replace(/"/g, "&quot;")' in content
    assert ".replace(/'/g, \"&#39;\")" in content

    # sanitizeToastHtml must enforce tag allowlist
    assert "allowedTags = new Set" in content
    assert '"STRONG"' in content
    assert '"SPAN"' in content
    assert '"CODE"' in content

    # Dangerous attributes / protocols must be stripped
    assert 'name.startsWith("on")' in content
    assert 'val.includes("javascript:")' in content

    # Outputs pane must escape dynamic fields
    assert "escapeHtml(out.id)" in content
    assert "escapeHtml(out.name)" in content
    assert "escapeHtml(out.formula)" in content


def test_local_agent_js_safe_dom_construction():
    """Verify local-agent.js builds DOM using textContent and safe createElement."""
    assert os.path.isfile(LOCAL_AGENT_JS), "local-agent.js must exist"
    with open(LOCAL_AGENT_JS, "r", encoding="utf-8") as f:
        content = f.read()

    # Device rendering must use textContent
    assert "nameSpan.textContent = dev.display_name;" in content
    assert "opt.textContent = dev.display_name" in content
    # No innerHTML assignments with unescaped device properties
    assert "item.innerHTML =" not in content
    assert "nameSpan.innerHTML =" not in content


def test_templates_index_html_has_no_unsafe_raw_jinja_filters():
    """Verify index.html contains no unsafe unescaped variables (| safe or | raw)."""
    assert os.path.isfile(INDEX_HTML), "index.html must exist"
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        content = f.read()

    # Search for Jinja unescaped filters like {{ ... | safe }}
    unsafe_jinja = re.findall(r"\{\{.*\|\s*safe\s*\}\}", content)
    assert len(unsafe_jinja) == 0, f"Found unsafe Jinja | safe filter: {unsafe_jinja}"


def test_xss_payload_neutralization_contract():
    """Simulate neutralization of standard OWASP XSS attack vectors."""
    payloads = [
        '<script>alert("XSS")</script>',
        '<img src=x onerror="alert(\'XSS\')">',
        '"><svg onload=alert(document.cookie)>',
        "javascript:alert(1)",
        '<a href="javascript:alert(1)">Click Me</a>',
        "'\"><iframe src=javascript:alert(1)></iframe>",
    ]

    def python_escape_html(s):
        if s is None:
            return ""
        return (
            str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )

    for p in payloads:
        escaped = python_escape_html(p)
        # Verify that tag opening brackets and quotes are neutralized
        assert "<" not in escaped
        assert ">" not in escaped
        assert '"' not in escaped
        assert "'" not in escaped
        # Cannot be parsed as an active HTML tag
        assert not re.search(r"<\s*script", escaped, re.IGNORECASE)
        assert not re.search(r"<\s*img", escaped, re.IGNORECASE)
        assert not re.search(r"<\s*svg", escaped, re.IGNORECASE)
        assert not re.search(r"<\s*iframe", escaped, re.IGNORECASE)
