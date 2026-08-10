"""Production routing invariants.

The historical /api/kaggle/* contract is compatibility-only. Every calculation
operation must execute OrchestratorService rather than the obsolete lifecycle.
"""
from app import app

LEGACY = {
    "/api/kaggle/login", "/api/kaggle/submit", "/api/kaggle/status",
    "/api/kaggle/download", "/api/kaggle/delete", "/api/kaggle/cancel",
    "/api/kaggle/resume",
}


def test_legacy_routes_are_bound_to_orchestrator_adapter():
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    assert LEGACY.issubset(rules)
    for path in LEGACY:
        view = app.view_functions[rules[path].endpoint]
        assert view.__module__ == "orca_orchestrator.legacy_compat"


def test_legacy_routes_do_not_use_legacy_runner():
    rules = {rule.rule: rule for rule in app.url_map.iter_rules()}
    for path in LEGACY:
        assert app.view_functions[rules[path].endpoint].__module__ != "kaggle_runner"


def test_canonical_orca_routes_are_registered():
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert {
        "/api/orca/login", "/api/orca/jobs", "/api/orca/submit",
        "/api/orca/status", "/api/orca/cancel", "/api/orca/resume",
        "/api/orca/delete", "/api/orca/results", "/api/orca/health",
        "/api/orca/sweep", "/api/orca/state-machine",
    }.issubset(rules)


def test_application_has_a_health_endpoint():
    assert "/health" in {rule.rule for rule in app.url_map.iter_rules()}
