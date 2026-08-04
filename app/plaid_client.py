"""Thin wrapper around plaid-python for the three calls this app needs:
create a Link token, exchange a public token for an access token, and pull
transactions via the cursor-based sync endpoint.

Nothing here touches SQLite or encryption — main.py stores the access token
(via app/crypto.py) and the returned transactions; this module only talks
to Plaid's API.
"""

from __future__ import annotations

import os

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

_ENV_HOSTS = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


class PlaidNotConfigured(RuntimeError):
    """PLAID_CLIENT_ID / PLAID_SECRET aren't set yet."""


_client: plaid_api.PlaidApi | None = None


def _get_client() -> plaid_api.PlaidApi:
    global _client
    if _client is not None:
        return _client

    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")
    if not client_id or not secret:
        raise PlaidNotConfigured(
            "PLAID_CLIENT_ID and PLAID_SECRET must be set in .env before Plaid can be used. "
            "Get free sandbox keys from https://dashboard.plaid.com."
        )
    env_name = os.getenv("PLAID_ENV", "sandbox")
    host = _ENV_HOSTS.get(env_name)
    if host is None:
        raise RuntimeError(f"Unknown PLAID_ENV: {env_name!r} (expected sandbox|production)")

    configuration = plaid.Configuration(
        host=host,
        api_key={"clientId": client_id, "secret": secret},
    )
    api_client = plaid.ApiClient(configuration)
    _client = plaid_api.PlaidApi(api_client)
    return _client


def reset_client() -> None:
    """Test-only: force re-init (e.g. after changing env vars mid-process)."""
    global _client
    _client = None


def create_link_token(card_id: str, redirect_uri: str | None = None) -> str:
    """A Link token is single-use and tied to one card_id (Plaid's `client_user_id`
    is repurposed here as the card, not a person — this app has one user).

    redirect_uri is required for OAuth institutions (Amex, Chase, and most
    major banks in Production — Sandbox never needs it). It must exactly
    match a URI registered in the Plaid Dashboard's allowed redirect URIs;
    the bank's OAuth login redirects the browser there, and the frontend
    resumes the same Link session on load (see resumePlaidOAuthIfNeeded in
    app.js) rather than starting over.
    """
    client = _get_client()
    kwargs = dict(
        client_name="CC Benefits Tracker",
        language="en",
        country_codes=[CountryCode("US")],
        user=LinkTokenCreateRequestUser(client_user_id=card_id),
        products=[Products("transactions")],
    )
    if redirect_uri:
        kwargs["redirect_uri"] = redirect_uri
    req = LinkTokenCreateRequest(**kwargs)
    resp = client.link_token_create(req)
    return resp["link_token"]


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Returns (access_token, item_id)."""
    client = _get_client()
    resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    return resp["access_token"], resp["item_id"]


def sync_transactions(access_token: str, cursor: str | None) -> dict:
    """One full cursor-sync pass: loops internally until has_more is False,
    so callers get everything new in one call rather than re-implementing
    the pagination loop themselves.

    Returns {"added": [...], "modified": [...], "removed": [...], "next_cursor": str}
    — each transaction is Plaid's raw dict-like model, left un-normalized so
    the matching engine decides what fields matter.
    """
    client = _get_client()
    added, modified, removed = [], [], []
    next_cursor = cursor
    has_more = True
    while has_more:
        # The SDK's model validation rejects cursor=None outright (it wants a
        # str or nothing at all) — the first-ever sync for a newly linked Item
        # has no cursor yet, so the kwarg has to be omitted, not passed as None.
        kwargs = {"access_token": access_token}
        if next_cursor is not None:
            kwargs["cursor"] = next_cursor
        req = TransactionsSyncRequest(**kwargs)
        resp = client.transactions_sync(req)
        added.extend(resp["added"])
        modified.extend(resp["modified"])
        removed.extend(resp["removed"])
        next_cursor = resp["next_cursor"]
        has_more = resp["has_more"]
    return {"added": added, "modified": modified, "removed": removed, "next_cursor": next_cursor}
