"""Tests for the budget guard; they need neither Google Cloud nor network access."""
import base64
import json
import types

import main


class FakeSession:
    def __init__(self, policy):
        self.policy = policy
        self.calls = []

    def get(self, url):
        self.calls.append(("GET", url))
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.policy)

    def post(self, url, json):
        self.calls.append(("POST", url))
        self.policy = json["policy"]
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.policy)


OPEN = {"etag": "abc", "bindings": [
    {"role": "roles/run.invoker", "members": ["allUsers"]},
    {"role": "roles/run.admin", "members": ["serviceAccount:guard@p.iam.gserviceaccount.com"]},
]}


def test_threshold():
    assert main.over_budget({"costAmount": 50, "budgetAmount": 50})
    assert not main.over_budget({"costAmount": 49.99, "budgetAmount": 50})
    assert not main.over_budget({"costAmount": 10, "budgetAmount": 0})


def test_removes_only_public_access_and_keeps_etag():
    session = FakeSession(OPEN)
    assert main.close_service(session) is True
    assert session.policy["etag"] == "abc"
    roles = {b["role"]: b["members"] for b in session.policy["bindings"]}
    assert "roles/run.invoker" not in roles                      # empty binding dropped
    assert roles["roles/run.admin"] == ["serviceAccount:guard@p.iam.gserviceaccount.com"]
    assert [c[0] for c in session.calls] == ["GET", "POST"]
    assert session.calls[1][1].endswith("/services/land-change:setIamPolicy")


def test_already_closed_does_not_write():
    session = FakeSession({"etag": "x", "bindings": [{"role": "roles/run.invoker", "members": ["user:a@b.c"]}]})
    assert main.close_service(session) is False
    assert [c[0] for c in session.calls] == ["GET"]


def event(payload):
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return types.SimpleNamespace(data={"message": {"data": data}})


def handler():
    """The undecorated function, so the tests can pass a plain event object."""
    return getattr(main.on_budget, "__wrapped__", main.on_budget)


def test_handler_does_nothing_within_budget(monkeypatch):
    def no_credentials(**_):
        raise AssertionError("must not touch Google Cloud below the budget")
    monkeypatch.setattr(main.google.auth, "default", no_credentials)
    handler()(event({"costAmount": 1.2, "budgetAmount": 50, "currencyCode": "TRY"}))


def test_handler_closes_when_over_budget(monkeypatch):
    session = FakeSession(OPEN)
    monkeypatch.setattr(main.google.auth, "default", lambda **_: (object(), "p"))
    monkeypatch.setattr(main, "AuthorizedSession", lambda _: session)
    handler()(event({"costAmount": 51, "budgetAmount": 50, "currencyCode": "TRY"}))
    assert all("allUsers" not in b["members"] for b in session.policy["bindings"])
