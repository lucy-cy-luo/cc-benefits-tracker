"""SQLite storage. Local file, never leaves this machine.

Holds only what the YAML can't: your toggles and your redemption history.
No Plaid tokens live here yet — when they do (Phase 2), they get encrypted
before insert, with the key in the macOS Keychain. See .env.example.

WHY THE DB IS NOT STORED NEXT TO THIS CODE
------------------------------------------
This project lives in a Google Drive folder. Putting the database there would:
  1. Upload your financial data to Google — violating the "local-first, no
     third-party hosting" constraint that started this project. In Phase 2 this
     file holds Plaid access tokens; syncing it to a cloud drive is exactly the
     thing we're not doing.
  2. Risk corruption. Drive's file-streaming client syncs files mid-write;
     SQLite in a synced folder is a well-documented way to lose data.

So the DB defaults to ~/Library/Application Support/ — local, outside Drive,
and the conventional macOS location for app data. Config YAML stays in Drive
(card terms and your judgments are not sensitive and benefit from syncing).
Override with DATABASE_PATH if you want it somewhere else — just not in Drive.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DEFAULT_DB = Path.home() / "Library" / "Application Support" / "cc-benefits-tracker" / "benefits.db"
DB_PATH = Path(os.getenv("DATABASE_PATH") or DEFAULT_DB).expanduser()

_CLOUD_MARKERS = ("CloudStorage", "Google Drive", "Dropbox", "OneDrive", "iCloud Drive")
if any(m in str(DB_PATH) for m in _CLOUD_MARKERS):
    raise RuntimeError(
        f"Refusing to open a database inside a cloud-synced folder:\n  {DB_PATH}\n"
        "This file holds your financial data (and Plaid tokens in Phase 2). "
        "Point DATABASE_PATH somewhere local."
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS benefit_state (
    benefit_id      TEXT PRIMARY KEY,
    enrolled        INTEGER,          -- 0/1/NULL(unknown)
    applicable      INTEGER,          -- 0/1/NULL(undecided)
    realistic_value REAL,             -- NULL => fall back to catalog value
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS card_state (
    card_id         TEXT PRIMARY KEY,
    spend_gate_met  INTEGER,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS redemptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    benefit_id  TEXT NOT NULL,
    date        TEXT NOT NULL,
    amount      REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | plaid
    period      TEXT,                             -- e.g. "Jul 2026", "H2 2026"
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redemptions_benefit ON redemptions(benefit_id);
CREATE INDEX IF NOT EXISTS idx_redemptions_period  ON redemptions(benefit_id, period);

-- Bilt Cash is a currency, not a credit: it needs earn/burn, not period resets.
CREATE TABLE IF NOT EXISTS bilt_cash_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    direction   TEXT NOT NULL,        -- earn | burn
    amount      REAL NOT NULL,
    channel     TEXT,                 -- burn only: grubhub | lyft | travel | housing | other
    note        TEXT,
    created_at  TEXT NOT NULL
);

-- Bilt Cash channels the user adds themselves — the catalog's channel list
-- (Grubhub, Lyft, etc.) is fixed at YAML-edit time, but Bilt lets you redeem
-- into categories that aren't in that list. User-added channels live here so
-- they survive a reload exactly like a manually logged redemption does.
CREATE TABLE IF NOT EXISTS custom_cash_channels (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    monthly_cap REAL NOT NULL,
    created_at  TEXT NOT NULL
);

-- Points redemptions — the raw inputs; realized value and cents-per-point are
-- DERIVED (see roi.points_math), never stored, so the attribution logic can be
-- corrected without a migration. One row per redemption event.
--   card_points          = how many of THIS card's program points were spent
--   cash_value           = dollar value of what was booked
--   partner              = transfer partner (e.g. "IHG"); NULL if booked direct
--   transfer_bonus_pct   = e.g. 100 for a +100% transfer bonus; 0 if none
--   partner_points_total = total partner points the redemption consumed,
--                          INCLUDING any the user already held; NULL = no mixing
--                          (attribute the whole cash_value to this card)
CREATE TABLE IF NOT EXISTS points_redemptions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id              TEXT NOT NULL,
    date                 TEXT NOT NULL,
    description          TEXT,
    card_points          REAL NOT NULL,
    cash_value           REAL NOT NULL,
    partner              TEXT,
    transfer_bonus_pct   REAL NOT NULL DEFAULT 0,
    partner_points_total REAL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_card ON points_redemptions(card_id, date);

-- Rent points earned per month (Option A for Bilt): the marginal points a card
-- generates on rent that no other card captures. Valued at a conservative cpp
-- and fed into that card's verdict. The multiplier varies month to month (it
-- depends on hitting the non-rent spend threshold), so this is stored as the
-- actual points earned each month, not derived from rent x a fixed rate.
CREATE TABLE IF NOT EXISTS rent_points (
    card_id    TEXT NOT NULL,
    year       INTEGER NOT NULL,
    month      INTEGER NOT NULL,   -- 1-12
    points     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (card_id, year, month)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Phase 2: Plaid auto-sync -----------------------------------------------
-- One Item per card. access_token_encrypted is a Fernet ciphertext (app/crypto.py)
-- — the plaintext token never touches this file. `cursor` drives Plaid's
-- /transactions/sync pagination so re-syncing only fetches what's new.
CREATE TABLE IF NOT EXISTS plaid_items (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id                TEXT NOT NULL UNIQUE,
    item_id                TEXT NOT NULL UNIQUE,
    access_token_encrypted TEXT NOT NULL,
    institution_name       TEXT,
    cursor                 TEXT,
    last_synced_at         TEXT,
    created_at             TEXT NOT NULL
);

-- Every transaction Plaid hands back for a linked card, plus what the
-- matching engine (app/matching.py) made of it. `candidates` is a JSON blob
-- of {benefit_id, confidence, reason} for the review-queue UI — kept even
-- after a match resolves, so "why did this land here" stays answerable.
-- `redemption_id` links to the row it created in `redemptions`, so
-- confirming/rejecting/undoing stays a two-way lookup instead of a guess.
CREATE TABLE IF NOT EXISTS plaid_transactions (
    id                 TEXT PRIMARY KEY,     -- Plaid's transaction_id
    item_id            TEXT NOT NULL,
    card_id            TEXT NOT NULL,
    date               TEXT NOT NULL,
    name               TEXT,
    amount             REAL NOT NULL,        -- Plaid convention: +debit / -credit
    pending            INTEGER NOT NULL DEFAULT 0,
    match_status       TEXT NOT NULL DEFAULT 'unmatched',
        -- unmatched | auto_matched | needs_review | confirmed | rejected | ignored
    matched_benefit_id TEXT,
    match_confidence   REAL,
    candidates         TEXT,
    redemption_id      INTEGER,
    raw_json           TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plaid_txn_card   ON plaid_transactions(card_id, date);
CREATE INDEX IF NOT EXISTS idx_plaid_txn_status ON plaid_transactions(match_status);
"""

