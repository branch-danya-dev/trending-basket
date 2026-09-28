# T000 — инициализация репозитория trending-basket

## Контекст

Репозиторий `https://github.com/branch-danya-dev/trending-basket` пустой. Перед началом прочитай `CLAUDE.md` в корне: его правила обязательны. Если файла ещё нет в репозитории, владелец передал его вместе с этой задачей — положи его в корень без изменений первым коммитом.

Эта задача создаёт только каркас: инструменты, структуру пакета, конфигурацию, CLI, базовые доменные типы, тесты, CI и документы. Торговой логики, сетевого кода и загрузки данных в ней нет.

## Git

Репозиторий пустой, поэтому результат этой задачи допускается закоммитить прямо в `main` несколькими осмысленными коммитами. Начиная с T001 — ветка `task/NNN-short-name` и PR в `main`.

## Что создать

### Структура

```
trending-basket/
├─ CLAUDE.md
├─ README.md
├─ pyproject.toml
├─ uv.lock
├─ .python-version            # 3.12
├─ .gitignore
├─ .env.example
├─ .github/workflows/ci.yml
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ ROADMAP.md
│  ├─ DECISIONS.md
│  └─ tasks/T000-init.md     # копия этой задачи
├─ src/trending_basket/
│  ├─ __init__.py            # __version__ = "0.1.0"
│  ├─ cli.py
│  ├─ config.py
│  ├─ clock.py
│  ├─ logging_setup.py
│  ├─ domain/
│  │  ├─ __init__.py
│  │  └─ types.py
│  ├─ data/__init__.py
│  ├─ strategies/__init__.py
│  ├─ portfolio/__init__.py
│  ├─ backtest/__init__.py
│  ├─ execution/__init__.py
│  └─ reporting/__init__.py
└─ tests/
   ├─ conftest.py
   ├─ test_smoke.py
   ├─ test_config.py
   ├─ test_clock.py
   ├─ test_domain.py
   └─ test_no_system_clock.py
```

Пустые подпакеты (`data`, `strategies`, `portfolio`, `backtest`, `execution`, `reporting`) содержат только `__init__.py` с docstring на одну-две строки о назначении слоя. Заглушки классов и функций на будущее не создавать.

### pyproject.toml

- Имя `trending-basket`, `requires-python = ">=3.12"`, сборка через `hatchling`.
- Точка входа: `tb = "trending_basket.cli:app"`.
- Зависимости runtime: только `pydantic>=2`, `pydantic-settings>=2`, `typer`. Pandas, numpy, pyarrow, httpx и прочие добавляются в задачах, где понадобятся.
- Dev-группа: `pytest`, `ruff`, `mypy`.
- Ruff: `line-length = 100`, правила `E, F, W, I, B, UP, SIM, DTZ, RUF`.
- Mypy: `strict = true` для `src/`.
- Pytest: `testpaths = ["tests"]`.

### config.py

Класс `Settings` на `pydantic-settings`: `env_prefix="TB_"`, чтение `.env`, `extra="forbid"`.

| Поле | Тип | По умолчанию |
|---|---|---|
| `mode` | `Literal["research", "paper", "demo", "live"]` | `"research"` |
| `data_dir` | `Path` | `./data` |
| `reports_dir` | `Path` | `./reports` |
| `log_level` | `Literal["DEBUG", "INFO", "WARNING", "ERROR"]` | `"INFO"` |
| `bybit_api_key` | `SecretStr \| None` | `None` |
| `bybit_api_secret` | `SecretStr \| None` | `None` |

Функция `load_settings()`:

1. Проверяет `os.environ` и `.env`: любой ключ с префиксом `TB_`, которого нет среди полей, вызывает ошибку с перечнем неизвестных ключей и подсказкой ближайшего по написанию известного.
2. Если `mode == "live"`, вызывает ошибку: «live mode is disabled until roadmap task T010».
3. Возвращает `Settings`.

Метод или функция для безопасного вывода конфигурации: секреты маскируются как `***` или `<unset>`.

### clock.py

- Протокол `Clock` с методом `now_ms() -> int`.
- `SystemClock` — единственное место в пакете, где разрешён вызов системного времени.
- `ManualClock(start_ms)` с методами `set(ms)` и `advance(ms)` для тестов и бэктеста. Время не может идти назад: попытка вызывает `ValueError`.

### domain/types.py

Frozen dataclasses (`slots=True`) с проверками в `__post_init__`:

