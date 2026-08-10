from app import app


def test_legacy_kaggle_routes_use_orchestrator_adapter():
    expected = {
        "/api/kaggle/login",
        "/api/kaggle/submit",
        "/api/kaggle/status",
        "/api/kaggle/download",
        "/api/kaggle/delete",
    }
    routes = {rule.rule for rule in app.url_map.iter_rules() if rule.rule in expected}
    assert routes == expected
    for endpoint, view in app.view_functions.items():
        if endpoint in {
            "api_kaggle_login",
            "api_kaggle_submit",
            "api_kaggle_status",
            "api_kaggle_download",
            "api_kaggle_delete",
        }:
            assert view.__module__ == "orca_orchestrator.legacy_compat"


def test_orca_routes_are_registered():
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/orca/submit" in rules
    assert "/api/orca/status" in rules
    assert "/api/orca/health" in rules