# Columns added after initial release — CREATE TABLE IF NOT EXISTS won't retrofit
# an existing table, so anything added post-launch is migrated here instead.
# Each entry is (table, column, ddl-fragment); applied only if the column is missing.
_MIGRATIONS = [
    ("redemptions", "plaid_transaction_id", "TEXT"),
]


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(catalog) -> None:
    """Create the schema and seed personal state from YAML — once.

    Seeding is idempotent and one-directional: YAML seeds the DB on first run,
    then the DB is authoritative for personal state. Re-running never clobbers
    toggles you've since changed in the UI.
    """
    with connect() as conn:
        conn.executescript(SCHEMA)
        _run_migrations(conn)

        seeded = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if seeded:
            return

        now = date.today().isoformat()

        for bid, st in catalog.benefit_state.items():
            if bid not in catalog.benefits:
                continue  # stale preference entry; ignore rather than crash
            conn.execute(
                "INSERT OR REPLACE INTO benefit_state"
                " (benefit_id, enrolled, applicable, realistic_value, updated_at)"
                " VALUES (?,?,?,?,?)",
                (
                    bid,
                    _tri(st.get("enrolled")),
                    _tri(st.get("applicable")),
                    st.get("realistic_value"),
                    now,
                ),
            )

        for cid, st in catalog.card_state.items():
            conn.execute(
                "INSERT OR REPLACE INTO card_state (card_id, spend_gate_met, updated_at) VALUES (?,?,?)",
                (cid, _tri(st.get("spend_gate_met")), now),
            )

        for e in catalog.seed_redemptions.get("entries", []) or []:
            conn.execute(
                "INSERT INTO redemptions (benefit_id, date, amount, source, period, note, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    e["benefit_id"],
                    str(e.get("as_of") or f"{date.today().year}-01-01"),
                    float(e["amount"]),
                    "manual",
                    e.get("period"),
                    e.get("note", "seeded from Google Sheet"),
                    now,
                ),
            )

        # Bilt Cash opening balance: the annual award, per the catalog. Seeded as a
        # ledger entry rather than a constant so every later burn nets against it.
        # NOTE: enrollment is required and UNCONFIRMED — if you never enrolled,
        # delete this entry; the balance is then $0 and the award was forfeited.
        spec = getattr(catalog, "bilt_cash", None) or {}
        award = (spec.get("earn") or {}).get("annual_award")
        if award:
            conn.execute(
                "INSERT INTO bilt_cash_ledger (date, direction, amount, channel, note, created_at)"
                " VALUES (?,'earn',?,NULL,?,?)",
                (f"{date.today().year}-01-01", float(award),
                 "annual Bilt Cash award (enrollment unconfirmed)", now),
            )

        conn.execute("INSERT INTO meta (key, value) VALUES ('seeded', ?)", (now,))


