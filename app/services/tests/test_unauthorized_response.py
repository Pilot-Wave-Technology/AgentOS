"""A refused /internal/* call must say 401, not 405.

Every gateway->control-panel call in the `next` environment reported
"HTTP 405 Method Not Allowed". Nothing was wrong with the method: the
bearer was refused, the 401 handler answered with a 307 to /login, and
`fetch` re-sent the POST to a GET-only login route. The status the
caller finally saw named a fault that did not exist.
"""
from types import SimpleNamespace

from app.error_responses import unauthorized_response, wants_html


def _request(path: str, headers: dict | None = None):
    h = {k.lower(): v for k, v in (headers or {}).items()}
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers=h,
        method="POST",
        cookies={},
    )


def test_internal_caller_gets_a_401_not_a_redirect():
    r = unauthorized_response(
        _request("/internal/goals/ncal1/stories/create",
                 {"Authorization": "Bearer wrong"}))
    assert r.status_code == 401


def test_browser_navigation_still_gets_the_login_page():
    r = unauthorized_response(
        _request("/goals", {"Accept": "text/html,application/xhtml+xml"}))
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/login"


def test_a_bearer_caller_is_never_redirected_whatever_the_path():
    # The gateway's tools send Accept: */* and a bearer. A redirect is
    # unreadable to them wherever it is issued from.
    assert not wants_html(
        _request("/goals/x", {"Authorization": "Bearer t", "Accept": "*/*"}))


def test_api_surface_is_machine_too():
    assert not wants_html(_request("/api/memory/usage", {"Accept": "text/html"}))


def test_the_405_symptom_is_gone_for_the_endpoint_that_showed_it():
    # create_story POSTs here; a 307 sends the POST to GET-only /login.
    r = unauthorized_response(_request("/internal/agents/lifecycle"))
    assert r.status_code == 401
    assert "location" not in r.headers
