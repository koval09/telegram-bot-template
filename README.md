# telegram-bot-template

Универсальный шаблонный Telegram-бот с модульной архитектурой на aiogram 3, SQLAlchemy 2 (async), Redis и TON Connect. Один репозиторий, семь этапов подключения: ядро (MVP) → админ-панель → TON Connect → UX/защита → рост (рефералы/рассылки/статистика) → платежи (Stars + TON) → прод-эксплуатация. Каждая опциональная подсистема включается флагом `FEATURE_*` без правок ядра.

[![CI](https://img.shields.io/badge/CI-pending-lightgrey)](.github/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-pending-lightgrey)](#тестирование)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](#лицензия)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

> Бейджи — заглушки; реальные URL подставляются после первого релиза и публикации workflow.

---

## Содержание

- [Быстрый старт (docker compose)](#быстрый-старт-docker-compose)
- [Переменные окружения по этапам](#переменные-окружения-по-этапам)
- [Запуск миграций](#запуск-миграций)
- [Переключение polling ↔ webhook](#переключение-polling--webhook)
- [Добавление нового модуля](#добавление-нового-модуля)
- [Тестирование](#тестирование)
- [Структура проекта](#структура-проекта)
- [Дальнейшее чтение](#дальнейшее-чтение)
- [Лицензия](#лицензия)

---

## Быстрый старт (docker compose)

Минимум для запуска MVP — токен от [@BotFather](https://t.me/BotFather) и (опционально) Telegram ID администратора.

```bash
cp .env.example .env
# Заполнить как минимум:
#   BOT_TOKEN=123456:ABC...
#   SUPERADMIN_IDS=123456789      # ваш Telegram ID; через запятую можно несколько

docker compose --profile dev up -d --build
docker compose logs -f bot
```

В режиме `dev` бот работает в long-polling, порт `8080` пробрасывается на хост — health-check доступен по адресу:

```bash
curl http://localhost:8080/healthz
```

### Профили compose

| Профиль | Что поднимается | Когда использовать |
|---|---|---|
| `dev`   | `bot` (polling, порт 8080 наружу) + `postgres` + `redis` | Локальная разработка, отладка |
| `prod`  | `bot-prod` (webhook, порты не публикуются) + `postgres` + `redis` | Продакшн за reverse-proxy (Caddy/Nginx) |
| `tools` | `migrate` (одноразовые задачи Alembic) | Миграции, ad-hoc операции |

```bash
docker compose --profile dev   up -d                          # локальный стенд
docker compose --profile prod  up -d                          # прод (читает .env.prod)
docker compose --profile tools run --rm migrate               # alembic upgrade head
```

В `prod`-профиле порт бота **не** публикуется — TLS-терминация и фильтрация по `WEBHOOK_SECRET` лежат на reverse-proxy. Готовые сниппеты Caddy и Nginx находятся в комментариях в конце [`docker-compose.yml`](docker-compose.yml).

### Локальный запуск без Docker

```bash
python -m venv .venv
. .venv/Scripts/Activate.ps1     # Windows PowerShell
# source .venv/bin/activate      # Linux / macOS
pip install -e ".[dev]"

cp .env.example .env
# Для локалки удобно: DB_DSN=sqlite+aiosqlite:///./bot.db, REDIS_URL=redis://localhost:6379/0
alembic upgrade head
python -m app
```

---

## Переменные окружения по этапам

Полный список с комментариями — в [`.env.example`](.env.example). Этапы соответствуют разделу «Поэтапное развёртывание» из [`design.md`](.kiro/specs/telegram-bot-template/design.md): каждый следующий этап добавляет переменные, ранее настроенные не трогаются. Кросс-полевые проверки выполняются `Settings._validate_stages` в [`app/config.py`](app/config.py) и валятся **fail-fast** при старте.

### Stage 1 — MVP-ядро (обязательно)

| Переменная | Обязательность | Описание / валидация |
|---|---|---|
| `BOT_TOKEN` | **обяз.** | Токен от @BotFather. `SecretStr`, в логи не попадает. |
| `DB_DSN` | **обяз.** | DSN SQLAlchemy: `postgresql+asyncpg://...` (прод) или `sqlite+aiosqlite:///...` (локально). |
| `REDIS_URL` | **обяз.** | DSN Redis для FSM-storage и кэшей. |
| `TG_MODE` | опц. | `polling` (default) или `webhook`. При `webhook` обязательны `WEBHOOK_PUBLIC_URL` и `WEBHOOK_SECRET`. |
| `HTTP_HOST` | опц. | Адрес aiohttp-сервера; default `0.0.0.0`. |
| `HTTP_PORT` | опц. | Порт aiohttp-сервера; default `8080` (1..65535). |
| `WEBHOOK_PUBLIC_URL` | условно | Публичный HTTPS URL бота. Обязателен при `TG_MODE=webhook` и при `FEATURE_TON_CONNECTOR=true`. |
| `WEBHOOK_SECRET` | условно | Секретный сегмент пути webhook. Обязателен при `TG_MODE=webhook`. |
| `FSM_TIMEOUT_MINUTES` | опц. | Таймаут неактивности FSM-диалога; default `30` (1..1440). |
| `DEFAULT_LOCALE` | опц. | Локаль до включения i18n и fallback после; default `ru`. |
| `LOG_LEVEL` | опц. | structlog уровень; default `INFO`. |

### Stage 2 — Админ-панель

| Переменная | Обязательность | Описание |
|---|---|---|
| `SUPERADMIN_IDS` | опц. | Список Telegram ID через запятую. Сидируется в БД при старте; роль `superadmin` нельзя выдать командами бота — только через эту переменную. |

### Stage 3 — TON Connect (`FEATURE_TON_CONNECTOR=true`)

| Переменная | Обязательность | Описание |
|---|---|---|
| `FEATURE_TON_CONNECTOR` | опц. | `true`/`false`; default `false`. |
| `TON_MANIFEST_URL` | **обяз.** при флаге | Публичный HTTPS URL `tonconnect-manifest.json`. |
| `TON_APP_URL` | **обяз.** при флаге | URL приложения (отображается кошельком). |
| `TON_APP_NAME` | **обяз.** при флаге | Имя приложения (1..50 символов). |
| `TON_APP_ICON_URL` | **обяз.** при флаге | HTTPS URL иконки (PNG/ICO, рекомендованный размер 180×180). |
| `TON_APP_TERMS_URL` | опц. | Ссылка на пользовательское соглашение. |
| `TON_APP_PRIVACY_URL` | опц. | Ссылка на политику конфиденциальности. |
| `WEBHOOK_PUBLIC_URL` | **обяз.** при флаге | Нужен для отдачи манифеста и TON-callback'ов через aiohttp-сервер. |

> При включённом TON Connect рекомендуется `TG_MODE=webhook` (Telegram требует HTTPS для совместного reverse-proxy).

### Stage 4 — UX и защита

| Переменная | Обязательность | Описание |
|---|---|---|
| `FEATURE_I18N` | опц. | Включает middleware и команды смены языка. |
| `FEATURE_ANTISPAM` | опц. | Включает rate-limit и капчу для новых пользователей. |
| `FEATURE_SUBSCRIPTIONS` | опц. | Включает обязательные подписки на каналы. |
| `SUPPORTED_LOCALES` | опц. | Список локалей через запятую; default `ru,en`. `DEFAULT_LOCALE` обязан в нём присутствовать. |
| `REQUIRED_CHANNELS` | условно | Список каналов через запятую (`@channel1,@channel2`). Обязателен при `FEATURE_SUBSCRIPTIONS=true`. Бот должен быть админом каждого канала. |

### Stage 5 — Рост (рефералы, рассылки, статистика)

| Переменная | Обязательность | Описание |
|---|---|---|
| `FEATURE_REFERRALS` | опц. | Включает `/ref`, `/referrals`, учёт `referrer_id`. |
| `FEATURE_BROADCASTS` | опц. | Включает producer + worker массовых рассылок (Redis-очередь, токен-бакет 30/с). |
| `FEATURE_STATS` | опц. | Включает админскую команду `/stats`. |

### Stage 6 — Платежи (Stars и/или TON)

| Переменная | Обязательность | Описание |
|---|---|---|
| `FEATURE_PAYMENTS` | опц. | Включает router платежей. |
| `PAYMENTS_PROVIDER` | **обяз.** при флаге | `stars` \| `ton` \| `both`. |
| `TON_RECEIVE_ADDRESS` | условно | TON-адрес получателя. Обязателен при `PAYMENTS_PROVIDER ∈ {ton, both}`. |
| `TON_API_URL` | условно | Базовый URL TonCenter/TonAPI (по умолчанию `https://toncenter.com`). Обязателен при провайдере с TON. |
| `TON_API_KEY` | опц. | API-ключ TonCenter (повышает rate-limit). |

### Сводка: какие флаги что включают

| Этап | Минимально новых env |
|---|---|
| 1 MVP | `BOT_TOKEN`, `DB_DSN`, `REDIS_URL` |
| 2 Админ | `+ SUPERADMIN_IDS` |
| 3 TON Connect | `+ FEATURE_TON_CONNECTOR=true`, `TON_MANIFEST_URL`, `TON_APP_URL`, `TON_APP_NAME`, `TON_APP_ICON_URL`, `WEBHOOK_PUBLIC_URL` |
| 4 UX | `+ FEATURE_I18N`, `FEATURE_ANTISPAM`, `FEATURE_SUBSCRIPTIONS`, `REQUIRED_CHANNELS` |
| 5 Рост | `+ FEATURE_REFERRALS`, `FEATURE_BROADCASTS`, `FEATURE_STATS` |
| 6 Монетизация | `+ FEATURE_PAYMENTS`, `PAYMENTS_PROVIDER`, при TON — `TON_RECEIVE_ADDRESS`, `TON_API_URL` |

---

## Запуск миграций

Alembic читает DSN из переменной окружения `DB_DSN`; конфиг — [`alembic.ini`](alembic.ini), сценарии — [`migrations/`](migrations).

### Через docker compose (профиль `tools`)

```bash
# Применить все миграции (default command — alembic upgrade head)
docker compose --profile tools run --rm migrate

# Откатить одну миграцию
docker compose --profile tools run --rm migrate downgrade -1

# Сгенерировать новую миграцию по diff'у моделей
docker compose --profile tools run --rm migrate revision --autogenerate -m "add_my_table"
```

`migrate` использует тот же runtime-образ, что и бот, и читает `.env`, поэтому подключение совпадает один-в-один.

### Локально

```bash
alembic upgrade head           # применить все
alembic downgrade -1           # откатить одну
alembic downgrade base         # откатить все
alembic revision --autogenerate -m "your_change"
alembic history                # история ревизий
```

CI прогоняет `upgrade head → downgrade base → upgrade head` на временной Postgres-инстанции — это проверяет обратимость каждой миграции (см. `.github/workflows/ci.yml` после Stage 7).

---

## Переключение polling ↔ webhook

Режим транспорта определяется единственной переменной `TG_MODE`. Кросс-полевая валидация падает на старте, если в режиме `webhook` не заданы публичный URL и секрет.

| Режим | `TG_MODE` | `WEBHOOK_PUBLIC_URL` | `WEBHOOK_SECRET` | Когда |
|---|---|---|---|---|
| Polling | `polling` (default) | — | — | Локальная разработка, простой VPS без публичного TLS |
| Webhook | `webhook` | `https://bot.example.com` | случайная строка ≥ 32 символов | Прод, обязателен HTTPS reverse-proxy |

В webhook-режиме aiohttp-сервер слушает `POST /tg/webhook/<WEBHOOK_SECRET>`. Перед контейнером ставьте Caddy (auto-TLS) или Nginx — готовые сниппеты лежат в комментариях [`docker-compose.yml`](docker-compose.yml). Telegram сам устанавливает webhook на старте бота, отзывает при остановке.

Полезные команды:

```bash
# Сгенерировать webhook secret
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Проверить health-check (через reverse-proxy /healthz публиковать НЕ нужно)
docker compose --profile prod exec bot-prod curl -fsS http://localhost:8080/healthz
```

При смене режима достаточно перезапустить сервис — миграции, состояние FSM в Redis и привязки кошельков не зависят от транспорта.

---

## Добавление нового модуля

Архитектура держится на четырёх точках расширения. Соглашение: модуль не трогает ядро (`app/core/*`), регистрируется условно по флагу.

1. **Создать пакет в `app/features/<имя>/`.** Минимум — `__init__.py` с экспортом «бандла» (dataclass с зависимостями) и `router` / middleware. Структуру удобно копировать с `app/features/i18n/` или `app/features/stats/`.

2. **Добавить флаг в [`app/config.py`](app/config.py).** Поле в `Settings`:
   ```python
   feature_my_module: bool = False
   my_module_threshold: int = Field(default=10, ge=1)
   ```
   Если у фичи есть обязательные параметры — добавьте проверку в `_validate_stages` (`@model_validator(mode="after")`), чтобы при включении флага без параметров приложение падало на старте.

3. **Опциональный bundle в [`app/container.py`](app/container.py).** В `AppServices` добавьте поле `my_module: MyModuleBundle | None = None`. В `build_services()` инстанцируйте только если `settings.feature_my_module is True` — иначе оставьте `None`.

4. **Conditional include в `register_routers` / `register_middlewares`** ([`app/bot.py`](app/bot.py)):
   ```python
   if services.my_module is not None:
       from app.features.my_module import my_module_router
       dispatcher.include_router(my_module_router)
       dispatcher["my_module"] = services.my_module
   ```

5. **Локализация** — добавьте ключи в [`app/locales/ru.yml`](app/locales/ru.yml) и [`app/locales/en.yml`](app/locales/en.yml). При включённом `FEATURE_I18N` ключи будут разрешаться через middleware; при выключенном — через `DEFAULT_LOCALE`.

6. **Документация** — обновите `.env.example` с новыми переменными и добавьте строку в раздел [«Переменные окружения по этапам»](#переменные-окружения-по-этапам) этого README.

7. **Тесты** — unit-тесты в `tests/unit/`, при необходимости integration в `tests/integration/`. Маркируйте `pytest.mark.unit` / `integration` / `functional` / `contract`.

---

## Тестирование

Стек: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`) + `pytest-cov` + `testcontainers[postgres,redis]`. Маркеры объявлены в [`pyproject.toml`](pyproject.toml).

| Маркер | Назначение | Внешние зависимости |
|---|---|---|
| `unit` | Быстрые тесты сервисов и логики на фейках | — |
| `integration` | Репозитории, миграции, цепочки middleware на реальных Postgres/Redis | Docker (для testcontainers) |
| `functional` | Сценарии хендлеров на mock `Bot` (`/start → ban → 24h ignore`, `/connect_wallet`, `/broadcast`) | — |
| `contract` | Внешние API: TON Connect, TonCenter | Сетевые моки или живые контракты |

### Запуск

```bash
pytest -m unit                      # быстрые тесты
pytest -m integration               # требует запущенный Docker
pytest -m functional
pytest -m contract                  # дольше всего, обычно отдельным CI-job
pytest -m "not contract"            # всё, кроме contract — типичный «зелёный прогон»
pytest                              # всё подряд
pytest --cov=app --cov-report=term-missing
```

Testcontainers сами поднимают Postgres и Redis в контейнерах per-session — нужен только установленный и доступный Docker daemon (на Windows — Docker Desktop).

### Линтеры и типы

```bash
ruff check app tests                # стиль + базовые ошибки
ruff check --fix app tests          # автофикс
mypy app                            # строгая типизация (strict = true)
```

Конфигурация ruff и mypy — в [`pyproject.toml`](pyproject.toml). Игноры там же снабжены комментариями с обоснованием.

---

## Структура проекта

```
.
├── app/
│   ├── __main__.py            точка входа: build_services → start polling/webhook
│   ├── bot.py                 фабрики Bot / Dispatcher; register_middlewares + register_routers
│   ├── web.py                 aiohttp: /healthz, webhook, TON Connect manifest и callback
│   ├── config.py              Settings (pydantic-settings), fail-fast cross-field валидация
│   ├── container.py           AppServices — простой DI-контейнер, бандлы по фичам
│   ├── core/
│   │   ├── db/                engine, Base, ORM-модели (users, action_log, payments, broadcasts)
│   │   ├── repositories/      UsersRepo, AuditRepo, PaymentsRepo, BroadcastsRepo
│   │   ├── cache/             Redis-клиент и хелперы
│   │   ├── middlewares/       RegistrationMiddleware, StatusGateMiddleware
│   │   ├── handlers/          /start, /profile, /cancel
│   │   ├── services/          AuditLog, RegistrationService, retry, FSM storage
│   │   ├── healthcheck.py     /healthz: статус БД и Redis
│   │   └── utils/             clock, structlog настройка
│   ├── admin/                 Stage 2: Authorization, UserManager, handlers, IsAdminFilter
│   ├── ton/                   Stage 3: TON Connect connector, manifest, verifier, handlers
│   ├── features/
│   │   ├── i18n/              Stage 4: middleware + YAML loader
│   │   ├── antispam/          Stage 4: rate-limit + капча
│   │   ├── subscriptions/     Stage 4: проверка подписок на каналы
│   │   ├── referrals/         Stage 5: deep-link, /ref, /referrals
│   │   ├── broadcasts/        Stage 5: producer (admin FSM) + worker (Redis-очередь)
│   │   ├── stats/             Stage 5: /stats для админа
│   │   └── payments/          Stage 6: Telegram Stars + TON
│   ├── scheduler/             APScheduler-jobs (cleanup сессий TON, polling TON-платежей, audit retention)
│   └── locales/               YAML-переводы (ru.yml, en.yml)
├── migrations/                Alembic versions/
├── tests/
│   ├── unit/                  быстрые тесты на фейках
│   ├── integration/           testcontainers (Postgres + Redis)
│   ├── functional/            сценарии на mock Bot
│   └── contract/              внешние API (TON Connect, TonCenter)
├── .github/workflows/         CI: lint, test, migrations, опционально docker publish
├── alembic.ini                конфиг Alembic
├── docker-compose.yml         профили dev / prod / tools
├── Dockerfile                 multi-stage; runtime python:3.11-slim, non-root user
├── pyproject.toml             зависимости, ruff, mypy, pytest
├── tonconnect-manifest.json.example   шаблон манифеста TON Connect
├── .env.example               все переменные с разбивкой по этапам
└── README.md                  этот файл
```

---

## Дальнейшее чтение

- [`.kiro/specs/telegram-bot-template/requirements.md`](.kiro/specs/telegram-bot-template/requirements.md) — функциональные и нефункциональные требования (формат EARS).
- [`.kiro/specs/telegram-bot-template/design.md`](.kiro/specs/telegram-bot-template/design.md) — технический дизайн: модель данных, middleware-цепочки, поэтапное развёртывание, стратегия тестирования.
- [`.kiro/specs/telegram-bot-template/tasks.md`](.kiro/specs/telegram-bot-template/tasks.md) — план реализации с трассировкой к требованиям.

---

## Лицензия

[MIT](LICENSE) © telegram-bot-template. Полный текст — в `pyproject.toml` (`license = { text = "MIT" }`); файл `LICENSE` создаётся при первом релизе.
