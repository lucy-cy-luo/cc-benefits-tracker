"""ROI math and verdict logic.

The verdict is the app's only real output. These tests pin down when it must
refuse to produce a number — refusing is a feature, and a regression that made
it start guessing would be invisible in the UI.
"""

import pytest

from app.roi import bilt_tier_status, verdict


class TestVerdictRunsOnRealistic:
    def test_keeps_when_realistic_clears_the_fee(self):
        state, detail = verdict(1190, 895, 0, "credits")
        assert state == "keep"
        assert "295" in detail and "over fee" in detail   # plain-English label

    def test_cancels_when_realistic_falls_short(self):
        state, detail = verdict(400, 895, 0, "credits")
        assert state == "cancel" and "495" in detail and "under fee" in detail

    def test_low_capture_alone_does_not_condemn_a_good_card(self):
        # Verdict runs on realistic, never on what you happened to capture —
        # forgetting to use a card is an execution problem, not a product problem.
        assert verdict(1190, 895, 0, "credits")[0] == "keep"


class TestVerdictRefusals:
    def test_points_thesis_is_pending_only_until_points_are_logged(self):
        # Bilt: $500 realistic vs $495 fee would print a meaningless "KEEP" while
        # ignoring the rent points the card exists for — SO LONG AS no points are
        # logged. That's now the condition, not a permanent refusal.
        assert verdict(500, 495, 0, "points", has_points_data=False)[0] == "pending"
        # Once points exist the caller folds them into `real` and the math runs.
        assert verdict(500, 495, 0, "points", has_points_data=True)[0] == "keep"

    def test_hybrid_thesis_pending_until_points_then_computes(self):
        assert verdict(300, 990, 0, "hybrid", has_points_data=False)[0] == "pending"
        assert verdict(1100, 990, 0, "hybrid", has_points_data=True)[0] == "keep"

    def test_undecided_benefits_block_the_verdict(self):
        state, detail = verdict(300, 795, 12, "credits")
        assert state == "incomplete" and "12" in detail

    def test_verdict_unblocks_once_everything_is_decided(self):
        assert verdict(900, 795, 0, "credits")[0] == "keep"


class TestBiltTierCliff:
    """The housing multiplier is a step function. Missing 100% by a dollar costs
    0.25x on the entire rent payment."""

    CATALOG = type("C", (), {"bilt_points": {
        "housing_payment_monthly": 2411,
        "tiers": [
            {"spend_pct_of_rent": 0, "multiplier": None, "flat_points": 250},
            {"spend_pct_of_rent": 25, "multiplier": 0.5},
            {"spend_pct_of_rent": 50, "multiplier": 0.75},
            {"spend_pct_of_rent": 75, "multiplier": 1.0},
            {"spend_pct_of_rent": 100, "multiplier": 1.25},
        ],
    }})()

    def test_below_first_tier_earns_flat_points_not_a_multiplier(self):
        s = bilt_tier_status(self.CATALOG, 100)
        assert s["current_multiplier"] is None and s["current_points"] == 250

    def test_identifies_the_next_tier_and_what_it_costs(self):
        s = bilt_tier_status(self.CATALOG, 2411 * 0.75)
        assert s["current_multiplier"] == 1.0
        assert s["next_tier"]["pct"] == 100
        assert s["next_tier"]["spend_needed"] == pytest.approx(602.75, abs=1)

    def test_a_dollar_short_of_a_tier_stays_on_the_lower_multiplier(self):
        # $1,808 is 74.99% of $2,411 and earns 0.75x, not 1.0x. This exactness is
        # the whole reason the cliff is worth tracking rather than eyeballing.
        assert bilt_tier_status(self.CATALOG, 1808)["current_multiplier"] == 0.75

    def test_marginal_dollar_beats_the_headline_2x_near_a_boundary(self):
        assert bilt_tier_status(self.CATALOG, 2200)["next_tier"]["marginal_rate"] > 4.0

    def test_no_next_tier_once_maxed(self):
        s = bilt_tier_status(self.CATALOG, 3000)
        assert s["current_multiplier"] == 1.25 and s["next_tier"] is None


class TestRealCatalogIntegrity:
    """Guards against the catalog drifting out from under the math."""

    def test_catalog_parses_and_every_benefit_has_a_real_card(self):
        from app.catalog import Catalog
        c = Catalog()                     # raises on unknown card refs
        assert len(c.benefits) > 30
        # User confirmed the aunt (AU) pays her own $195 fee — not 795+195.
        assert c.annual_cost("chase_sapphire_reserve") == 795
        assert c.annual_cost("amex_platinum") == 895

    def test_every_benefit_carries_a_tracking_mode(self):
        from app.catalog import Catalog
        for bid, b in Catalog().benefits.items():
            assert b.get("tracking_mode") in {"plaid_auto", "app_only_manual", "planned"}, bid

    def test_csr_benefits_reconcile_to_its_available_total(self):
        # The sum of every CSR credit must equal the card's headline available
        # value. Catches a credit being added, dropped, or mis-valued.
        from app.catalog import Catalog
        c = Catalog()
        assert sum(b["value"] for b in c.benefits_for("chase_sapphire_reserve")) == 3468

    def test_doordash_is_three_separate_buckets_in_one_group(self):
        from app.catalog import Catalog
        c = Catalog()
        dd = [b for b in c.benefits_for("chase_sapphire_reserve") if b.get("group") == "doordash"]
        assert len(dd) == 3
        assert sorted(b["value"] for b in dd) == [60, 120, 120]   # $5 + $10 + $10 per month

    def test_bilt_cash_award_is_not_also_a_benefit(self):
        # It was counted twice: once as a credit, once as Bilt Cash earnings.
        from app.catalog import Catalog
        c = Catalog()
        assert "bilt_annual_cash" not in c.benefits
        assert (c.bilt_cash["earn"]["annual_award"]) == 200

    def test_priority_pass_is_reference_only(self):
        # Notional and triple-counted across Platinum/CSR/Bilt lounge access.
        from app.catalog import Catalog
        c = Catalog()
        assert "bilt_priority_pass" not in c.benefits
        assert any("Priority Pass" in r["name"] for r in c.reference)

    def test_bilt_card_credits_total_400(self):
        # Was $1,600 (a $1,200 "monthly hotel credit" + the real $400 semiannual
        # one) until research confirmed the $1,200 line was Bilt Cash's travel
        # channel counted a second time as a fake standalone credit. Removed —
        # the $400 semiannual hotel credit is the only genuine card credit Bilt
        # has; the $100/mo travel value now lives only in bilt_cash.channels.
        from app.catalog import Catalog
        c = Catalog()
        assert sum(b["value"] for b in c.benefits_for("bilt_palladium")) == 400
