# UX Redesign Plan — Phase 1.5

Interactive prototype: published as a Claude artifact (clickable). This doc is the
implementation reference. Nothing here changes the data model or ROI math from
Phase 1 — it's a presentation-layer reorganization plus one genuinely new
interaction (the month grid).

## Problem with the current build

- One long vertical scroll: ~50 rows, four cards stacked. Bilt is ~3 screens down.
- No wayfinding — can't jump to a card or see all verdicts at a glance.
- Monthly credits show only the current month + a yearly total. You can't see or
  fix *which* months you missed.
- The expiring-soon banner names what's due but you scroll to hunt for the row to act.
- No visible redemption history; logging is a full-page 303 redirect.
- Every benefit competes for attention equally — no hierarchy.

## Target IA: hub + focus

Replace the stack with a **sticky top card-nav**. Views:

1. **Overview (default)** — **no portfolio grand total.** A blended figure across
   four cards isn't actionable; keep-or-cancel is decided one card at a time. Show
   "Expiring within 30 days" (derived from live state, not a static list) → 2×2 grid
   of card summaries, each with its own 3-tier bar, verdict, and gap
   ("$793 left to capture this year"). Click a card to focus.
   - **Expiring items are launchers, not shortcuts.** Tapping one navigates to that
     card, opens that credit, scrolls it into view and highlights it — the month grid
     is where logging happens. No inline redeem: a one-click redeem on the overview
     assumes the full amount was used, which defeats partial/per-month tracking.
   - Items leave the expiring list automatically once the current period is logged.
2. **Card focus** — one card only. Benefits grouped: *Needs attention* (expiring
   or undecided) → *On track* → *Done & not applicable* (collapsed). Left severity
   stripe encodes urgency in form, not just color.
3. **Bilt Cash tab** — it's a currency, not a credit; own tab with balance,
   over-$100 year-end expiry warning, channel breakdown, live overlap nudges.

Each tab carries its verdict chip (KEEP / PENDING / INCOMPLETE / CANCEL) and an
attention badge count.

## Tab structure

One tab per *relationship*, not per data type:

- Overview · Platinum · Gold · CSR · **Bilt**
- **Bilt Cash lives inside the Bilt tab**, not as its own tab — it's the same card
  and the same $495 fee. The tab shows three bars: a combined total at top
  (card credits + Bilt Cash), then card credits, then Bilt Cash (Earned YTD /
  Redeemed / Balance, with the over-$100 year-end expiry warning). The Overview's
  Bilt card shows the combined figure.
- Tab badges are computed (undecided + expiring), not hardcoded, so they self-update.

## The period grid (headline feature)

Every periodic credit expands to a grid of its *actual* windows — cadence decides
the shape:

| Cadence | Cells | Labels |
|---|---|---|
| monthly | 12 | Jan … Dec |
| quarterly | 4 | Q1 · Jan–Mar … Q4 · Oct–Dec |
| **semiannual** | **2** | **H1 · Jan–Jun / H2 · Jul–Dec** |

Semiannual credits must NOT be shown as one yearly number — an unused H1 is
forfeited money and has to read as a missed window. Live examples: Platinum's
$600 FHR/Hotel Collection credit (H1 2026 forfeited, H2 open), CSR's $300 dining
and $300 StubHub credits ($150/half), Gold's $100 Resy ($50/half), Bilt's $400
semiannual hotel credit ($200/half).

Detail on the grid behavior (identical for all three shapes):

- **Green** = captured that period · **red** = missed & past · **outlined** = open
  current period · **faint** = future.
- Click an open/missed cell to log that period. Partial amounts supported —
  each cell stores its own amount; the row header sums actuals
  ("$87 of $300 · 4 of 7 months used"). Do NOT assume captured = full cap.
- Data model already supports this: `redemptions` are dated, and
  `db.redeemed_in_period()` queries any window. The grid is a per-period query
  loop over existing data — no schema change.

## Implementation notes

- **Inline updates, no reload.** Month-grid logging means many clicks in a row;
  move redeem / toggle / month-log to HTMX or a small fetch layer that patches the
  row and re-sums in place. Keep the scroll position.
- **Grouping/sort** happens in `roi.build` output or the template — urgency and
  `applicable is None` drive the group. Already computed in `BenefitView`.
- **Verdict-unblock CTA.** Surface "N decisions needed to unlock CSR's verdict" on
  the Overview, linking into that card's undecided group.
- **Reference benefits & notes** — the `notes` field (currently hidden) surfaces in
  the expanded panel; reference/insurance benefits stay in a collapsed section.
- **Theme** — both light/dark already token-based; carry the same token approach.

## Grouped credits → bucket × period matrix

When several capped credits compete for the *same* period budget, one row that
expands into a **matrix** (buckets down, periods across) beats both one blended
row and N separate rows:

- **DoorDash** — one row "DoorDash Credits · monthly · 3 buckets · $25/mo",
  expanding to a 3 × 12 grid (Restaurant $5, Non-food #1 $10, Non-food #2 $10)
  with a YTD column per bucket. Each cell logs independently.
- Reading **down a column** = what one month captured. Reading **across a row** =
  a habit. This is what makes "Non-food #2 is never captured" visible at a glance;
  it is invisible in three separate rows and impossible in one blended row.
- Below the matrix, each bucket keeps its own line with its own
  Useful / N/A decision — grouped for scanning, still separate for logging.

Same component serves **Bilt Cash monthly channels** (Grubhub / Lyft / Travel
Portal / Other × 12 months). There the matrix quantifies the overlap warning:
"$100 of Bilt Cash went to channels another card already pays for."

Bilt Cash `redeemed` and `balance` are **derived** from the channel logs, never
stored separately — so logging a burn moves the balance, the Bilt combined total,
and the Overview in one pass.

## Every periodic credit is drillable

If a credit resets on a period, it must expose that period's grid — including
Bilt Cash channels, which are monthly-capped and non-rolling like any credit.

## Decision changes re-file the row immediately

Marking a credit **Not applicable** moves it to *Done & not applicable* on the
spot (and back on undo). Group by the **live decision** (`appl === false`), not
the static seed flag — the original implementation checked the seed data and so
left N/A'd credits sitting in "Needs attention".

## Catalog completeness (caught during prototyping)

The prototype originally showed a token sample of CSR. The real card has **15
credits totalling exactly $3,468**, and the UI must render all of them —
including the three *separate* DoorDash buckets that Chase markets as one
"$25/month":

- DoorDash Restaurant **$5/mo** · Non-Food #1 **$10/mo** · Non-Food #2 **$10/mo**

They cannot substitute for each other, so they are three rows with three grids.
Non-Food #2 additionally requires a *second distinct* order in the same month —
realistically the hardest credit in the portfolio to capture.

Sanity check to keep: the sum of every benefit's annual value per card should
reconcile to that card's `available` total. CSR reconciles at $3,468.

## Deliberate non-goals for 1.5

- No new benefits, no ROI logic changes.
- Bilt/CSR stay PENDING — points verdict is still Phase 4.
- Not a mobile app; desktop-first local dashboard (responsive grid collapse is enough).

## Build order suggestion

1. Card-nav + view routing (biggest friction relief, low risk).
2. Card focus grouping + severity stripes.
3. Month-grid expansion (the new interaction) + inline logging.
4. Overview act-now inline redeem + verdict-unblock CTA.
5. Bilt Cash tab with live overlap warnings.
