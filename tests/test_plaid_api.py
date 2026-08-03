"""Plaid endpoints, end-to-end through the real FastAPI app but with the
actual Plaid network calls monkeypatched out — these tests exercise the
storage/matching/review-queue wiring, not Plaid's API itself (plaid_client.py
is a thin enough wrapper that mocking at its boundary is a faithful test)."""

import importlib
import os
import tempfile

import pytest


@pytest.fixture()
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    os.environ["TODAY_OVERRIDE"] = "2026-07-18"
    os.environ["ENCRYPTION_KEY_SOURCE"] = "passphrase"
    os.environ["ENCRYPTION_PASSPHRASE"] = "test-only-passphrase"
    try:
        from app import db as db_mod
        importlib.reload(db_mod)
        from app import crypto as crypto_mod
        importlib.reload(crypto_mod)
        from app import roi as roi_mod
        importlib.reload(roi_mod)
        from app import main as main_mod
        importlib.reload(main_mod)
        from fastapi.testclient import TestClient
        yield TestClient(main_mod.app), main_mod
    finally:
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("TODAY_OVERRIDE", None)
        os.environ.pop("ENCRYPTION_KEY_SOURCE", None)
        os.environ.pop("ENCRYPTION_PASSPHRASE", None)
        os.unlink(path)


def _sync_result(added=(), modified=(), removed=(), next_cursor="cursor-1"):
    return {"added": list(added), "modified": list(modified), "removed": list(removed),
            "next_cursor": next_cursor}


def cards(state):
    return {c["id"]: c for c in state["cards"]}


HULU_TXN = {
    "transaction_id": "txn_hulu_1", "name": "HULU", "merchant_name": "Hulu",
    "amount": -19.99, "date": "2026-07-15", "pending": False,  # Plaid: negative = credit
}


class TestLinkTokenAndExchange:
    def test_link_token_without_credentials_returns_400(self, client, monkeypatch):
        c, main_mod = client
        # Isolate from whatever's actually in the developer's real .env — this
        # test asserts behavior with NO credentials configured, regardless of
        # what's really sitting in the process environment.
        monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
        monkeypatch.delenv("PLAID_SECRET", raising=False)
        main_mod.plaid_client.reset_client()
        resp = c.post("/api/plaid/link_token", data={"card_id": "amex_platinum"})
        assert resp.status_code == 400

    def test_link_token_unknown_card_returns_404(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "create_link_token", lambda cid: "link-tok-abc")
        resp = c.post("/api/plaid/link_token", data={"card_id": "not_a_real_card"})
        assert resp.status_code == 404

    def test_exchange_stores_encrypted_token_and_runs_initial_sync(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-sandbox-real-secret", "item-abc"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(HULU_TXN)]))

        resp = c.post("/api/plaid/exchange", data={
            "card_id": "amex_platinum", "public_token": "public-sandbox-xyz",
            "institution_name": "Platypus Bank",
        })
        assert resp.status_code == 200
        state = resp.json()

        item = main_mod.db.plaid_item_for_card("amex_platinum")
        assert item is not None
        # The stored token must be ciphertext, never the plaintext secret.
        assert "real-secret" not in item["access_token_encrypted"]
        assert main_mod.crypto.decrypt(item["access_token_encrypted"]) == "access-sandbox-real-secret"

        assert any(p["card_id"] == "amex_platinum" for p in state["plaid_items"])


class TestHighConfidenceAutoMatch:
    def test_hulu_transaction_auto_creates_a_redemption(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(HULU_TXN)]))

        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})
        state = c.get("/api/state").json()

        digital_ent = next(
            e for e in cards(state)["amex_platinum"]["entries"]
            if e.get("id") == "amex_plat_digital_entertainment"
        )
        assert digital_ent["period_redeemed"] == pytest.approx(19.99)
        assert state["review_queue"] == []

    def test_syncing_twice_does_not_duplicate_the_redemption(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(
                                added=[dict(HULU_TXN)] if cursor is None else [],
                                modified=[dict(HULU_TXN)] if cursor is not None else [],
                            ))

        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})
        # Second sync: Plaid resends the same transaction as "modified" (e.g.
        # pending -> posted). Must not create a second redemption.
        c.post("/api/plaid/sync/amex_platinum")
        state = c.get("/api/state").json()

        digital_ent = next(
            e for e in cards(state)["amex_platinum"]["entries"]
            if e.get("id") == "amex_plat_digital_entertainment"
        )
        assert digital_ent["period_redeemed"] == pytest.approx(19.99)


class TestManualOverrideAlwaysWins:
    def test_auto_match_defers_to_review_if_window_already_manually_logged(self, client, monkeypatch):
        c, main_mod = client
        # User already logged July by hand before ever connecting Plaid.
        c.post("/api/period", data={"benefit_id": "amex_plat_digital_entertainment", "index": 6, "amount": 25})

        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(HULU_TXN)]))
        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})

        state = c.get("/api/state").json()
        digital_ent = next(
            e for e in cards(state)["amex_platinum"]["entries"]
            if e.get("id") == "amex_plat_digital_entertainment"
        )
        # Still just the manually-logged $25 — the Plaid transaction did NOT
        # get silently added on top of it.
        assert digital_ent["period_redeemed"] == pytest.approx(25)
        assert len(state["review_queue"]) == 1
        assert state["review_queue"][0]["name"] == "Hulu"


class TestReviewQueue:
    WEAK_TXN = {
        "transaction_id": "txn_resy_1", "name": "RESY INC", "merchant_name": None,
        "amount": -25.00, "date": "2026-08-01", "pending": False,  # Plaid: negative = credit
    }

    def test_ambiguous_transaction_lands_in_review_queue(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(self.WEAK_TXN)]))
        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})

        state = c.get("/api/state").json()
        assert len(state["review_queue"]) == 1
        entry = state["review_queue"][0]
        assert entry["name"] == "RESY INC"
        assert any(cand["benefit_id"] == "amex_plat_resy" for cand in entry["candidates"])

    def test_confirming_creates_the_redemption(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(self.WEAK_TXN)]))
        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})
        txn_id = c.get("/api/state").json()["review_queue"][0]["id"]

        resp = c.post(f"/api/review/{txn_id}/confirm", data={"benefit_id": "amex_plat_resy"})
        assert resp.status_code == 200
        state = resp.json()
        assert state["review_queue"] == []
        resy = next(
            e for e in cards(state)["amex_platinum"]["entries"]
            if e.get("id") == "amex_plat_resy"
        )
        assert resy["period_redeemed"] == pytest.approx(25.00)

    def test_rejecting_creates_no_redemption_and_clears_the_queue(self, client, monkeypatch):
        c, main_mod = client
        monkeypatch.setattr(main_mod.plaid_client, "exchange_public_token",
                            lambda pub: ("access-token-1", "item-1"))
        monkeypatch.setattr(main_mod.plaid_client, "sync_transactions",
                            lambda token, cursor: _sync_result(added=[dict(self.WEAK_TXN)]))
        c.post("/api/plaid/exchange", data={"card_id": "amex_platinum", "public_token": "pub-1"})
        txn_id = c.get("/api/state").json()["review_queue"][0]["id"]

        resp = c.post(f"/api/review/{txn_id}/reject")
        assert resp.status_code == 200
        state = resp.json()
        assert state["review_queue"] == []
        resy = next(
            e for e in cards(state)["amex_platinum"]["entries"]
            if e.get("id") == "amex_plat_resy"
        )
        assert resy["period_redeemed"] == 0
