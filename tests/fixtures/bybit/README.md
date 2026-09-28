# Bybit v5 fixtures — reconstructed, not captured

**2026-09-28:** this environment's network egress proxy blocks both
`bybit-exchange.github.io` (the docs site) and `api.bybit.com` (the API
itself) — verified with `curl`, not assumed. These JSON files were
**not captured from live responses**. They were reconstructed from
training-time knowledge of the Bybit v5 API shape (endpoint paths,
field names, envelope structure, pagination fields), trimmed to a few
rows each with plausible-looking BTCUSDT/ETHUSDT/SOLUSDT values.

They are internally consistent (correct interval alignment, correct
ascending/descending order, correct pagination cursors) so the client
and cache logic can be tested against them, but **do not treat the
exact field set, types, or edge-case behavior as verified against the
real API.** See the T001 task report for the specific points that need
checking, in particular:

- `GET /v5/market/funding-history` — the task doc assumed
  `/v5/market/funding/history`; reconstructed here as
  `/v5/market/funding-history`, which is what training-time knowledge
  and a (blocked) doc search both point to. **Verify this first** —
  everything else follows from whether this path is right.
- Whether `/v5/market/funding-history` behaves differently when only
  `startTime` is given (no `endTime`) — not verified.
- Whether `retCode`s other than `10006` also mean "rate limited" —
  not verified; the client currently retries only on `10006`.
- Exact `instruments-info` field set (e.g. presence/spelling of
  `minNotionalValue`) — not verified.

**Action for the owner:** re-run `tb data sync ...` against the real
API once this repo is somewhere with network access, capture the raw
responses, and replace these files (keep them trimmed to a few rows).