- **`Interval`** — `StrEnum` со значениями `H4 = "4h"`, `D1 = "1d"`, методом `to_bybit()` (`"240"`, `"D"`) и свойством `duration_ms`.
- **`Candle`** — поля `symbol`, `interval`, `open_time_ms`, `open`, `high`, `low`, `close`, `volume`, `turnover`. Проверки: цены > 0; `high >= max(open, close)`; `low <= min(open, close)`; `volume >= 0`; `open_time_ms` кратно `interval.duration_ms` (для `D1` — начало суток UTC).
- **`FundingRate`** — поля `symbol`, `funding_time_ms`, `rate_frac`.
- **`Side`** — `StrEnum`: `LONG`, `SHORT`.
- **`TargetWeight`** — поля `symbol`, `weight_frac`. Проверка: `-1 <= weight_frac <= 1`.

### logging_setup.py

Настройка stdlib `logging`: уровень из `Settings`, время в UTC в формате ISO-8601, формат `time level logger message`.

### cli.py

Приложение `typer` с командами:

| Команда | Что делает |
|---|---|
| `tb version` | Печатает версию пакета |
| `tb config show` | Печатает действующую конфигурацию с замаскированными секретами |
| `tb doctor` | Проверяет версию Python ≥ 3.12, что `data_dir` и `reports_dir` создаются и доступны на запись, что режим не `live`. Печатает результат по каждой проверке, код выхода 0 или 1 |

Сетевых вызовов в CLI нет.

### Тесты

- **`conftest.py`**:
  - фикстура с `autouse`, которая запрещает сеть: подменяет `socket.socket.connect` на функцию, бросающую исключение;
  - фикстура, очищающая переменные `TB_*` на время теста.
- **`test_smoke.py`**: пакет импортируется; `tb --help` и `tb version` работают через `typer.testing.CliRunner`.
- **`test_config.py`**:
  - значения по умолчанию;
  - переопределение через `TB_*`;
  - неизвестный `TB_FOO` → ошибка с именем ключа;
  - `TB_MODE=live` → ошибка;
  - секреты замаскированы в выводе `config show`.
- **`test_clock.py`**: `ManualClock` — `set`, `advance`, запрет движения назад.
- **`test_domain.py`**: валидные и невалидные `Candle`, `Interval.to_bybit()`, `duration_ms`, проверка выравнивания `open_time_ms`, границы `TargetWeight`.
- **`test_no_system_clock.py`**: AST-проверка файлов в `domain/`, `strategies/`, `portfolio/`, `backtest/` на вызовы `time.time`, `time.monotonic`, `time.perf_counter`, `datetime.now`, `datetime.utcnow`, `date.today`, включая формы `from time import time`. Отдельный тест подаёт в проверяющую функцию синтетический фрагмент кода с запрещённым вызовом и убеждается, что он найден.

### CI (.github/workflows/ci.yml)

- Запуск на `push` и `pull_request`.
- Матрица `ubuntu-latest` и `windows-latest`, Python 3.12.
- Установка через `astral-sh/setup-uv`.
- Шаги:
  1. `uv sync --locked`
  2. `uv run ruff check .`
  3. `uv run ruff format --check .`
  4. `uv run mypy src`
  5. `uv run pytest`

### .gitignore и .env.example

`.gitignore`: `.venv/`, `__pycache__/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `data/`, `reports/`, `.env`, `*.parquet`.

`.env.example`: `TB_MODE=research`, `TB_DATA_DIR=./data`, `TB_REPORTS_DIR=./reports`, `TB_LOG_LEVEL=INFO`. Ключи API закомментированы, с пояснением: только право торговли, без вывода, с привязкой к IP.

### README.md (на русском)

1. Назначение проекта и статус: не для реальных денег.
2. Быстрый старт для Windows PowerShell и Linux:
   ```
   uv sync
   uv run tb --help
   uv run tb doctor
   uv run pytest
   ```
3. Ссылки на `CLAUDE.md` и документы в `docs/`.

### docs/ARCHITECTURE.md (на русском, коротко)

Слои и поток данных:

```
data → backtest/research → strategies (целевые веса) → portfolio (риск, размер)
     → execution (целевые позиции → разница с фактическими → заявки) → reporting
