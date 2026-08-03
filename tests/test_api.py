"""End-to-end tests through the real FastAPI app.

Every number the UI shows comes from `/api/state`, and every action returns a
fresh copy of it — so these tests are the real guard against a figure in one view
disagreeing with the same figure in another.
"""

import importlib
import os
import tempfile

import pytest


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    os.environ["TODAY_OVERRIDE"] = "2026-07-18"
    try:
        from app import db as db_mod
        importlib.reload(db_mod)
        from app import roi as roi_mod
        importlib.reload(roi_mod)
        from app import main as main_mod
        importlib.reload(main_mod)
        from fastapi.testclient import TestClient
        yield TestClient(main_mod.app)
    finally:
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("TODAY_OVERRIDE", None)
        os.unlink(path)


def cards(state):
    return {c["id"]: c for c in state["cards"]}


def find(state, card_id, name_part):
    for e in cards(state)[card_id]["entries"]:
        if e["kind"] == "group":
            if name_part in e["title"]:
                return e
            for m in e["members"]:
                if name_part in m["name"]:
                    return m
        elif name_part in e["name"]:
            return e
    raise AssertionError(f"{name_part} not found on {card_id}")


def test_page_and_state_load(client):
    assert client.get("/").status_code == 200
    s = client.get("/api/state").json()
    assert s["today"] == "2026-07-18"
    assert len(s["cards"]) == 4


class TestPeriodGrids:
    def test_monthly_credit_has_twelve_cells(self):
        pass  # covered in test_periods.py

    def test_semiannual_credit_shows_two_windows(self, client):
        s = client.get("/api/state").json()
        hotel = find(s, "amex_platinum", "Hotel Credit")
        cells = hotel["grid"]["cells"]
        assert len(cells) == 2
        assert [c["long_label"] for c in cells] == ["H1 · Jan–Jun", "H2 · Jul–Dec"]
        # H1 closed unused -> forfeited, and must read that way rather than
        # disappearing into a single annual number
        assert cells[0]["state"] == "missed"
        assert cells[1]["state"] == "open"

    def test_logging_a_period_rolls_up_to_the_card(self, client):
        before = cards(client.get("/api/state").json())["amex_platinum"]["actual"]
        hotel = find(client.get("/api/state").json(), "amex_platinum", "Hotel Credit")
        s = client.post("/api/period", data={"benefit_id": hotel["id"], "index": 1}).json()
        assert cards(s)["amex_platinum"]["actual"] == pytest.approx(before + 300)

    def test_logging_is_reversible_via_explicit_zero(self, client):
        # /api/period is a "set", not a "toggle" — clearing a window means
        # posting amount=0, not re-clicking blind. That's what makes a logged
        # amount editable rather than only clearable (see partial-amount tests).
        hotel = find(client.get("/api/state").json(), "amex_platinum", "Hotel Credit")
        base = cards(client.get("/api/state").json())["amex_platinum"]["actual"]
        client.post("/api/period", data={"benefit_id": hotel["id"], "index": 1})
        s = client.post("/api/period", data={"benefit_id": hotel["id"], "index": 1, "amount": 0}).json()
        assert cards(s)["amex_platinum"]["actual"] == pytest.approx(base)

    def test_discontinued_windows_are_dead_not_open(self, client):
        # Saks ended 2026-06-30: H2 was never attainable and must not be offered.
        s = client.get("/api/state").json()
        saks = find(s, "amex_platinum", "Saks")
        assert saks["grid"]["cells"][1]["state"] == "dead"