def _run_migrations(conn) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _tri(v):
    """None stays None. Unknown is a real state and must survive the round trip."""
    return None if v is None else int(bool(v))


# --- reads -------------------------------------------------------------------

def all_benefit_state() -> dict[str, dict]:
    with connect() as conn:
        return {r["benefit_id"]: dict(r) for r in conn.execute("SELECT * FROM benefit_state")}


def all_card_state() -> dict[str, dict]:
    with connect() as conn:
        return {r["card_id"]: dict(r) for r in conn.execute("SELECT * FROM card_state")}


def redemptions_for(benefit_id: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM redemptions WHERE benefit_id=? ORDER BY date DESC", (benefit_id,)
            )
        ]


def redeemed_in_period(benefit_id: str, start: str, end: str) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM redemptions"
            " WHERE benefit_id=? AND date>=? AND date<=?",
            (benefit_id, start, end),
        ).fetchone()
        return float(row["t"])


def redeemed_in_year(benefit_id: str, year: int) -> float:
    return redeemed_in_period(benefit_id, f"{year}-01-01", f"{year}-12-31")


# --- writes ------------------------------------------------------------------

ALLOWED_STATE_COLS = {"enrolled", "applicable", "realistic_value"}


def set_benefit_state(benefit_id: str, **fields) -> None:
    """Upsert personal state for one benefit. Ensure the row exists, then update
    only the columns provided — so setting `applicable` never wipes an existing
    `realistic_value`, and vice versa."""
    fields = {k: v for k, v in fields.items() if k in ALLOWED_STATE_COLS}
    if not fields:
        return
    now = date.today().isoformat()
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO benefit_state (benefit_id, updated_at) VALUES (?,?)",
            (benefit_id, now),
        )
        conn.execute(
            f"UPDATE benefit_state SET {cols}, updated_at=? WHERE benefit_id=?",
            (*fields.values(), now, benefit_id),
        )


