from datetime import date

from app import matching

DIGITAL_ENT = {
    "id": "amex_plat_digital_entertainment",
    "name": "Digital Entertainment Credit",
    "value": 300,
    "cadence": "monthly",
    "detection_hint": {"merchant_patterns": ["NYTIMES", "HULU", "DISNEY PLUS"]},
}

GOLD_DUNKIN = {
    "id": "amex_gold_dunkin",
    "name": "Dunkin' Credit",
    "value": 84,           # $7/mo — matches the real catalog figure
    "cadence": "monthly",
    "detection_hint": {"merchant_patterns": ["DUNKIN"]},
}

RESY = {
    "id": "amex_plat_resy",
    "name": "Resy Credit",
    "value": 400,
    "cadence": "quarterly",
    "detection_hint": {"note": "Qualifying U.S. Resy restaurants; must book via Resy."},
}

CSR_TRAVEL = {
    "id": "csr_travel_credit",
    "name": "Annual Travel Credit",
    "value": 300,
    "cadence": "annual",
    "detection_hint": {"note": "Auto-applies to travel-category charges."},
}


class TestIsCandidateTransaction:
    def test_negative_amount_is_a_candidate(self):
        assert matching.is_candidate_transaction("HULU", -19.99) is True

    def test_positive_amount_normal_purchase_is_not(self):
        assert matching.is_candidate_transaction("WHOLE FOODS", 42.10) is False

    def test_positive_amount_but_issuer_credit_wording_is_still_a_candidate(self):
        # belt-and-suspenders: some feeds may not follow the negative-credit
        # convention consistently, so wording alone can still flag it.
        assert matching.is_candidate_transaction("STATEMENT CREDIT ADJUSTMENT", 19.99) is True


class TestHighConfidenceAutoMatch:
    def test_merchant_and_amount_and_date_all_fit(self):
        result = matching.match_transaction("HULU", -19.99, date(2026, 7, 15), [DIGITAL_ENT])
        assert result.status == "auto_matched"
        assert result.best.benefit_id == "amex_plat_digital_entertainment"
        assert result.best.confidence >= matching.AUTO_APPLY_THRESHOLD

    def test_merchant_matches_but_amount_exceeds_cap_is_rejected(self):
        # $25/mo cap (300/12); $50 is not a plausible partial redemption of it.
        result = matching.match_transaction("HULU", -50.00, date(2026, 7, 15), [DIGITAL_ENT])
        assert result.status == "unmatched"
        assert result.candidates == []

    def test_amount_and_date_fit_but_no_merchant_pattern_match(self):
        result = matching.match_transaction("NETFLIX", -19.99, date(2026, 7, 15), [DIGITAL_ENT])
        assert result.status == "unmatched"

    def test_discontinued_benefit_does_not_match_a_window_after_valid_until(self):
        discontinued = dict(DIGITAL_ENT, valid_until=date(2026, 6, 30))
        result = matching.match_transaction("HULU", -19.99, date(2026, 7, 15), [discontinued])
        assert result.status == "unmatched"


class TestAmbiguousMatchGoesToReview:
    def test_two_benefits_both_plausibly_match_never_auto_picks_one(self):
        # Contrived: DUNKIN also happens to fit Digital Entertainment's cap —
        # the engine must not silently prefer one.
        overlapping = dict(DIGITAL_ENT, id="fake_overlap", name="Fake Overlap Credit",
                           detection_hint={"merchant_patterns": ["DUNKIN"]})
        result = matching.match_transaction("DUNKIN #4021", -7.00, date(2026, 7, 15),
                                            [GOLD_DUNKIN, overlapping])
        assert result.status == "needs_review"
        assert len(result.candidates) == 2
        ids = {c.benefit_id for c in result.candidates}
        assert ids == {"amex_gold_dunkin", "fake_overlap"}


class TestNoMerchantPatternBenefits:
    def test_issuer_credit_wording_scores_medium_confidence_review(self):
        result = matching.match_transaction("AMEX OFFER CREDIT RESY", -25.00, date(2026, 8, 1), [RESY])
        assert result.status == "needs_review"
        assert result.best.benefit_id == "amex_plat_resy"
        assert 0.3 < result.best.confidence < matching.AUTO_APPLY_THRESHOLD

    def test_no_issuer_wording_still_surfaces_as_weak_review_candidate(self):
        result = matching.match_transaction("RESY INC", -25.00, date(2026, 8, 1), [RESY])
        assert result.status == "needs_review"
        assert result.best.confidence == matching.REVIEW_THRESHOLD

    def test_never_auto_applies_without_a_merchant_pattern(self):
        # Even a "perfect" amount+date fit for a no-pattern benefit must not
        # cross into auto_matched — there's no merchant signal to justify it.
        result = matching.match_transaction("STATEMENT CREDIT", -300.00, date(2026, 3, 1), [CSR_TRAVEL])
        assert result.status == "needs_review"
        assert result.best.confidence < matching.AUTO_APPLY_THRESHOLD


class TestQuarterlyAndAnnualWindows:
    def test_quarterly_window_selects_the_right_quarter(self):
        # Resy is $400/yr over 4 quarters = $100/quarter.
        result = matching.match_transaction("RESY INC", -100.00, date(2026, 4, 2), [RESY])
        assert result.best.window_label == "Q2 2026"
        assert result.best.allowance == 100.0

    def test_annual_window_covers_the_whole_year(self):
        result = matching.match_transaction("DELTA AIR LINES", -300.00, date(2026, 11, 20), [CSR_TRAVEL])
        assert result.best.window_label == "2026"
        assert result.best.allowance == 300.0
