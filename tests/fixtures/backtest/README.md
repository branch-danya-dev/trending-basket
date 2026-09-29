# T004 exchange regression fixtures

Frozen synthetic engine outputs from unmodified T004 code (728793d / 95eaddc),
recorded before the T004b engine changes. These are offline artificial OHLC/funding
series, not exchange responses. The deterministic input builder is in
`tests/test_execution_quantity.py`. Both V1 and the BTC benchmark cover the
complete fills, positions, equity and events records; do not regenerate from
new code to make a regression pass. Real V1–V4 hashes remain in T004-runs.json.