class TestPartialAmounts:
    """Digital Entertainment (and every other periodic credit) gets a different
    amount redeemed each period — the grid has to accept and later EDIT an exact
    figure, not just toggle a cell between $0 and the full cap."""

    def test_a_partial_amount_is_stored_exactly_not_rounded_to_the_cap(self, client):
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        cap = de["grid"]["cells"][0]["allowance"]
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 0, "amount": 12}).json()
        cell = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][0]
        assert cell["redeemed"] == 12
        assert cell["state"] == "done"
        assert cap > 12          # confirms this genuinely under-fills the cap, not a coincidence

    def test_re_clicking_a_logged_period_edits_it_in_place(self, client):
        # Logging $12 then $18 into the SAME window must leave $18, not $30 —
        # "set", not "add". This is what lets a mis-entered month be corrected.
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        client.post("/api/period", data={"benefit_id": de["id"], "index": 2, "amount": 12})
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 2, "amount": 18}).json()
        cell = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][2]
        assert cell["redeemed"] == 18

    def test_partial_amounts_across_different_months_sum_correctly(self, client):
        # Seed data (config/preferences.yaml) lumps the whole $87 YTD figure into
        # January as one row, so logging January's real amount here OVERWRITES
        # that lump — "set" semantics, not additive. Read the pre-existing
        # January value so the expected total accounts for that overwrite,
        # rather than assuming every post is purely additive on top of baseline.
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        before = cards(client.get("/api/state").json())["amex_platinum"]["actual"]
        jan_before = de["grid"]["cells"][0]["redeemed"]

        client.post("/api/period", data={"benefit_id": de["id"], "index": 0, "amount": 25})
        client.post("/api/period", data={"benefit_id": de["id"], "index": 1, "amount": 25})
        client.post("/api/period", data={"benefit_id": de["id"], "index": 2, "amount": 25})
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 3, "amount": 12}).json()

        expected = before - jan_before + 25 + 25 + 25 + 12
        assert cards(s)["amex_platinum"]["actual"] == pytest.approx(expected)
        cells = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"]
        assert [c["redeemed"] for c in cells[:4]] == [25, 25, 25, 12]

    def test_clearing_a_partial_returns_the_window_to_missed(self, client):
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        client.post("/api/period", data={"benefit_id": de["id"], "index": 4, "amount": 12})
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 4, "amount": 0}).json()
        cell = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][4]
        assert cell["redeemed"] == 0
        assert cell["state"] == "missed"          # index 4 (May) is a past month as of 2026-07-18

    def test_omitting_amount_still_logs_the_full_allowance(self, client):
        # The one-click "I used all of it" path must keep working for credits
        # that genuinely are all-or-nothing.
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        cap = de["grid"]["cells"][0]["allowance"]
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 0}).json()
        assert find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][0]["redeemed"] == cap

    def test_bilt_cash_channel_amounts_are_also_editable(self, client):
        # Same "set" semantics apply to Bilt Cash channels — you rarely spend
        # exactly the monthly cap on Lyft either.
        client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 2, "amount": 4})
        s = client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 2, "amount": 6}).json()
        ch = next(c for c in cards(s)["bilt_palladium"]["cash"]["channels"] if c["id"] == "bilt_cash_lyft")
        cell = next(c for c in ch["cells"] if c["index"] == 2)
        assert cell["redeemed"] == 6


class TestGroupedCredits:
    def test_doordash_collapses_to_one_row_with_three_members(self, client):
        g = find(client.get("/api/state").json(), "chase_sapphire_reserve", "DoorDash Credits")
        assert g["kind"] == "group"
        assert len(g["members"]) == 3
        assert g["allowance"] == pytest.approx(25)      # $5 + $10 + $10 per month
        assert g["available"] == 300

    def test_each_bucket_logs_independently(self, client):
        g = find(client.get("/api/state").json(), "chase_sapphire_reserve", "DoorDash Credits")
        nf2 = [m for m in g["members"] if m["short_name"] == "Non-food #2"][0]
        s = client.post("/api/period", data={"benefit_id": nf2["id"], "index": 4}).json()
        g2 = find(s, "chase_sapphire_reserve", "DoorDash Credits")
        assert g2["year_redeemed"] == pytest.approx(10)
        # the other two buckets are untouched — they can't substitute for each other
        assert [m["year_redeemed"] for m in g2["members"] if m["short_name"] != "Non-food #2"] == [0, 0]

    def test_expiring_list_shows_the_group_once(self, client):
        s = client.get("/api/state").json()
        dd = [x for x in s["expiring"] if "DoorDash" in x["name"]]
        assert len(dd) == 1
        assert dd[0]["target"] == "grp-doordash"