```

Принципы:

- **Один путь кода** для бэктеста, Demo и live: различаются только источник данных и исполнитель.
- **Время** только через `Clock`.
- **Издержки** (комиссии, фандинг, проскальзывание) учитываются всегда.
- **Исполнение идемпотентно:** после перезапуска бот сверяет фактические позиции с целевыми и приводит их в соответствие.
- **Стопы на стороне биржи.**

### docs/ROADMAP.md (на русском)

Перенеси список ниже. Для T000 отметь «выполнено» после мержа.

| ID | Цель | Критерий готовности |
|---|---|---|
| T000 | Каркас репозитория (эта задача) | Все проверки зелёные на Ubuntu и Windows |
| T001 | Данные: публичный REST-клиент Bybit (свечи, история фандинга, параметры инструментов) с ограничением частоты и повторами; локальный кэш parquet; `tb data sync` | Докачка идемпотентна, пропуски в истории обнаруживаются и сообщаются; тесты на фикстурах без сети |
| T002 | Вселенная символов: правило отбора на момент времени (по обороту за прошлый период), учёт делистингов, фиксация списка в конфиге эксперимента | Отбор на дату использует только данные до этой даты; тест на заглядывание вперёд |
| T003 | Бэктест-движок: целевые веса → сделки → комиссии, фандинг, проскальзывание → кривая капитала; метрики (доходность, волатильность, Sharpe, максимальная просадка, оборот, доля издержек); сравнение с buy-and-hold BTC | Ручной пример на 3 свечах сходится до цента; тест на отсутствие заглядывания вперёд |
| T004 | Трендовая корзина v0: пробой экстремумов нескольких горизонтов, размер обратно волатильности, трейлинг-стоп по ATR, ребалансировка раз в сутки | Отчёт бэктеста на периоде разработки; параметры зафиксированы в `DECISIONS.md` |
| T005 | Проверка устойчивости: разбиение разработка/валидация/отложенный период, walk-forward, плато параметров, bootstrap-интервалы, разбивка по годам и монетам | Отчёт с решением «продолжать / нет» по критериям, записанным до запуска |
| T006 | Исполнение на Bybit Demo: целевые позиции → разница → заявки, нормализация по инструменту, reconciliation, стопы на бирже, уведомления в Telegram, остановка по просадке | 2 недели на Demo без ручных вмешательств; поведение совпадает с бэктестом в пределах издержек |
| T007 | Funding carry: исследование окупаемости по истории фандинга и комиссиям спота и перпетуала, затем модуль | Отчёт с точкой окупаемости и решением |
| T008 | ML-модель волатильности: HAR как база, затем бустинг; используется для размера позиций | Лучше базы на отложенном периоде, иначе выключена |
| T009 | Развёртывание на Linux-VPS (Docker или systemd), мониторинг | Бот переживает перезапуск сервера без ручных действий |
| T010 | Малый live | Критерии записываются перед задачей |

### docs/DECISIONS.md (на русском)

Журнал решений в формате: номер, дата, решение, причина, последствия. Первые записи:

- **ADR-001.** Горизонт 4h/1d и фоновая работа. Причина: комиссии Bybit 0,02% / 0,055% делают скальпинг экономически невыгодным, а редкие решения устойчивы к сбоям сети.
- **ADR-002.** Python 3.12, uv, src-layout, кросс-платформенность.
- **ADR-003.** Время только через `Clock`; проверка тестом.
- **ADR-004.** Режим `live` запрещён кодом до T010.
- **ADR-005.** Код scalp-bot не копируется целиком. Отдельные проверенные модули (нормализация количества по инструменту, ограничение частоты REST, reconciliation) переносятся явными задачами с тестами.

## Вне рамок этой задачи

- Сетевой код, клиент Bybit, загрузка данных.
- Бэктест, стратегии, ML.
- Docker и развёртывание.
- Копирование кода из scalp-bot.
- Любая торговля.

Если кажется, что что-то из этого нужно уже сейчас, — написать об этом в отчёте, но не делать.

## Критерии готовности

1. На чистой машине Windows и Linux `uv sync` проходит; `uv run tb --help`, `tb version`, `tb config show`, `tb doctor` работают.
2. `ruff check`, `ruff format --check`, `mypy src`, `pytest` зелёные локально и в CI на обеих ОС.
3. Неизвестная переменная `TB_*` и `TB_MODE=live` приводят к понятной ошибке.
4. AST-проверка системного времени ловит синтетический запрещённый вызов.
5. Тесты не обращаются к сети.
6. Отчёт по задаче оформлен по разделу «Отчёт по задаче» из `CLAUDE.md`.
