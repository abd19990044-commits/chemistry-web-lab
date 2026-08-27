# -*- coding: utf-8 -*-
"""Route tests for the three primary sections (/lab, /calculations, /analysis).

Each section is a REAL Flask route that renders the full app with the
requested studio pre-activated, so deep links, refreshes and back/forward
navigation work without a client-side router.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import app as webapp
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


ROUTE_VIEW = {
    "/lab": "draw",
    "/calculations": "orca",
    "/analysis": "quantum",
}


@pytest.mark.parametrize("route,expected_view", sorted(ROUTE_VIEW.items()))
def test_primary_section_route_activates_its_studio(client, route, expected_view):
    resp = client.get(route)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-initial-view="%s"' % expected_view in body, (
        "%s must pre-activate the %s studio" % (route, expected_view))


def test_primary_sections_carry_the_primary_navigation(client):
    for route in ROUTE_VIEW:
        body = client.get(route).get_data(as_text=True)
        assert 'class="topnav-link' in body
        for link in ("/lab", "/calculations", "/analysis"):
            assert 'href="%s"' % link in body, "missing nav link %s on %s" % (link, route)
        assert 'aria-current="page"' in body, "active section must be announced to AT"


def test_active_navigation_state_follows_the_route(client):
    import re
    body = client.get("/calculations").get_data(as_text=True)
    calc_anchor = re.search(r'<a class="topnav-link[^\"]*" href="/calculations"[^>]*>', body)
    lab_anchor = re.search(r'<a class="topnav-link[^\"]*" href="/lab"[^>]*>', body)
    assert calc_anchor and lab_anchor, "primary navigation links must be rendered"
    assert "is-active" in calc_anchor.group(0) and "aria-current" in calc_anchor.group(0)
    assert "is-active" not in lab_anchor.group(0) and "aria-current" not in lab_anchor.group(0)


def test_home_still_serves_the_landing_view(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-initial-view="home"' in body
    assert 'home-view' in body


def test_theme_layer_is_served_after_the_main_stylesheet(client):
    body = client.get("/lab").get_data(as_text=True)
    assert "css/theme.css" in body
    assert body.index("css/style.css") < body.index("css/theme.css"), \
        "the theme layer must override the base tokens"
    assert "js/app.js?v=20260827a" in body, "app.js cache-bust must follow behaviour changes"


def test_unknown_view_is_never_activated(client):
    body = client.get("/lab").get_data(as_text=True)
    assert 'data-initial-view="unknown"' not in body