class TestDecisions:
    def test_marking_not_applicable_zeroes_realistic(self, client):
        b = find(client.get("/api/state").json(), "chase_sapphire_reserve", "Peloton")
        s = client.post("/api/state/" + b["id"], data={"applicable": "0"}).json()
        assert find(s, "chase_sapphire_reserve", "Peloton")["applicable"] is False
        assert find(s, "chase_sapphire_reserve", "Peloton")["realistic"] == 0

    def test_undecided_blocks_the_verdict_and_resolving_unblocks_it(self, client):
        # Drive both transitions rather than assume which benefit happens to be
        # undecided in the seed data today — that's incidental and drifts as
        # preferences.yaml is edited. Gold is fully resolved as of writing, so
        # asserting it "starts incomplete" would be testing the fixture, not the
        # app; this instead forces a resolved credits-thesis card into the
        # unresolved state and back, which is the actual behavior in view.
        b = find(client.get("/api/state").json(), "amex_platinum", "Airline Fee")
        assert cards(client.get("/api/state").json())["amex_platinum"]["verdict"][0] in ("keep", "cancel")

        s = client.post("/api/state/" + b["id"], data={"applicable": ""}).json()
        assert cards(s)["amex_platinum"]["verdict"][0] == "incomplete"

        s = client.post("/api/state/" + b["id"], data={"applicable": "1"}).json()
        assert cards(s)["amex_platinum"]["verdict"][0] in ("keep", "cancel")

    def test_setting_realistic_value_preserves_applicable(self, client):
        b = find(client.get("/api/state").json(), "chase_sapphire_reserve", "Peloton")
        client.post("/api/state/" + b["id"], data={"applicable": "1"})
        s = client.post("/api/state/" + b["id"], data={"realistic_value": "60"}).json()
        got = find(s, "chase_sapphire_reserve", "Peloton")
        assert got["applicable"] is True and got["realistic"] == 60


class TestBiltCash:
    def test_cash_lives_on_the_bilt_card_and_sums_in(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        assert c["cash"] is not None
        assert c["credits"]["available"] == 400   # the one genuine card credit
        # combined = card credits + Bilt Cash earned
        assert c["available"] == pytest.approx(400 + c["cash"]["earned"])

    def test_logging_a_burn_moves_balance_and_card_total(self, client):
        before = cards(client.get("/api/state").json())["bilt_palladium"]
        s = client.post("/api/cash", data={"channel": "bilt_cash_grubhub", "index": 4}).json()
        after = cards(s)["bilt_palladium"]
        assert after["cash"]["redeemed"] == pytest.approx(before["cash"]["redeemed"] + 10)
        assert after["cash"]["balance"] == pytest.approx(before["cash"]["balance"] - 10)
        assert after["actual"] == pytest.approx(before["actual"] + 10)

    def test_overlap_spend_is_quantified(self, client):
        client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 3})
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        # Lyft duplicates CSR's monthly Lyft credit, so it counts as overlap spend
        assert c["cash"]["wasted_on_overlap"] == pytest.approx(10)

    def test_channels_are_monthly_drillable(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        for ch in c["cash"]["channels"]:
            assert len(ch["cells"]) == 12


class TestOverview:
    def test_no_portfolio_grand_total_only_per_card(self, client):
        s = client.get("/api/state").json()
        assert "totals" not in s          # per-card only, by design
        assert all("gap" in c for c in s["cards"])

    def test_expiring_items_carry_a_navigation_target(self, client):
        s = client.get("/api/state").json()
        assert s["expiring"]
        for x in s["expiring"]:
            assert x["card"] and x["target"]

    def test_logging_removes_an_item_from_the_expiring_list(self, client):
        s = client.get("/api/state").json()
        uber = find(s, "amex_platinum", "Uber Cash")
        assert any(x["target"] == uber["id"] for x in s["expiring"])
        s = client.post("/api/period", data={"benefit_id": uber["id"], "index": 6}).json()
        assert not any(x["target"] == uber["id"] for x in s["expiring"])


def test_digest_is_valid_and_per_card(client):
    d = client.get("/digest").json()
    assert d["generated"] == "2026-07-18"
    assert {c["verdict"] for c in d["cards"]} <= {"keep", "cancel", "pending", "incomplete"}


class TestAmountClamping:
    """An amount above a period's cap must not be accepted verbatim — it self-
    corrects to the cap rather than failing the save or being trusted as-is."""

    def test_period_amount_above_cap_is_clamped(self, client):
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        cap = de["grid"]["cells"][0]["allowance"]
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 0, "amount": 9999}).json()
        cell = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][0]
        assert cell["redeemed"] == cap

    def test_cash_amount_above_cap_is_clamped(self, client):
        s = client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 0, "amount": 9999}).json()
        ch = next(c for c in cards(s)["bilt_palladium"]["cash"]["channels"] if c["id"] == "bilt_cash_lyft")
        assert ch["cells"][0]["redeemed"] == ch["cap"]

    def test_negative_amount_clamps_to_zero_not_negative(self, client):
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        s = client.post("/api/period", data={"benefit_id": de["id"], "index": 0, "amount": -50}).json()
        cell = find(s, "amex_platinum", "Digital Entertainment")["grid"]["cells"][0]
        assert cell["redeemed"] == 0
        assert cell["state"] == "missed"


