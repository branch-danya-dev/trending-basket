# Bybit v5 fixtures

Captured on Windows from **https://api.bybit.com**, without API keys,
on **2026-09-28 at 22:28:25–22:28:30 UTC** (2026-09-29, Europe/Moscow).
All JSON files except `rate_limit_10006.json` are real HTTP 200 responses.
Only JSON formatting and the instrument list lengths were changed;
field names, types, values, timestamps and cursor tokens are preserved.

## Requests and trimming

All market-data requests use `category=linear`; klines and funding use
`symbol=BTCUSDT`. Times below are milliseconds since the Unix epoch.

| Fixture | GET endpoint | Other query parameters | Kept rows |
|---|---|---|---|
| `server_time.json` | `/v5/market/time` | none | Complete response |
| `server_time_for_unclosed_4h.json` | `/v5/market/time` | none; captured immediately before the 4h candles | Complete response |
| `kline_1d_page1_recent.json` | `/v5/market/kline` | `interval=D&start=1704067200000&end=1704499199999&limit=3` | All 3 |
| `kline_1d_page2_older.json` | `/v5/market/kline` | `interval=D&start=1704067200000&end=1704239999999&limit=3` | All 2 |
| `kline_4h_with_unclosed_last.json` | `/v5/market/kline` | `interval=240&start=1790568000000&end=1790634506000&limit=3` | All 3, including the open candle at `1790625600000` |
| `kline_4h_page2_older.json` | `/v5/market/kline` | `interval=240&start=1790568000000&end=1790596799999&limit=3` | All 2 |
| `funding_page1_recent.json` | `/v5/market/funding/history` | `startTime=1704067200000&endTime=1704182400000&limit=3` | All 3 |
| `funding_page2_older.json` | `/v5/market/funding/history` | `startTime=1704067200000&endTime=1704124799999&limit=3` | All 2 |
| `non_retryable_error.json` | `/v5/market/funding/history` | `startTime=1704067200000`, no `endTime` | Complete response: `retCode=10001` |
| `instruments_page1_cursor.json` | `/v5/market/instruments-info` | `limit=500` | BTCUSDT and ETHUSDT from 500 rows |
| `instruments_page2_final.json` | `/v5/market/instruments-info` | `limit=500`, `cursor` from the first response | SOLUSDT from 386 rows; final cursor is empty |

Pass the cursor string `first%3D0GUSDT%26last%3DMONUSDT` to the HTTP
client as a query parameter without decoding it. Its encoded wire value
is `first%253D0GUSDT%2526last%253DMONUSDT`.

The 1d and funding samples cover January 2024. The 4h sample contains
one candle that was still open at the recorded server time. Tests replay
that time instead of consulting the current clock. Pagination tests also
split these same recorded 4h rows into single-row pages to check an
all-unclosed first page; no market values are invented.

## Synthetic rate-limit response

`rate_limit_10006.json` is the original synthetic fixture from T001,
dated 2026-09-28. A real rate-limit response was not observed during
normal requests, and traffic was not increased to trigger one. It remains
explicitly synthetic for deterministic retry tests. The client retries
HTTP 429/5xx, transport errors and `retCode=10006`. WebSocket-only codes
are outside this public REST client; no additional REST rate-limit code
was observed in this capture.

## Verified API details

- Funding uses **`GET /v5/market/funding/history`**. Supplying only
  `startTime` returned `10001` (`params error: Time Is Invalid`).
- Klines and funding arrive newest first; the next page uses the oldest
  raw timestamp minus one millisecond. Count raw rows before discarding
  an unclosed candle, otherwise a full page can appear incomplete.
- Instrument responses include `lotSizeFilter.minNotionalValue` as a
  string and `fundingInterval` as an integer number of minutes.
- Test execution is offline; fixture capture is a separate manual step.

Official references: [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate),
[klines](https://bybit-exchange.github.io/docs/v5/market/kline),
[instruments](https://bybit-exchange.github.io/docs/v5/market/instrument),
[error codes](https://bybit-exchange.github.io/docs/v5/error).

Windows command results and limitations are recorded in
[`T001a-bybit-live-validation.md`](../../../docs/tasks/T001a-bybit-live-validation.md).
