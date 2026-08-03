"""Unit tests for the plaid_client wrapper itself — the Plaid SDK client is
faked out so these run with no network access, but they exercise the real
request-building logic (this is exactly where a real bug was found: passing
cursor=None to the SDK's request model raises a type error instead of
starting a fresh sync)."""

from app import plaid_client


class FakeSdkClient:
    """Records every TransactionsSyncRequest it's called with, and plays back
    one page per call from a scripted list of responses."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def transactions_sync(self, req):
        # Mirror the real SDK: cursor=None would raise ApiTypeError before
        # ever reaching here, so recording whether 'cursor' was even set
        # is what actually catches the regression.
        self.calls.append({"has_cursor_kwarg": "cursor" in req, "cursor": req.get("cursor")})
        return self.pages.pop(0)


def _page(added=(), modified=(), removed=(), next_cursor="c1", has_more=False):
    return {"added": list(added), "modified": list(modified), "removed": list(removed),
            "next_cursor": next_cursor, "has_more": has_more}


class TestSyncTransactionsCursorHandling:
    def test_first_sync_omits_cursor_kwarg_entirely(self, monkeypatch):
        fake = FakeSdkClient([_page(next_cursor="c1")])
        monkeypatch.setattr(plaid_client, "_get_client", lambda: fake)

        result = plaid_client.sync_transactions("access-token", None)

        assert fake.calls[0]["has_cursor_kwarg"] is False
        assert result["next_cursor"] == "c1"

    def test_later_sync_passes_the_stored_cursor(self, monkeypatch):
        fake = FakeSdkClient([_page(next_cursor="c2")])
        monkeypatch.setattr(plaid_client, "_get_client", lambda: fake)

        plaid_client.sync_transactions("access-token", "c1")

        assert fake.calls[0]["has_cursor_kwarg"] is True
        assert fake.calls[0]["cursor"] == "c1"

    def test_loops_until_has_more_is_false(self, monkeypatch):
        fake = FakeSdkClient([
            _page(added=[{"transaction_id": "t1"}], next_cursor="c1", has_more=True),
            _page(added=[{"transaction_id": "t2"}], next_cursor="c2", has_more=False),
        ])
        monkeypatch.setattr(plaid_client, "_get_client", lambda: fake)

        result = plaid_client.sync_transactions("access-token", None)

        assert len(fake.calls) == 2
        assert [t["transaction_id"] for t in result["added"]] == ["t1", "t2"]
        assert result["next_cursor"] == "c2"