class TestCadenceLabels:
    """'$150 twice a year · $300 total' beats 'semiannual' — no mental math."""

    def test_monthly_label(self, client):
        de = find(client.get("/api/state").json(), "amex_platinum", "Digital Entertainment")
        assert de["cadence_label"] == "$25/month · $300 total"

    def test_semiannual_label(self, client):
        hotel = find(client.get("/api/state").json(), "amex_platinum", "Hotel Credit")
        assert hotel["cadence_label"] == "$300 twice a year · $600 total"

    def test_grouped_credit_label_sums_all_buckets(self, client):
        dd = find(client.get("/api/state").json(), "chase_sapphire_reserve", "DoorDash Credits")
        # $5 + $10 + $10 monthly, $60 + $120 + $120 annual
        assert dd["cadence_label"] == "$25/month · $300 total"

    def test_per_booking_label_names_the_deadline(self, client):
        hc = find(client.get("/api/state").json(), "amex_gold", "Hotel Collection")
        assert "Dec 31" in hc["cadence_label"]


class TestPlaidVisibility:
    """User-confirmed ground truth: Plaid cleanly sees only the named Amex
    cash-back-style dining credits and Chase's $300 travel credit. Everything
    else routes through a mechanism Plaid can't cleanly match, regardless of
    how the issuer markets it."""

    PLAID_VISIBLE = {
        "amex_plat_resy", "amex_plat_digital_entertainment",
        "amex_gold_dining", "amex_gold_resy", "amex_gold_dunkin",
        "csr_travel_credit",
    }

    def test_exactly_the_confirmed_benefits_are_plaid_auto(self):
        from app.catalog import Catalog
        c = Catalog()
        actual = {bid for bid, b in c.benefits.items() if b.get("tracking_mode") == "plaid_auto"}
        assert actual == self.PLAID_VISIBLE


