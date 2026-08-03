# CC Benefits Tracker

A local-first tracker that answers one question per card: **am I getting more
value than I pay in annual fees?** Runs entirely on your Mac. No financial data
leaves the machine.

Replaces Kudos. The difference: Kudos shows *available* value (the big number
that makes every card look worth keeping). This shows **available / realistic /
captured** side by side, and computes the keep/cancel verdict on *realistic* —
the value you'd actually get, per your own judgment.

## Status

**Phase 1, 4, and 5 complete.** Phase 2 (Plaid auto-sync) is code-complete and
tested against a mocked Plaid client — it needs one thing only you can
provide: free Plaid **sandbox** API keys (see "Connecting a card via Plaid"
below) to actually run the Link flow. Reminders (Phase 3) are scoped but not
built.

### Connecting a card via Plaid (Phase 2)

1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com) (free,
   sandbox needs no business verification) and copy your `client_id` and
   `sandbox` secret.
2. `cp .env.example .env`, fill in `PLAID_CLIENT_ID` and `PLAID_SECRET`,
   leave `PLAID_ENV=sandbox`.
3. Restart the server. Open a card's tab, scroll to **Bank sync**, click
   **Connect via Plaid**. In Plaid's sandbox Link flow, search for any
   institution (e.g. "Platypus Bank") and log in with `user_good` /
   `pass_good`.
4. The app runs an immediate sync after linking — high-confidence matches
   (a `plaid_auto` benefit's merchant pattern + an amount that fits the
   period's cap + a date inside that period) auto-create a redemption.
   Everything else lands in the **Needs review** banner on Overview for you
   to confirm or reject.
5. Only benefits with `tracking_mode: plaid_auto` in the catalog are ever
   matched — that flag already encodes which credits genuinely post as a
   normal statement line Plaid can see, versus landing in an app wallet or
   requiring a booking channel Plaid has no visibility into.
6. Manual entries always win: if you've already logged a period by hand,
   auto-matching defers to the review queue instead of silently adding a
   second entry on top.

## Cards tracked

| Card | Annual cost | Verdict basis |
|------|------------:|---------------|
| Amex Platinum | $895 | credits |
| Amex Gold | $325 | credits |
| Chase Sapphire Reserve | $795 (incl. $195 AU fee) | credits |
| Bilt Palladium | $495 | points (Phase 4) |

CSR and Bilt show **PENDING** until Phase 4 — their value is mostly points, and
a credits-only verdict would be confidently wrong. That's deliberate; see the
`value_thesis` notes in `config/catalog.yaml`.

## Setup

Needs Python 3.8+ (`/usr/bin/python3` on macOS is 3.9 and works; the Anaconda
3.7 on this machine does not).

```bash
cd "path/to/CC Benefits Tracker"
/usr/bin/python3 -m venv ~/.venvs/cc-benefits-tracker   # venv OUTSIDE Google Drive
~/.venvs/cc-benefits-tracker/bin/pip install -r requirements.txt
~/.venvs/cc-benefits-tracker/bin/uvicorn app.main:app --port 8420
```

Open http://127.0.0.1:8420. Phase 1 needs no `.env` and no secrets.

### Where your data lives

- **Config** (`config/*.yaml`) — card terms + your judgments. Lives in the
  project folder (Google Drive). Not sensitive; benefits from syncing.
- **Database** — `~/Library/Application Support/cc-benefits-tracker/benefits.db`.
  **Deliberately outside Google Drive:** it holds your redemption history (and,
  from Phase 2, encrypted Plaid tokens). The app refuses to start if
  `DATABASE_PATH` points into any cloud-synced folder.

## Using it

- **Redeem** — enter an amount and click redeem. Partial amounts are the norm
  (used $10 of a $20 credit). Defaults to the full remaining amount.
- **Useful / N/A** — undecided benefits (yellow) don't count until you judge
  them. Mark Equinox N/A and it drops out of the realistic total and the verdict.
- **set** — override the realistic annual value for any applicable benefit.
- **Bilt Cash ledger** — earn/burn tracking, since it's a currency, not a credit.
  Warns you when spending it on something another card already covers.
- **Colors** — red < 10 days left, orange 10–30, neutral otherwise.

Manual entries always win; Phase 2 auto-detection will never overwrite them.

## Editing the benefit catalog

Two files, on purpose:

- `config/catalog.yaml` — objective card terms. Edit when an **issuer** changes
  something. Every dollar figure cites a source; each card has a
  `terms_last_verified` date. Re-verify periodically — card terms change
  constantly.
- `config/preferences.yaml` — your personal state (enrolled / applicable /
  realistic value). Edit when **you** change your mind. Read the two rules at the
  top before setting a `realistic_value`: *capture is not realization* and
  *intention is not evidence*.

YAML seeds the database once, on first run. After that the database is
authoritative for personal state, so editing YAML won't clobber toggles you've
changed in the UI. To re-seed from scratch, delete the database file.

### Known flags in the catalog (things to verify yourself)

- **CSR $300 travel credit anchor** — modeled as account-anniversary
  (Chase's docs); you recalled calendar halves. Check your Chase app.
- **Bilt figures** — biltrewards.com blocks automated fetch; corroborated from
  your Kudos dashboard but confirm against your card terms.
- **Bilt $100/mo hotel credit** — may be a Bilt Cash *channel*, not an
  independent credit. If so it's double-counted. Ask Bilt support.

## Digest (Phase 5 seam, already live)

```bash
curl http://127.0.0.1:8420/digest
```

Returns JSON: credits expiring within 30 days (unused amount + deadline) and a
per-card ROI snapshot. This is the seam your weekly automation reads. This app
owns credit-card benefits and card ROI **only** — subscription cancellation
stays in your Renewal & Benefit Sweep.

## Tests

```bash
~/.venvs/cc-benefits-tracker/bin/python -m pytest tests/ -q
```

Covers the period/reset engine (`test_periods.py`), the ROI and verdict math
(`test_roi.py`), and the HTTP write paths (`test_api.py`).

## Layout

```
config/
  catalog.yaml        card terms (objective) — cite sources, has verified dates
  preferences.yaml    your judgments (enrolled/applicable/realistic + seed data)
app/
  main.py             FastAPI routes; binds loopback only
  catalog.py          loads + validates YAML
  db.py               SQLite; refuses cloud-synced DB paths
  periods.py          reset windows, valid_until proration, urgency colors
  roi.py              three-tier math, keep/cancel verdict, Bilt tier cliff
  templates/dashboard.html
tests/
.env.example          Phase 2+ config (Plaid, encryption). Copy to .env.
```
