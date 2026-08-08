#!/bin/bash
# Daily Plaid sync. Run by launchd (see scripts/launchd/), not by hand.
#
# Talks to the already-running local server rather than importing the app,
# so there's exactly one process owning the SQLite file at a time — two
# writers to the same DB is how you corrupt it.
set -uo pipefail

PORT="${PORT:-8420}"
BASE="http://127.0.0.1:${PORT}"
LOG_TAG="[cc-benefits-sync $(date '+%Y-%m-%d %H:%M:%S')]"

# The server may still be starting (e.g. right after a reboot). Give it a
# reasonable window before giving up rather than failing the whole run.
for _ in $(seq 1 30); do
  if curl -sf "${BASE}/api/state" -o /dev/null; then break; fi
  sleep 2
done

if ! curl -sf "${BASE}/api/state" -o /dev/null; then
  echo "${LOG_TAG} server not reachable at ${BASE} — skipping"
  exit 0        # not an error worth alerting on; next run will retry
fi

cards=$(curl -sf "${BASE}/api/state" | python3 -c "
import json,sys
print(' '.join(p['card_id'] for p in json.load(sys.stdin).get('plaid_items', [])))
")

if [ -z "${cards}" ]; then
  echo "${LOG_TAG} no cards linked to Plaid — nothing to sync"
  exit 0
fi

for card in ${cards}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/api/plaid/sync/${card}")
  echo "${LOG_TAG} ${card} -> HTTP ${code}"
done

# Surface anything the matcher wasn't confident about; the review queue is
# the only part of this that wants a human.
pending=$(curl -sf "${BASE}/api/state" | python3 -c "
import json,sys; print(len(json.load(sys.stdin).get('review_queue', [])))
")
echo "${LOG_TAG} done · ${pending} transaction(s) awaiting review"