class TestBiltHotelCreditResolved:
    """The $100/mo 'Bilt Travel Hotel Credit' Kudos showed as a standalone
    benefit was Bilt Cash's travel channel double-counted. Confirms it's gone
    from card credits and lives only once, inside bilt_cash.channels."""

    def test_monthly_hotel_credit_no_longer_exists_as_a_benefit(self):
        from app.catalog import Catalog
        assert "bilt_monthly_hotel_credit" not in Catalog().benefits

    def test_travel_channel_is_not_flagged_as_wasteful_overlap(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        travel = next(ch for ch in c["cash"]["channels"] if ch["id"] == "bilt_cash_travel")
        assert travel["overlap"] is False
        assert travel["best_use"] is True

    def test_only_the_semiannual_hotel_credit_remains_as_a_card_credit(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        names = [e["name"] for e in c["entries"]]
        assert names == ["Bilt Travel Hotel Credit (semiannual)"]


class TestReferenceBenefitsShowTheirCard:
    def test_every_reference_entry_has_a_card_label(self, client):
        s = client.get("/api/state").json()
        assert len(s["reference"]) > 5
        for r in s["reference"]:
            assert r["card_label"] in {"Platinum", "Gold", "CSR", "Bilt"}

    def test_csr_protections_are_present(self, client):
        s = client.get("/api/state").json()
        names = {r["name"] for r in s["reference"] if r["card_label"] == "CSR"}
        assert "Primary Rental Car Insurance" in names
        assert "Trip Cancellation / Interruption Insurance" in names


class TestCSRFeeCorrection:
    def test_csr_costs_795_not_990(self, client):
        # User confirmed: the aunt's $195 AU fee is hers, not the primary
        # cardholder's. Correcting this shouldn't silently erase the AU's
        # existence from the data — just stop charging her fee to the wrong person.
        c = cards(client.get("/api/state").json())["chase_sapphire_reserve"]
        assert c["cost"] == 795
        assert c["au_fee"] is None


class TestCustomCashChannels:
    """Bilt lets you redeem into categories the catalog doesn't enumerate —
    the user must be able to add one, and it must survive a reload exactly
    like a manually logged redemption does."""

    def test_walgreens_renamed_from_generic_other(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        names = [ch["name"] for ch in c["cash"]["channels"]]
        assert "Walgreens" in names
        assert not any("Other" in n for n in names)

    def test_adding_a_channel_persists_across_a_simulated_reload(self, client):
        s = client.post("/api/cash/channel", data={"name": "Target", "monthly_cap": 15}).json()
        c = cards(s)["bilt_palladium"]
        added = next(ch for ch in c["cash"]["channels"] if ch["name"] == "Target")
        assert added["cap"] == 15
        assert added["custom"] is True
        assert len(added["cells"]) == 12

        # "Reload" = fetch state fresh, independent of the POST's response —
        # this is exactly what a real page refresh does (GET /api/state).
        s2 = client.get("/api/state").json()
        c2 = cards(s2)["bilt_palladium"]
        assert any(ch["name"] == "Target" for ch in c2["cash"]["channels"])

    def test_a_burn_on_a_custom_channel_rolls_up_to_the_card(self, client):
        s = client.post("/api/cash/channel", data={"name": "Target", "monthly_cap": 15}).json()
        ch = next(c for c in cards(s)["bilt_palladium"]["cash"]["channels"] if c["name"] == "Target")
        before = cards(s)["bilt_palladium"]["actual"]
        s2 = client.post("/api/cash", data={"channel": ch["id"], "index": 6, "amount": 12}).json()
        assert cards(s2)["bilt_palladium"]["actual"] == pytest.approx(before + 12)

    def test_duplicate_names_get_distinct_ids_not_overwritten(self, client):
        client.post("/api/cash/channel", data={"name": "CVS", "monthly_cap": 10})
        s = client.post("/api/cash/channel", data={"name": "CVS", "monthly_cap": 20}).json()
        cvs_channels = [ch for ch in cards(s)["bilt_palladium"]["cash"]["channels"] if ch["name"] == "CVS"]
        assert len(cvs_channels) == 2
        assert len({ch["id"] for ch in cvs_channels}) == 2   # distinct ids, second one not overwriting the first
        assert {ch["cap"] for ch in cvs_channels} == {10, 20}

    def test_blank_name_is_rejected(self, client):
        r = client.post("/api/cash/channel", data={"name": "   ", "monthly_cap": 10})
        assert r.status_code == 400

    def test_zero_or_negative_cap_is_rejected(self, client):
        r = client.post("/api/cash/channel", data={"name": "Target", "monthly_cap": 0})
        assert r.status_code == 400


class TestBiltCashRealisticIsRedeemedNotEarned:
    """'Realistic' means redeemed, not earned — the same 'capture is not
    realization' rule every other benefit follows. An unspent balance is a
    number in an account, not value you've actually gotten."""

    def test_realistic_equals_zero_when_nothing_redeemed(self, client):
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        assert c["cash"]["earned"] == 200
        assert c["cash"]["redeemed"] == 0
        assert c["cash"]["realistic"] == 0

    def test_realistic_tracks_redemptions_not_the_full_balance(self, client):
        client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 3, "amount": 10})
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        assert c["cash"]["earned"] == 200        # unchanged — still available to spend
        assert c["cash"]["redeemed"] == 10
        assert c["cash"]["realistic"] == 10       # tracks redeemed, not the $200 balance

    def test_combined_bilt_total_uses_the_corrected_realistic(self, client):
        client.post("/api/cash", data={"channel": "bilt_cash_lyft", "index": 3, "amount": 10})
        c = cards(client.get("/api/state").json())["bilt_palladium"]
        # combined realistic = card-credit realistic (0, nothing marked useful
        # yet) + cash realistic (10, what's actually been redeemed) — not the
        # full $200 balance inflating the number.
        assert c["realistic"] == pytest.approx(c["credits"]["realistic"] + 10)


class TestBiltReferenceBenefits:
    def test_bilt_protections_are_present(self, client):
        s = client.get("/api/state").json()
        names = {r["name"] for r in s["reference"] if r["card_label"] == "Bilt"}
        assert "Purchase Protection" in names
        assert "Cell Phone Protection" in names
        assert "Trip Cancellation / Interruption Insurance" in names


class TestAddEarned:
    """The Bilt app is the source of truth for the balance — Plaid can't read a
    loyalty-currency ledger, so a reconciling top-up has to be a manual action
    the user can take themselves, not something only fixable by hand in SQL."""

    def test_add_earned_raises_the_balance(self, client):
        before = cards(client.get("/api/state").json())["bilt_palladium"]["cash"]["balance"]
        s = client.post("/api/cash/earn", data={"amount": 330, "note": "reconciled to real Bilt balance"}).json()
        c = cards(s)["bilt_palladium"]
        assert c["cash"]["balance"] == pytest.approx(before + 330)
        assert c["cash"]["earned"] == pytest.approx(200 + 330)

    def test_add_earned_does_not_change_realistic(self, client):
        # Realistic tracks redeemed, not earned (see TestBiltCashRealisticIsRedeemedNotEarned)
        # — a reconciling top-up must not silently inflate it.
        before = cards(client.get("/api/state").json())["bilt_palladium"]["cash"]["realistic"]
        s = client.post("/api/cash/earn", data={"amount": 330}).json()
        assert cards(s)["bilt_palladium"]["cash"]["realistic"] == before

    def test_reconciling_to_a_known_real_balance(self, client):
        # The exact scenario: user knows their true current balance ($510) but
        # not yet the historical earn/redeem detail behind it. Top up by the
        # gap and the tracked balance should land exactly on the real number.
        before = cards(client.get("/api/state").json())["bilt_palladium"]["cash"]["balance"]
        gap = 510 - before
        s = client.post("/api/cash/earn", data={"amount": gap, "note": "reconciliation — real record still TBD"}).json()
        assert cards(s)["bilt_palladium"]["cash"]["balance"] == pytest.approx(510)


class TestBiltCashAtRiskIsFlagged:
    """A large unredeemed balance is worth surfacing well before its Dec 31
    deadline, not just once it's within the usual 30-day expiring window —
    there's months of runway to actually spend it down."""

    def test_at_risk_balance_bumps_the_tab_attention_badge(self, client):
        # Seed state already has $200 balance ($100 at risk), so establish a
        # true no-risk baseline first by spending it down to exactly $0.
        client.post("/api/cash", data={"channel": "bilt_cash_travel", "index": 6, "amount": 100})
        client.post("/api/cash", data={"channel": "bilt_cash_travel", "index": 5, "amount": 100})
        baseline = cards(client.get("/api/state").json())["bilt_palladium"]
        assert baseline["cash"]["balance"] == 0
        assert baseline["cash"]["at_risk"] == 0
        before = baseline["attention"]

        client.post("/api/cash/earn", data={"amount": 300})  # -> $300 balance, $200 at risk
        s = client.get("/api/state").json()
        c = cards(s)["bilt_palladium"]
        assert c["cash"]["at_risk"] > 0
        assert c["attention"] == before + 1

    def test_badge_drops_once_balance_is_spent_down_to_the_100_threshold(self, client):
        # Seed state: $200 earned, $0 redeemed -> $200 balance -> $100 at risk,
        # so the badge already includes the +1 at-risk flag here.
        before_attn = cards(client.get("/api/state").json())["bilt_palladium"]["attention"]
        # Spend the $100-over-threshold portion via the travel channel's cap.
        client.post("/api/cash", data={"channel": "bilt_cash_travel", "index": 6, "amount": 100})
        s = client.get("/api/state").json()
        c = cards(s)["bilt_palladium"]
        assert c["cash"]["balance"] == 100
        assert c["cash"]["at_risk"] == 0
        assert c["attention"] == before_attn - 1


class TestPointsRedemptions:
    """Phase 4: log points value realized per card. Per the user's rules, points
    are BONUS info for the credits-thesis cards (Amex Platinum, CSR) and do NOT
    enter their verdict; only Bilt (points thesis) folds points into its verdict."""

    IHG = {"card_id": "chase_sapphire_reserve", "description": "IHG hotel",
           "card_points": 51000, "cash_value": 553, "partner": "IHG",
           "transfer_bonus_pct": 100, "partner_points_total": 110000}

    def test_transfer_bonus_and_mixing_attributes_the_card_its_share(self, client):
        s = client.post("/api/points", data=self.IHG).json()
        p = cards(s)["chase_sapphire_reserve"]["points"]
        it = p["items"][0]
        # 51k UR x2 = 102k of 110k IHG pts (92.73%); $553 x 0.9273 = $512.78
        assert it["realized"] == pytest.approx(512.78, abs=0.02)
        assert it["cpp"] == pytest.approx(1.005, abs=0.005)
        assert p["realized"] == pytest.approx(512.78, abs=0.02)

    def test_direct_booking_with_no_partner_attributes_full_value(self, client):
        s = client.post("/api/points", data={
            "card_id": "chase_sapphire_reserve", "description": "Chase Travel flight",
            "card_points": 80000, "cash_value": 1200}).json()
        it = cards(s)["chase_sapphire_reserve"]["points"]["items"][0]
        assert it["realized"] == 1200
        assert it["cpp"] == pytest.approx(1.5)

    def test_csr_is_credits_thesis_points_are_bonus_not_in_verdict(self, client):
        # CSR moved from hybrid -> credits: the verdict is decided by credits
        # (not PENDING), and logging points must NOT move it.
        before = cards(client.get("/api/state").json())["chase_sapphire_reserve"]
        assert before["verdict"][0] != "pending"
        s = client.post("/api/points", data=self.IHG).json()
        csr = cards(s)["chase_sapphire_reserve"]
        assert csr["verdict"] == before["verdict"]                       # unchanged
        assert csr["verdict_value"] == pytest.approx(before["verdict_value"])
        assert csr["points"]["counts_in_verdict"] is False
        assert csr["points"]["realized"] == pytest.approx(512.78, abs=0.02)  # still logged/shown

    def test_points_do_not_change_a_credits_card_verdict(self, client):
        before = cards(client.get("/api/state").json())["amex_platinum"]["verdict"]
        s = client.post("/api/points", data={
            "card_id": "amex_platinum", "card_points": 40000, "cash_value": 600}).json()
        plat = cards(s)["amex_platinum"]
        assert plat["verdict"] == before                    # unchanged
        assert plat["points"]["counts_in_verdict"] is False
        assert plat["points"]["realized"] == 600            # still logged as bonus

    def test_bilt_redemptions_alone_do_not_resolve_pending(self, client):
        # Option A: Bilt's verdict is driven by rent points EARNED, not
        # redeemed. Logging a redemption must NOT resolve PENDING or count.
        assert cards(client.get("/api/state").json())["bilt_palladium"]["verdict"][0] == "pending"
        s = client.post("/api/points", data={
            "card_id": "bilt_palladium", "card_points": 30000, "cash_value": 450}).json()
        bilt = cards(s)["bilt_palladium"]
        assert bilt["verdict"][0] == "pending"
        assert bilt["points"]["counts_in_verdict"] is False
        assert bilt["points"]["realized"] == 450   # still logged/shown

    def test_cards_without_a_points_program_have_no_points_section(self, client):
        assert cards(client.get("/api/state").json())["amex_gold"]["points"] is None

    def test_redemption_is_deletable(self, client):
        s = client.post("/api/points", data=self.IHG).json()
        rid = cards(s)["chase_sapphire_reserve"]["points"]["items"][0]["id"]
        s2 = client.post(f"/api/points/{rid}/delete").json()
        assert cards(s2)["chase_sapphire_reserve"]["points"]["count"] == 0

    def test_rejects_points_on_a_card_without_a_program(self, client):
        r = client.post("/api/points", data={"card_id": "amex_gold", "card_points": 1000, "cash_value": 10})
        assert r.status_code == 400


class TestRentPoints:
    """Bilt Option A: value points EARNED on rent this year (durable, doesn't
    expire) rather than points redeemed (lumpy — a year with no redemption
    would falsely read $0 even though the card generated real value)."""

    MONTHS = [(3, 2343), (4, 2928), (5, 2928), (6, 2996), (7, 1205)]  # user's real data

    def test_logging_a_month_resolves_pending(self, client):
        assert cards(client.get("/api/state").json())["bilt_palladium"]["verdict"][0] == "pending"
        s = client.post("/api/rent-points",
                        data={"card_id": "bilt_palladium", "year": 2026, "month": 3, "points": 2343}).json()
        assert cards(s)["bilt_palladium"]["verdict"][0] != "pending"

    def test_real_five_month_total_and_default_valuation(self, client):
        s = None
        for m, pts in self.MONTHS:
            s = client.post("/api/rent-points",
                            data={"card_id": "bilt_palladium", "year": 2026, "month": m, "points": pts}).json()
        rp = cards(s)["bilt_palladium"]["rent_points"]
        assert rp["total_points"] == sum(p for _, p in self.MONTHS) == 12400
        assert rp["rate_is_default"] is True
        assert rp["rate_cpp"] == 1.0
        assert rp["value"] == pytest.approx(124.0)

    def test_verdict_value_is_credits_plus_rent_points_not_redemptions(self, client):
        for m, pts in self.MONTHS:
            client.post("/api/rent-points",
                        data={"card_id": "bilt_palladium", "year": 2026, "month": m, "points": pts})
        s = client.get("/api/state").json()
        bilt = cards(s)["bilt_palladium"]
        expected = bilt["credits"]["realistic"] + (bilt["cash"]["realistic"] if bilt["cash"] else 0) + 124.0
        assert bilt["verdict_value"] == pytest.approx(expected)

    def test_logging_a_redemption_refines_the_valuation_rate(self, client):
        # A real Bilt points redemption at 1.8cpp should pull the rate off the
        # 1c default — this is what "refine toward your actual cpp" means.
        client.post("/api/rent-points",
                    data={"card_id": "bilt_palladium", "year": 2026, "month": 3, "points": 10000})
        client.post("/api/points", data={
            "card_id": "bilt_palladium", "description": "Alaska transfer",
            "card_points": 10000, "cash_value": 180})   # 1.8 cpp
        s = client.get("/api/state").json()
        rp = cards(s)["bilt_palladium"]["rent_points"]
        assert rp["rate_is_default"] is False
        assert rp["rate_cpp"] == pytest.approx(1.8)
        assert rp["value"] == pytest.approx(10000 * 1.8 / 100)

    def test_month_is_idempotent_correcting_not_adding(self, client):
        client.post("/api/rent-points",
                    data={"card_id": "bilt_palladium", "year": 2026, "month": 3, "points": 2000})
        s = client.post("/api/rent-points",
                        data={"card_id": "bilt_palladium", "year": 2026, "month": 3, "points": 2343}).json()
        rp = cards(s)["bilt_palladium"]["rent_points"]
        assert rp["total_points"] == 2343
        assert len(rp["months"]) == 1

    def test_deletable(self, client):
        client.post("/api/rent-points",
                    data={"card_id": "bilt_palladium", "year": 2026, "month": 3, "points": 2343})
        s = client.post("/api/rent-points/bilt_palladium/2026/3/delete").json()
        assert cards(s)["bilt_palladium"]["rent_points"]["total_points"] == 0

    def test_rejects_a_card_without_a_rent_points_model(self, client):
        r = client.post("/api/rent-points",
                        data={"card_id": "chase_sapphire_reserve", "year": 2026, "month": 3, "points": 100})
        assert r.status_code == 400

    def test_no_rent_points_section_for_other_cards(self, client):
        assert cards(client.get("/api/state").json())["chase_sapphire_reserve"]["rent_points"] is None
