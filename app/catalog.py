"""Load the YAML catalog and preferences.

Separation of concerns, deliberately:
  catalog.yaml     -> objective card terms. Changes when an ISSUER changes them.
  preferences.yaml -> your judgments. Changes when YOU change your mind.
  SQLite           -> your toggles and redemptions. Written by the UI.

Reload order on startup: YAML defines what exists; the DB overrides personal
state. Editing YAML never clobbers your history; clicking in the UI never
rewrites YAML.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def _load(name: str) -> dict:
    with open(CONFIG / name) as f:
        return yaml.safe_load(f)


def load_catalog() -> dict:
    return _load("catalog.yaml")


def load_preferences() -> dict:
    return _load("preferences.yaml")


class Catalog:
    """Read-only view over the YAML, with the lookups the app actually needs."""

    def __init__(self) -> None:
        raw = load_catalog()
        prefs = load_preferences()

        self.raw = raw
        self.cards = {c["id"]: c for c in raw["cards"]}
        self.benefits = {b["id"]: b for b in raw["benefits"]}
        self.reference = raw.get("reference_benefits", [])
        self.bilt_cash = raw.get("bilt_cash")
        self.bilt_points = raw.get("bilt_points_model")

        self.benefit_state = prefs.get("benefit_state", {}) or {}
        self.card_state = prefs.get("card_state", {}) or {}
        self.seed_redemptions = prefs.get("seed_redemptions_2026", {}) or {}

        # Fail loudly on a benefit pointing at a card that doesn't exist —
        # a typo here would silently drop it from every total.
        for bid, b in self.benefits.items():
            if b["card"] not in self.cards:
                raise ValueError(f"benefit {bid!r} references unknown card {b['card']!r}")

    def benefits_for(self, card_id: str) -> list[dict]:
        return [b for b in self.benefits.values() if b["card"] == card_id]

    def state_for(self, benefit_id: str) -> dict:
        return self.benefit_state.get(benefit_id) or {}

    def card_state_for(self, card_id: str) -> dict:
        return self.card_state.get(card_id) or {}

    def annual_cost(self, card_id: str) -> float:
        """What the card actually costs you — including AU fees.

        The CSR is $795 on the marketing page and $990 in your life, because
        the aunt's authorized-user fee is money you pay.
        """
        c = self.cards[card_id]
        return float(c.get("effective_annual_cost") or c["annual_fee"])
