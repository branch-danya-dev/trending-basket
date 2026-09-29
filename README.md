# trending-basket

Личный фоновый торговый бот для линейных USDT-перпетуалов Bybit. Горизонт решений — 4 часа и 1 день, позиции держатся от дней до недель.

**Статус: в разработке. Бот не торгует реальными деньгами.** Режим `live` запрещён кодом до отдельной задачи (см. `docs/ROADMAP.md`).

## Быстрый старт

### Windows (PowerShell)

```powershell
uv sync
uv run tb --help
uv run tb doctor
uv run pytest
```

### Linux

```bash
uv sync
uv run tb --help
uv run tb doctor
uv run pytest
```

## Вселенная символов

Из корня репозитория, одинаково на Windows и Linux:

```text
uv run tb data sync instruments
uv run tb universe candidates --out data/universe/candidates.txt
uv run tb data sync klines --symbols-file data/universe/candidates.txt --interval 1d --since 2021-01-01
uv run tb universe build --name core15 --since 2021-01-01
uv run tb universe report core15
uv run tb universe show core15 --at 2024-03-01
```

По умолчанию — 15 монет, минимум 120 записанных дневных свечей, медианный
оборот за полные предыдущие 30 дней от 10 млн. Параметры: `--top`,
`--min-history-days`, `--turnover-window`, `--min-turnover`.
Пересборка первого числа в 00:00 UTC; `--since` — тоже первое число месяца.
Стейблкоины и дополнительные исключения задаются в
`config/universe_exclusions.csv`; другой файл можно передать через
`--exclusions-file` в `candidates` и `build`.

Результат: `data/universe/core15.parquet` и `core15.meta.json`. В metadata
сохраняются параметры и происхождение входных данных. Нехватка символов
помечается по месяцам, отсутствующие кэши перечисляются отдельно.
Снимки, сделанные до T002a, нужно переснять для включения закрытых инструментов.
Вселенную старого формата нужно пересобрать (schema_version=2).

Пул включает `Trading` и `Closed` с одинаковыми фильтрами активов. Снимок
сохраняет также остальные статусы API. `listed_until_ms` берётся из
`deliveryTime`; при его отсутствии у Closed — из закрытия последней дневной
свечи, как оценка. У Trading конец не ограничен. Источник хранится в
`listed_until_source`, начало — в `listed_from_ms` (первая доступная свеча).
Все границы доступны в metadata `trading_periods`, а для выбранных символов
продублированы в колонках parquet.

**Ограничение:** API может не возвращать часть инструментов или их историю,
а классификация активов остаётся текущей. Объект `survivorship_bias` содержит
`delisted_included` (закрытые кандидаты с историей), `delisted_without_history`
и пояснение. Отдельно перечислены символы без истории и выбранные Closed;
`report` показывает их участие по месяцам. Устранение ошибки выжившего не гарантируется.

Для T003:

```python
from pathlib import Path
from trending_basket.universe.storage import load_universe

universe = load_universe(Path("data"), "core15")
symbols = universe.universe_at(1709251200000)  # 2024-03-01 00:00 UTC
can_trade = universe.is_tradeable_at("BTCUSDT", 1709251200000)
```

Возвращается последний состав не позже указанного времени, в порядке ранга.
До первой пересборки и в пустом месяце — пустой список.
`is_tradeable_at` проверяет полуинтервал `[первая свеча, listed_until_ms)`;
для неизвестного символа или отсутствующей истории возвращает False.
Это проверка границ листинга, а не гарантии наличия свечи или ликвидности
в любой момент внутри диапазона. T003 должен проверять её и между
месячными пересборками, закрывая позицию при делистинге и запрещая новые входы.
Известная сейчас будущая дата закрытия не влияет на более ранний отбор.

Основной период оценки T003/T005 начинается 2022-03-01 UTC. Период
2021-01-01–2022-02-28 показывается отдельно; данные доступны для прогрева
индикаторов (ADR-011). После T002a полная корзина начинается уже в ноябре 2021,
но граница основной оценки сохранена по решению владельца.

## Документация

- [`CLAUDE.md`](CLAUDE.md) — постоянные правила проекта для агента.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — архитектура и поток данных.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — дорожная карта.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — журнал решений (ADR).
