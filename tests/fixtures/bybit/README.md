# Bybit v5 fixtures

Captured on Windows from **https://api.bybit.com**, without API keys,
on **2026-09-28 at 22:28:25–22:28:30 UTC** (2026-09-29, Europe/Moscow).
The additional T002a instrument fixtures below were captured separately.
All JSON files except `rate_limit_10006.json` are real HTTP 200 responses.
Only JSON formatting and the list lengths explicitly described below were changed;
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
- Klines and funding arrive newest first. Funding uses the oldest timestamp
  minus one millisecond. Since T002a, klines traverse consecutive time windows
  of at most `limit` candle slots, including empty windows. Neither a short
  nor an empty page proves that older history is absent for a delisted symbol.
  Dropping an unclosed candle also must not stop pagination.
- Instrument responses include `lotSizeFilter.minNotionalValue` as a
  string (possibly empty for historical instruments) and `fundingInterval`
  as an integer number of minutes. An empty notional is stored as null.
- Test execution is offline; fixture capture is a separate manual step.

Official references: [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate),
[klines](https://bybit-exchange.github.io/docs/v5/market/kline),
[instruments](https://bybit-exchange.github.io/docs/v5/market/instrument),
[error codes](https://bybit-exchange.github.io/docs/v5/error).

Windows command results and limitations are recorded in
[`T001a-bybit-live-validation.md`](../../../docs/tasks/T001a-bybit-live-validation.md).

## T002a status fixtures

Captured from the same public mainnet on Windows, 2026-09-28 around 23:13 UTC.
Query parameters: `category=linear&limit=1000`, plus the status below.
All envelopes, field values and cursors are unchanged; only lists are trimmed.

| Fixture | Status query | Kept rows |
|---|---|---|
| `instruments_closed_page1.json` | Closed | 10000000AIDOGEUSDT, 1000000VINUUSDT and all 5 PendingOpen, from 1000 |
| `instruments_closed_page2.json` | Closed, cursor from page 1 | Complete final response: ZRCUSDT |
| `instruments_prelaunch_page1.json` | PreLaunch | First of 6 |
| `instruments_pendingopen_page1.json` | PendingOpen | Complete empty response |
| `instruments_delivering_page1.json` | Delivering | Complete empty response |

The Closed cursor is `first%3D10000000AIDOGEUSDT%26last%3DZKJUSDT`.
The actual row status must be preserved: the Closed query also returned
PendingOpen, while the PendingOpen query itself was empty. Tests exercise
pagination per status and the empty `minNotionalValue` in 1000000VINUUSDT.
Overlap/conflict and repeated-cursor cases are synthetic modifications of
recorded rows in tests, explicitly separate from the saved fixtures.

## T002a delisted kline windows

Captured on Windows on 2026-09-28 around 23:29 UTC, mainnet, no credentials.
Both requests use `/v5/market/kline`, `category=linear&symbol=FTTUSDT&interval=D&limit=1000`.

| Fixture | start | end | Kept rows |
|---|---:|---:|---|
| `kline_ftt_closed_empty_recent.json` | 1704240000000 | 1790639999999 | Complete empty response, retCode=0 |
| `kline_ftt_closed_older.json` | 1617840000000 | 1704239999999 | Newest 3 and oldest 2 of 398 rows |

The second window returned 2021-10-12 through 2022-11-13 despite the first
window being empty. The regression test reuses the empty envelope for the
earliest window before listing. Candle values and envelope fields are unchanged.

## T006a: Demo

`demo-public.json` записан 2026-09-29 с `https://api-demo.bybit.com` без ключей:
GET `/v5/market/time` и GET `/v5/market/instruments-info?category=linear&symbol=BTCUSDT`.
Сохранены реальные публичные ответы; параметры количества/цены проверяются тестом.
Приватные сценарии в `tests/demo_support.py` — явно синтетическая биржа на
`httpx.MockTransport`: записи позиций, заявок, стопов и исполнений меняются после
запросов. Это не выдаётся за запись реального Demo-счёта. Подпись проверена также
фиксированным значением независимого `.NET HMACSHA256`.
