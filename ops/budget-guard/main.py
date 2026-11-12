"""Budget guard: takes the site offline when the monthly budget is spent.

Cloud Billing publishes the budget's status to a Pub/Sub topic several times a day. When the cost of
the current period reaches the budget, this function removes public access (allUsers) from the Cloud
Run service. Requests are then refused by Google's front end before they reach the container, so the
service stops costing money. Nothing is deleted; access is restored with one command (see README).
"""
from __future__ import annotations

import base64
import json
import os

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT = os.environ.get("PROJECT_ID", "")
REGION = os.environ.get("REGION", "europe-west1")
SERVICE = os.environ.get("SERVICE", "land-change")
THRESHOLD = float(os.environ.get("THRESHOLD", "1.0"))   # share of the budget that triggers the stop
PUBLIC = "allUsers"
INVOKER = "roles/run.invoker"


def service_url() -> str:
    return f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/services/{SERVICE}"


def over_budget(notification: dict) -> bool:
    cost = float(notification.get("costAmount", 0))
    budget = float(notification.get("budgetAmount", 0))
    return budget > 0 and cost >= budget * THRESHOLD


def without_public_access(policy: dict) -> tuple[dict, bool]:
    """The policy with allUsers removed from the invoker role, and whether anything changed."""
    changed = False
    bindings = []
    for binding in policy.get("bindings", []):
        members = binding.get("members", [])
        if binding.get("role") == INVOKER and PUBLIC in members:
            members = [m for m in members if m != PUBLIC]
            changed = True
        if members:
            bindings.append({**binding, "members": members})
    return {**policy, "bindings": bindings}, changed


def close_service(session) -> bool:
    """Remove public access. Returns True when access was removed, False when it was already closed."""
    url = service_url()
    response = session.get(f"{url}:getIamPolicy")
    response.raise_for_status()
    policy, changed = without_public_access(response.json())
    if not changed:
        return False
    response = session.post(f"{url}:setIamPolicy", json={"policy": policy})
    response.raise_for_status()
    return True


@functions_framework.cloud_event
def on_budget(event):
    message = event.data.get("message", {})
    notification = json.loads(base64.b64decode(message.get("data", "")) or b"{}")
    cost, budget = notification.get("costAmount"), notification.get("budgetAmount")
    currency = notification.get("currencyCode", "")
    if not over_budget(notification):
        print(f"Within budget: {cost} of {budget} {currency}. Nothing to do.")
        return
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if close_service(AuthorizedSession(credentials)):
        print(f"Budget reached ({cost} of {budget} {currency}): public access to {SERVICE} removed.")
    else:
        print(f"Budget reached ({cost} of {budget} {currency}): {SERVICE} was already closed.")
