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
Снимки, сделанные до T002, нужно переснять для получения классификации активов.

**Ограничение:** исторический отбор использует сегодняшних кандидатов;
делистингованные символы исключены (`survivorship_bias=true`). Отбор закрытых
свечей без заглядывания вперёд не устраняет эту ошибку выжившего.

Для T003:

```python
from pathlib import Path
from trending_basket.universe.storage import load_universe

universe = load_universe(Path("data"), "core15")
symbols = universe.universe_at(1709251200000)  # 2024-03-01 00:00 UTC
```

Возвращается последний состав не позже указанного времени, в порядке ранга.
До первой пересборки и в пустом месяце — пустой список.

## Документация

- [`CLAUDE.md`](CLAUDE.md) — постоянные правила проекта для агента.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — архитектура и поток данных.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — дорожная карта.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — журнал решений (ADR).