def add_redemption(benefit_id: str, amount: float, when: str, period: str | None, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO redemptions (benefit_id, date, amount, source, period, note, created_at)"
            " VALUES (?,?,?,'manual',?,?,?)",
            (benefit_id, when, float(amount), period, note, date.today().isoformat()),
        )


def delete_redemption(rid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM redemptions WHERE id=?", (rid,))


def bilt_cash_balance() -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='earn' THEN amount ELSE -amount END),0) AS b"
            " FROM bilt_cash_ledger"
        ).fetchone()
        return float(row["b"])


def cash_earned(year: int) -> float:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM bilt_cash_ledger"
            " WHERE direction='earn' AND date>=? AND date<=?",
            (f"{year}-01-01", f"{year}-12-31"),
        ).fetchone()
        return float(row["t"])


def cash_burned(channel: str, start: str, end: str) -> float:
    """Burns on one channel inside one window — the cell value in the cash matrix."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM bilt_cash_ledger"
            " WHERE direction='burn' AND channel=? AND date>=? AND date<=?",
            (channel, start, end),
        ).fetchone()
        return float(row["t"])


def delete_cash_in_window(channel: str, start: str, end: str) -> None:
    """Un-log a cash burn — clicking a green cell again clears that month."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM bilt_cash_ledger WHERE direction='burn' AND channel=?"
            " AND date>=? AND date<=?",
            (channel, start, end),
        )


def delete_redemptions_in_window(benefit_id: str, start: str, end: str) -> None:
    """Un-log a period — clicking a captured cell again clears it."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM redemptions WHERE benefit_id=? AND date>=? AND date<=?",
            (benefit_id, start, end),
        )


def bilt_cash_entries() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM bilt_cash_ledger ORDER BY date DESC, id DESC")]


def add_bilt_cash(direction: str, amount: float, when: str, channel: str | None, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO bilt_cash_ledger (date, direction, amount, channel, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (when, direction, float(amount), channel, note, date.today().isoformat()),
        )


def all_custom_channels() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM custom_cash_channels ORDER BY created_at")]


def add_custom_channel(channel_id: str, name: str, monthly_cap: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO custom_cash_channels (id, name, monthly_cap, created_at) VALUES (?,?,?,?)",
            (channel_id, name, float(monthly_cap), date.today().isoformat()),
        )


# --- points redemptions ------------------------------------------------------

def points_redemptions_for(card_id: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM points_redemptions WHERE card_id=? ORDER BY date DESC, id DESC",
                (card_id,),
            )
        ]


def add_points_redemption(card_id: str, date_: str, description: str, card_points: float,
                          cash_value: float, partner: str | None, transfer_bonus_pct: float,
                          partner_points_total: float | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO points_redemptions"
            " (card_id, date, description, card_points, cash_value, partner,"
            "  transfer_bonus_pct, partner_points_total, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (card_id, date_, description, float(card_points), float(cash_value), partner,
             float(transfer_bonus_pct or 0),
             float(partner_points_total) if partner_points_total else None,
             date.today().isoformat()),
        )


def delete_points_redemption(rid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM points_redemptions WHERE id=?", (rid,))


# --- rent points (Bilt, Option A) ---------------------------------------------

def rent_points_for(card_id: str, year: int) -> list[dict]:
    with connect() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM rent_points WHERE card_id=? AND year=? ORDER BY month",
                (card_id, year),
            )
        ]


def set_rent_points(card_id: str, year: int, month: int, points: float) -> None:
    """Idempotent by (card_id, year, month) — re-logging a month corrects it
    rather than adding a duplicate, same 'set not add' rule as everything else
    the grid touches."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO rent_points (card_id, year, month, points, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(card_id, year, month) DO UPDATE SET points=excluded.points, updated_at=excluded.updated_at",
            (card_id, year, month, float(points), date.today().isoformat()),
        )


