# T002a — закрытые инструменты и проверка истории

Проверено на Windows 2026-09-29 по Москве; снимок API получен 2026-09-28 UTC.
Источник: публичный `https://api.bybit.com`, без API-ключей.
База: T002 из [PR #4](https://github.com/branch-danya-dev/trending-basket/pull/4),
смёрженного коммитом `c75e07d` после CI на Windows и Ubuntu и разрешения владельца.
Изменения T002a: [PR #5](https://github.com/branch-danya-dev/trending-basket/pull/5).
Коммит проверенной реализации: `4f086da872b750c02789fe835f794f7d2250e7f5`.

## Снимок и пул

| Статус | Инструментов |
|---|---:|
| Closed | 996 |
| PendingOpen | 5 |
| PreLaunch | 6 |
| Trading | 886 |

Всего 1893 уникальных инструмента. Пять запросов: Trading, Closed, PreLaunch,
PendingOpen и Delivering, каждый со своей пагинацией. Closed вернул 1001 строку:
996 Closed и 5 PendingOpen; собственный запрос PendingOpen был пустым.
Delivering также пуст. Фактический статус сохраняется, фильтр запроса
не используется как значение строки.

В исходных ответах у всех 996 Closed заполнены symbol, contractType, status, baseCoin, quoteCoin,
launchTime, положительный deliveryTime, priceScale, priceFilter, lotSizeFilter.
fundingInterval положителен у 323 (у истёкших фьючерсов он нулевой);
fullName непустой у 928, symbolType — innovation у 87, пустой у 909.
marketRegion и underlyingTicker пусты у всех. minNotionalValue пуст у 143 Closed
и 2 PendingOpen; парсер сохраняет null вместо выдуманного нулевого ограничения.
[Документация Bybit](https://bybit-exchange.github.io/docs/v5/market/instrument)
определяет deliveryTime как время делистинга перпетуала.

Из 306 Closed LinearPerpetual USDT исключены BUSD, FDUSD и исторический UST как стейблкоины.
UST добавлен в CSV наряду с USTC: прежнее имя TerraUSD тоже должно исключаться.
Его классификация как стейблкоина подтверждается [отчётом Bybit/Nansen за март 2022](https://assets.contentstack.io/v3/assets/bltd582a520b3ab6888/blt6123b861b9611d18/624ea3581da0ac09296dfc7b/Bybit_X_Nansen_State_of_the_Industry_Report_March_2022.pdf).
Пул: **817**, из них **303 Closed** и 514 Trading.

## Реальная история и исправление пагинации

Прежний клиент останавливался на короткой или пустой странице. Для Closed
это неверно: FTTUSDT с конечной датой в сентябре 2026 вернул пустой ответ,
но запрос до 2024-01-02 вернул 398 свечей. LUNAUSDT аналогично вернул 214.
Неполная страница MATIC/EOS также скрывала ранние свечи. Теперь весь диапазон
проходится последовательными окнами максимум по 1000 интервалов, включая пустые.
Первая проверка со старой пагинацией не используется для вывода о доступности данных.

Окончательный проход с 2021-01-01: история у **302** Closed;
пусто у **1**, API-ошибка у **0**, отсутствующих файлов **0**.
Пустые: LAYERUSDT.
Ошибки: нет.
Для LAYERUSDT проверен также точный интервал из снимка:
2025-01-24 05:01–09:05 UTC; API вернул retCode=0 и пустой список дневных свечей.
Повторный проход добавил 45 270 свечей в 120 кэшей. Отдельная проверка перед
ним восстановила ещё 398 FTT, 214 LUNA и по 918 MATIC/EOS.

Пять Closed с наибольшим **суммарным** оборотом USDT по загруженной истории
(это описание данных, не изменение месячного ранжирования по медиане):

| Символ | Свечей | Первая, UTC | Последняя, UTC | deliveryTime, UTC | Оборот, млрд USDT |
|---|---:|---|---|---|---:|
| MATICUSDT | 1166 | 2021-06-29 | 2024-09-06 | 2024-09-06T10:00:00+00:00 | 98.859 |
| FTMUSDT | 1206 | 2021-09-23 | 2025-01-10 | 2025-01-10T10:00:00+00:00 | 84.762 |
| TONUSDT | 1020 | 2023-08-31 | 2026-06-15 | 2026-06-15T09:00:00+00:00 | 56.975 |
| EOSUSDT | 1423 | 2021-06-29 | 2025-05-21 | 2025-05-21T09:00:00+00:00 | 35.338 |
| LUNAUSDT | 214 | 2021-10-11 | 2022-05-12 | 2022-05-12T10:03:00+00:00 | 33.193 |

## Границы и остаточные ограничения

У всех Closed-кандидатов положительный deliveryTime, поэтому реальный источник
`listed_until_ms` — `snapshot_delivery_time`. Запасной `last_candle_close` проверен
синтетически. У Trading — null (`open`). Нет истории — нет начала торговли,
поэтому `is_tradeable_at` возвращает False даже при известном deliveryTime.
Первая свеча включается в диапазон; точный момент закрытия уже исключён.
Для выбранных символов границы и источник — колонки parquet; для всех кандидатов
они находятся в metadata.trading_periods. Схема 2 требует пересборки старых вселенных.

`is_tradeable_at` не гарантирует ликвидность или наличие свечи внутри диапазона.
T003 должен отдельно определить цену и исполнение принудительного выхода, в том
числе при закрытии посреди дневной свечи; здесь торгового исполнения нет.

Метаданные survivorship_bias:

```json
{
  "delisted_included": 302,
  "delisted_without_history": 1,
  "note": "Closed symbols with available history are included, but survivorship bias remains: the API may omit instruments or their candle history, and asset classification is current. A last-candle close fallback estimates, rather than proves, the delisting time. Historical results may still overstate performance."
}
```

`delisted_included` означает кандидатов с непустой историей, а не число выбранных
хотя бы раз. Последнее приведено ниже. История может быть неполной; API может
не перечислять часть инструментов вообще. Классификация активов текущая,
повторные листинги одним именем и торговые перерывы единым диапазоном не восстановлены.

Первая свеча позже дня launchTime (с учётом отсечения 2021-01-01) у 25 Closed:

| Символ | launchTime | Первая свеча | Разница, дней |
|---|---|---|---:|
| 10000000AIDOGEUSDT | 2024-03-04T09:01:39+00:00 | 2024-03-05 | 1 |
| AIUSDT | 2024-01-16T11:51:09+00:00 | 2024-01-17 | 1 |
| ANTUSDT | 2021-12-16T16:42:19+00:00 | 2021-12-17 | 1 |
| CTKUSDT | 2021-12-02T11:34:07+00:00 | 2021-12-03 | 1 |
| DARKUSDT | 2025-04-25T15:34:50+00:00 | 2025-04-27 | 2 |
| DENTUSDT | 2022-01-13T10:15:28+00:00 | 2022-01-14 | 1 |
| DODOUSDT | 2022-04-20T12:30:00+00:00 | 2022-04-22 | 2 |
| FETUSDT | 2023-02-07T08:50:51+00:00 | 2023-02-09 | 2 |
| GPTUSDT | 2023-03-16T08:13:58+00:00 | 2023-03-17 | 1 |
| HIGHUSDT | 2023-02-07T08:42:50+00:00 | 2023-02-17 | 10 |
| HOOKUSDT | 2023-02-07T08:25:52+00:00 | 2023-02-13 | 6 |
| MAJORUSDT | 2024-11-18T09:18:02+00:00 | 2024-11-19 | 1 |
| MATICUSDT | 2021-01-15T00:00:00+00:00 | 2021-06-29 | 165 |
| MBOXUSDT | 2024-01-18T10:45:43+00:00 | 2024-01-19 | 1 |
| MEMEFIUSDT | 2024-11-12T12:38:51+00:00 | 2024-11-13 | 1 |
| MYROUSDT | 2024-01-18T13:39:51+00:00 | 2024-01-19 | 1 |
| ORBSUSDT | 2023-10-09T07:45:42+00:00 | 2023-10-13 | 4 |
| RNDRUSDT | 2021-11-30T15:23:33+00:00 | 2021-12-01 | 1 |
| SCRUSDT | 2024-10-08T12:25:58+00:00 | 2024-10-10 | 2 |
| SCUSDT | 2021-11-30T15:25:58+00:00 | 2021-12-01 | 1 |
| STPTUSDT | 2023-10-03T09:46:17+00:00 | 2023-10-04 | 1 |
| SWEATUSDT | 2022-11-03T15:10:53+00:00 | 2022-11-04 | 1 |
| TOMOUSDT | 2022-04-27T23:50:52+00:00 | 2022-04-28 | 1 |
| ZEUSUSDT | 2024-04-08T06:50:11+00:00 | 2024-04-09 | 1 |
| ZKFUSDT | 2024-01-16T11:25:59+00:00 | 2024-01-17 | 1 |

У всех 302 Closed с историей последняя свеча доходит до дня завершения торговли.
Свечей, открывшихся в момент deliveryTime или позже, не обнаружено.
Для MATIC отдельный запрос за 2021-01-01–2021-06-28 также вернул пустой список.

Это расхождения покрытия с полями снимка, а не автоматически доказанные пропуски торгов.
Положительный data check подтверждает только отсутствие разрывов внутри имеющейся истории.

## Сравнение с T002

Те же 69 пересборок, январь 2021 — сентябрь 2026: **916 → 924** строк.
По ключу (месяц, символ) добавлено **69**, удалено **61**;
симметричная разность **130** записей в **39** месяцах.
У общих записей изменён ранг в **235** случаях (отдельно от замены членства).
Числовые показатели оборота и служебные новые колонки в этот подсчёт не входят.

Хотя бы однажды выбрано 15 Closed: AERGOUSDT, BITUSDT, BLZUSDT, EOSUSDT, FETUSDT, FTMUSDT, LINAUSDT, LUNAUSDT, MATICUSDT, REEFUSDT, RNDRUSDT, SXPUSDT, TOMOUSDT, TONUSDT, UNFIUSDT.
Месяцев с ними: 39.

| Месяц | До → после | Добавлено | Удалено | Позже Closed в составе |
|---|---:|---|---|---|
| 2021-11-01 | 13 → 15 | EOSUSDT, MATICUSDT | нет | EOSUSDT, MATICUSDT |
| 2021-12-01 | 13 → 15 | EOSUSDT, MATICUSDT | нет | EOSUSDT, MATICUSDT |
| 2022-01-01 | 13 → 15 | EOSUSDT, MATICUSDT | нет | EOSUSDT, MATICUSDT |
| 2022-02-01 | 13 → 15 | FTMUSDT, MATICUSDT | нет | FTMUSDT, MATICUSDT |
| 2022-03-01 | 15 → 15 | BITUSDT, FTMUSDT, LUNAUSDT, MATICUSDT | DOGEUSDT, DOTUSDT, LINKUSDT, LTCUSDT | BITUSDT, FTMUSDT, LUNAUSDT, MATICUSDT |
| 2022-04-01 | 15 → 15 | FTMUSDT, LUNAUSDT, MATICUSDT | AXSUSDT, DOTUSDT, LINKUSDT | FTMUSDT, LUNAUSDT, MATICUSDT |
| 2022-05-01 | 15 → 15 | FTMUSDT, LUNAUSDT, MATICUSDT | LINKUSDT, LTCUSDT, RUNEUSDT | FTMUSDT, LUNAUSDT, MATICUSDT |
| 2022-06-01 | 15 → 15 | FTMUSDT, MATICUSDT | ATOMUSDT, BNBUSDT | FTMUSDT, MATICUSDT |
| 2022-07-01 | 15 → 15 | FTMUSDT, MATICUSDT | DOTUSDT, TRXUSDT | FTMUSDT, MATICUSDT |
| 2022-08-01 | 15 → 15 | MATICUSDT | BNBUSDT | MATICUSDT |
| 2022-09-01 | 15 → 15 | MATICUSDT | DOTUSDT | MATICUSDT |
| 2022-10-01 | 15 → 15 | MATICUSDT | BNBUSDT | MATICUSDT |
| 2022-11-01 | 15 → 15 | MATICUSDT | GMTUSDT | MATICUSDT |
| 2022-12-01 | 15 → 15 | MATICUSDT | AVAXUSDT | MATICUSDT |
| 2023-01-01 | 15 → 15 | FTMUSDT, MATICUSDT | AVAXUSDT, NEARUSDT | FTMUSDT, MATICUSDT |
| 2023-02-01 | 15 → 15 | MATICUSDT | AXSUSDT | MATICUSDT |
| 2023-03-01 | 15 → 15 | FTMUSDT, MATICUSDT | ADAUSDT, NEARUSDT | FTMUSDT, MATICUSDT |
| 2023-04-01 | 15 → 15 | FTMUSDT, MATICUSDT | AVAXUSDT, DYDXUSDT | FTMUSDT, MATICUSDT |
| 2023-05-01 | 15 → 15 | FTMUSDT, MATICUSDT, SXPUSDT | ATOMUSDT, AVAXUSDT, LINKUSDT | FTMUSDT, MATICUSDT, SXPUSDT |
| 2023-06-01 | 15 → 15 | FTMUSDT, MATICUSDT, RNDRUSDT | ARPAUSDT, ATOMUSDT, AVAXUSDT | FTMUSDT, MATICUSDT, RNDRUSDT |
| 2023-07-01 | 15 → 15 | LINAUSDT, MATICUSDT, RNDRUSDT, TOMOUSDT | APEUSDT, LINKUSDT, MTLUSDT, STXUSDT | LINAUSDT, MATICUSDT, RNDRUSDT, TOMOUSDT |
| 2023-08-01 | 15 → 15 | MATICUSDT | AVAXUSDT | MATICUSDT |
| 2023-09-01 | 15 → 15 | BLZUSDT, MATICUSDT | LINKUSDT, LTCUSDT | BLZUSDT, MATICUSDT |
| 2023-10-01 | 15 → 15 | BLZUSDT, MATICUSDT, UNFIUSDT | BNBUSDT, DOGEUSDT, SUIUSDT | BLZUSDT, MATICUSDT, UNFIUSDT |
| 2023-11-01 | 15 → 15 | BLZUSDT, MATICUSDT | APTUSDT, ARBUSDT | BLZUSDT, MATICUSDT |
| 2023-12-01 | 15 → 15 | MATICUSDT | 1000BONKUSDT | MATICUSDT |
| 2024-01-01 | 15 → 15 | MATICUSDT | TRBUSDT | MATICUSDT |
| 2024-02-01 | 15 → 15 | MATICUSDT | DOGEUSDT | MATICUSDT |
| 2024-03-01 | 15 → 15 | MATICUSDT | 1000PEPEUSDT | MATICUSDT |
| 2024-04-01 | 15 → 15 | FETUSDT, FTMUSDT | 1000BONKUSDT, ARBUSDT | FETUSDT, FTMUSDT |
| 2024-05-01 | 15 → 15 | FTMUSDT, TONUSDT | LTCUSDT, WLDUSDT | FTMUSDT, TONUSDT |
| 2024-06-01 | 15 → 15 | TONUSDT | ORDIUSDT | TONUSDT |
| 2024-07-01 | 15 → 15 | TONUSDT | JASMYUSDT | TONUSDT |
| 2024-09-01 | 15 → 15 | TONUSDT | WLDUSDT | TONUSDT |
| 2024-10-01 | 15 → 15 | FTMUSDT, REEFUSDT | AVAXUSDT, NEARUSDT | FTMUSDT, REEFUSDT |
| 2024-11-01 | 15 → 15 | FTMUSDT | ENAUSDT | FTMUSDT |
| 2025-01-01 | 15 → 15 | FTMUSDT | AAVEUSDT | FTMUSDT |
| 2025-05-01 | 15 → 15 | AERGOUSDT | CRVUSDT | AERGOUSDT |
| 2026-06-01 | 15 → 15 | TONUSDT | FARTCOINUSDT | TONUSDT |

Неполные месяцы после T002a:

| Пересборка | Символов |
|---|---:|
| 2021-01-01 | 0 |
| 2021-02-01 | 0 |
| 2021-03-01 | 0 |
| 2021-04-01 | 0 |
| 2021-05-01 | 5 |
| 2021-06-01 | 5 |
| 2021-07-01 | 5 |
| 2021-08-01 | 5 |
| 2021-09-01 | 8 |
| 2021-10-01 | 11 |

После включения Closed корзина полная с **2021-11-01**, включая январь–февраль 2022.
Основная оценка T003/T005 остаётся с **2022-03-01 UTC** (ADR-011).
2021 и январь–февраль 2022 — отдельная дополнительная история с фактическим
размером корзины. Ранние свечи можно использовать для прогрева. Граница выбрана
до расчёта доходности; восстановление Closed не меняет её автоматически.

## Проверки и воспроизведение

```text
uv run tb data sync instruments
uv run tb universe candidates --out data/universe/candidates.txt
uv run tb data sync klines --symbols-file data/universe/candidates.txt --interval 1d --since 2021-01-01
uv run tb data check --symbols-file data/universe/candidates.txt --interval 1d
uv run tb universe build --name core15 --since 2021-01-01
uv run tb universe report core15
uv run tb universe show core15 --at 2022-05-01
```

Локальные журналы (игнорируются Git): reports/t002a-sync-klines-final.log,
reports/t002a-validation.json, reports/t002a-audit-summary.json.
Исходная вселенная T002 сохранена в reports/t002-baseline для сравнения.

| Проверка | Код выхода |
|---|---:|
| uv run tb data check --symbols-file data/universe/candidates.txt --interval 1d | 0 |
| uv run tb universe build --name core15 --since 2021-01-01 | 0 |
| uv run tb universe report core15 | 0 |
| uv run tb universe show core15 --at 2022-05-01 | 0 |
| uv run tb universe build --name core15 --since 2021-01-01 | 0 |

Повторная сборка дала побайтно одинаковый parquet: `7576210f2d2ba932114a42d257b468f5cc37c5c253b0f67be2abbdc023270fdd`.
На реальном артефакте проверена торгуемость каждой строки на её пересборке
и запрет торговли выбранными Closed точно в момент deliveryTime.

Локально: ruff check, ruff format --check, mypy src и **135 offline-тестов** прошли.
[CI реализации 4f086da](https://github.com/branch-danya-dev/trending-basket/actions/runs/36498530359)
успешен на Ubuntu и Windows. Финальный статус CI документационного коммита виден в PR #5.
Тесты покрывают будущий/прошлый/точный делистинг, независимость ранга от будущего
закрытия, границы is_tradeable_at, оба источника конца, отсутствие истории,
повторяемость, статусы и курсоры, пустой notional, полную страницу 1000 свечей,
стык окон, пустую и неполную страницу перед доступной историей, реальные ответы FTT.

Новых зависимостей нет. Бэктест, принудительное исполнение и автоматизация вне T002a.
