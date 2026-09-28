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

## Документация

- [`CLAUDE.md`](CLAUDE.md) — постоянные правила проекта для агента.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — архитектура и поток данных.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — дорожная карта.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — журнал решений (ADR).