def delete_rent_points(card_id: str, year: int, month: int) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM rent_points WHERE card_id=? AND year=? AND month=?",
            (card_id, year, month),
        )


# --- Plaid items ---------------------------------------------------------

def plaid_item_for_card(card_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM plaid_items WHERE card_id=?", (card_id,)).fetchone()
        return dict(row) if row else None


def all_plaid_items() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM plaid_items ORDER BY created_at")]


def upsert_plaid_item(card_id: str, item_id: str, access_token_encrypted: str,
                       institution_name: str | None) -> None:
    now = date.today().isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO plaid_items (card_id, item_id, access_token_encrypted, institution_name, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(card_id) DO UPDATE SET item_id=excluded.item_id,"
            " access_token_encrypted=excluded.access_token_encrypted,"
            " institution_name=excluded.institution_name",
            (card_id, item_id, access_token_encrypted, institution_name, now),
        )


def set_plaid_cursor(card_id: str, cursor: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE plaid_items SET cursor=?, last_synced_at=? WHERE card_id=?",
            (cursor, date.today().isoformat(), card_id),
        )


# --- Plaid transactions ---------------------------------------------------

def upsert_plaid_transaction(card_id: str, item_id: str, txn_id: str, when: str, name: str,
                             amount: float, pending: bool, raw_json: str) -> None:
    """Cursor-sync 'added'/'modified' both land here — a modified transaction
    (e.g. pending -> posted) just overwrites the same row by Plaid's own id."""
    now = date.today().isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO plaid_transactions"
            " (id, item_id, card_id, date, name, amount, pending, raw_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET date=excluded.date, name=excluded.name,"
            " amount=excluded.amount, pending=excluded.pending, raw_json=excluded.raw_json,"
            " updated_at=excluded.updated_at",
            (txn_id, item_id, card_id, when, name, float(amount), int(bool(pending)), raw_json, now, now),
        )


def delete_plaid_transaction(txn_id: str) -> None:
    """Cursor-sync 'removed' — Plaid pulled the transaction back (rare)."""
    with connect() as conn:
        conn.execute("DELETE FROM plaid_transactions WHERE id=?", (txn_id,))


def plaid_transaction(txn_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM plaid_transactions WHERE id=?", (txn_id,)).fetchone()
        return dict(row) if row else None


def set_transaction_match(txn_id: str, match_status: str, matched_benefit_id: str | None = None,
                          match_confidence: float | None = None, candidates_json: str | None = None,
                          redemption_id: int | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE plaid_transactions SET match_status=?, matched_benefit_id=?,"
            " match_confidence=?, candidates=?, redemption_id=?, updated_at=? WHERE id=?",
            (match_status, matched_benefit_id, match_confidence, candidates_json,
             redemption_id, date.today().isoformat(), txn_id),
        )


def review_queue() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM plaid_transactions WHERE match_status='needs_review' ORDER BY date DESC"
        )]


def has_non_plaid_redemption_in_window(benefit_id: str, start: str, end: str) -> bool:
    """The 'manual overrides always win' guard: if the user already logged this
    window by hand, auto-matching must defer to a human review rather than
    silently adding a second, possibly-conflicting redemption on top."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM redemptions"
            " WHERE benefit_id=? AND date>=? AND date<=? AND source!='plaid'",
            (benefit_id, start, end),
        ).fetchone()
        return row["n"] > 0


def add_plaid_redemption(benefit_id: str, amount: float, when: str, period: str | None,
                         note: str, plaid_transaction_id: str) -> int:
    """Same shape as add_redemption, but tagged source='plaid' and linked back
    to the transaction that produced it, so a later undo/re-match is a lookup,
    not a guess."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO redemptions"
            " (benefit_id, date, amount, source, period, note, created_at, plaid_transaction_id)"
            " VALUES (?,?,?,'plaid',?,?,?,?)",
            (benefit_id, when, float(amount), period, note, date.today().isoformat(), plaid_transaction_id),
        )
        return cur.lastrowid
