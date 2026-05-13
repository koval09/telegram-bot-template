# План реализации: Шаблонный Telegram-бот (telegram-bot-template)

## Обзор

Задачи сгруппированы по этапам из Приложения А требований (Stage 1 MVP → Stage 7 Эксплуатация) и опираются на архитектурные решения из `design.md`. Каждый следующий этап строится поверх предыдущего, ядро остаётся неизменным — новые модули подключаются условно в `app/bot.py` по флагам `feature_*` из Конфигурации.

Язык реализации: Python 3.11+. Стек фиксирован дизайном: aiogram 3, aiohttp, SQLAlchemy 2 async + Alembic, Redis, pytonconnect, pydantic-settings, APScheduler, structlog.

Тестирование: дизайн не содержит секции «Correctness Properties», поэтому используются unit-тесты (pytest), integration-тесты (testcontainers: Postgres + Redis), functional-сценарии (моки `Bot` / `aiogram_tests`) и contract-тесты внешних API (TON Connect, TonCenter/TonAPI). Тестовые подзадачи помечены `*` и могут быть пропущены для ускорения MVP.

## Задачи

### Stage 1 — Bootstrap + MVP-ядро

- [x] 1. Инициализация проекта и каталогов
  - [x] 1.1 Создать `pyproject.toml` и зафиксировать зависимости
    - Указать `requires-python = ">=3.11"`, PEP 621 метаданные, секцию `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`
    - Добавить зависимости: `aiogram>=3.4`, `aiohttp`, `sqlalchemy[asyncio]>=2.0`, `alembic`, `asyncpg`, `aiosqlite`, `redis>=5`, `pydantic-settings>=2`, `structlog`, `apscheduler`, `pytonconnect`, `pytoniq-core`, `pyyaml`, `python-dateutil`
    - Dev-зависимости: `pytest`, `pytest-asyncio`, `pytest-cov`, `testcontainers[postgres,redis]`, `aiogram-tests` (или аналог), `ruff`, `mypy`, `types-PyYAML`
    - _Требования: 15.4, 16.1_
  - [x] 1.2 Создать скелет каталогов `app/`
    - Каталоги `app/core/{db,repositories,cache,middlewares,handlers,services,utils}`, `app/admin`, `app/ton`, `app/features/{i18n,antispam,subscriptions,referrals,broadcasts,stats,payments}`, `app/scheduler`, `app/locales`, `migrations`, `tests`
    - В каждой директории создать пустой `__init__.py`
    - _Требования: 15.1, 16.2_
  - [x] 1.3 Создать `.env.example` с секциями по этапам
    - Секции: `# Stage 1 (required)`, `# Stage 2`, `# Stage 3 (TON)`, `# Stage 4 (UX)`, `# Stage 5 (Growth)`, `# Stage 6 (Payments)`
    - Stage 1: `BOT_TOKEN=`, `DB_DSN=`, `REDIS_URL=`, `TG_MODE=polling`, `HTTP_HOST=0.0.0.0`, `HTTP_PORT=8080`
    - Добавить `.gitignore` с `.env`, `__pycache__/`, `.venv/`, `*.db`
    - _Требования: 15.3, 15.4, 16.4_
  - [x] 1.4 Создать `Dockerfile` и `docker-compose.yml`
    - `Dockerfile` multi-stage: builder (pip install) + runtime (python:3.11-slim, non-root user, `ENTRYPOINT ["python","-m","app"]`)
    - `docker-compose.yml` с сервисами `bot`, `postgres:15`, `redis:7-alpine`, healthcheck для БД и Redis, `depends_on` с `condition: service_healthy`
    - Volumes для postgres/redis, env_file: `.env`
    - _Требования: 16.1, 17.3_

- [x] 2. Конфигурация и входная точка
  - [x] 2.1 Реализовать `app/config.py` (Settings на pydantic-settings)
    - Поля согласно `design.md` § «Конфигурация»: обязательные `bot_token/db_dsn/redis_url`, `tg_mode`, `webhook_*`, `superadmin_ids`, `feature_*`, параметры модулей (`default_locale`, `supported_locales`, `required_channels`, `ton_*`, `payments_provider`, `fsm_timeout_minutes`)
    - `model_validator(mode="after")`: если `feature_ton_connector=True` → требовать `ton_manifest_url` и `webhook_public_url`; если `tg_mode=webhook` → требовать `webhook_public_url` и `webhook_secret`; если `feature_payments=True` и `payments_provider in ("ton","both")` → требовать `ton_receive_address` и `ton_api_url`
    - Загрузка из `.env` через `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")`
    - _Требования: 15.1, 15.3, 15.4, 16.4_
  - [ ]* 2.2 Unit-тесты `Settings` в `tests/unit/test_config.py`
    - Проверить fail-fast при отсутствии `bot_token`/`db_dsn`/`redis_url`
    - Проверить cross-field валидации для `feature_ton_connector`, `tg_mode=webhook`, `feature_payments`
    - _Требования: 15.3, 16.4_
  - [x] 2.3 Реализовать `app/container.py` с `AppServices`
    - Определить `@dataclass AppServices`: `settings`, `db`, `redis`, `users_repo`, `audit_repo`, `payments_repo`, `broadcasts_repo`, `audit`, `authorization`, `user_manager`, `ton`, `i18n`, `antispam`, `subscriptions`, `referrals`, `broadcasts`, `stats`, `payments`, `scheduler`, `bot`, `dispatcher`
    - Функция-фабрика `async def build_services(settings) -> AppServices` инстанцирует только включённые флагами модули, остальные `= None`
    - _Требования: 15.2, 16.2_
  - [x] 2.4 Реализовать `app/__main__.py` (точка входа)
    - Загрузить `Settings`, настроить `structlog` JSON → stdout, собрать `AppServices`, запустить веб-приложение и диспетчер (polling или webhook в зависимости от `tg_mode`)
    - Перехватить `SIGTERM`/`SIGINT` и вызвать graceful shutdown (см. задачу 5.3)
    - _Требования: 15.2, 16.1, 17.5_

- [x] 3. Слой данных: движок, модели, миграции
  - [x] 3.1 Реализовать `app/core/db/engine.py` и `app/core/db/base.py`
    - `engine.py`: `create_async_engine(dsn, pool_pre_ping=True, pool_size=10)` + `async_sessionmaker(expire_on_commit=False)` + `async def db_ping() -> bool` с таймаутом 1с
    - `base.py`: `class Base(DeclarativeBase)` + `MappedAsDataclass` mixin (по желанию) + общие `TimestampMixin`
    - _Требования: 15.3, 17.3, 17.4_
  - [x] 3.2 Определить модели SQLAlchemy в `app/core/db/models.py`
    - `User(telegram_id PK, username, first_name, last_name, language_code, status enum, role enum, created_at, last_seen_at, banned_*, muted_*, referrer_id FK, referrals_count, last_referral_at, ton_address unique-partial, ton_wallet_name, ton_connected_at, is_blocked_bot)` — enum-типы `UserStatus = {active,banned,muted,pending_captcha}`, `UserRole = {user,admin,superadmin}`
    - `ActionLog(id BIGSERIAL PK, created_at, level enum, actor_id, target_id, action, source, reason VARCHAR(500), message VARCHAR(1000), trace_id)` — индексы `created_at DESC`, `level`, `actor_id`, `target_id`
    - `Payment(id, user_id FK, provider enum, status enum, amount, currency, purpose, tx_hash_or_charge_id, payload_id UUID unique, created_at, paid_at, expires_at)` + `UniqueConstraint(provider, tx_hash_or_charge_id)` partial
    - `Broadcast(id, created_by FK, created_at, started_at, finished_at, status enum, filter_kind, filter_value, text TEXT, total, delivered, failed, blocked)`
    - Все индексы из секции «Модели данных» дизайна
    - _Требования: 1.6, 5.1, 6.1, 6.2, 8, 9, 13.5_
  - [x] 3.3 Настроить Alembic и создать initial-миграцию
    - Создать `alembic.ini` + `migrations/env.py` с async-конфигурацией (`AsyncEngine`, `run_sync`)
    - Сгенерировать `migrations/versions/0001_initial.py`: все enum-типы, таблицы, индексы, unique constraints из 3.2
    - Добавить команду в README: `alembic upgrade head`
    - _Требования: 1.6, 6.1, 13.5, 16.4_
  - [ ]* 3.4 Integration-тест миграции в `tests/integration/test_migrations.py`
    - Через testcontainers Postgres: `alembic upgrade head` → проверить существование таблиц/индексов/enum-типов → `alembic downgrade base` → проверить откат
    - _Требования: 1.6, 6.1_

- [x] 4. Redis-клиент, retry-хелпер, репозитории
  - [x] 4.1 Реализовать `app/core/cache/redis_client.py`
    - `async def create_redis(settings) -> Redis` — `redis.asyncio.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=1.0)`
    - `async def redis_ping(client) -> bool` с таймаутом 1с (для health-check)
    - Хелперы `set_nx_with_ttl`, `sliding_window_incr` (INCR+EXPIRE) для антиспама
    - _Требования: 11.4, 14.1, 17.3, 17.4_
  - [x] 4.2 Реализовать `app/core/services/retry.py`
    - `async def with_retry(fn, *, attempts=3, delays=(1,2,4), retry_on=(...))` — логирует каждую попытку через `structlog`
    - Отдельная ветка для `TelegramRetryAfter` (ждать `retry_after` секунд, не считать как failed попытку)
    - Специализации: `telegram_retry()`, `db_retry()` как фасады с предустановленными exceptions
    - _Требования: 1.4, 6.6, 8 расширенный, 17.1, 17.2_
  - [ ]* 4.3 Unit-тесты `with_retry` в `tests/unit/test_retry.py`
    - Успех с первой попытки; успех после N ретраев; проваленные N попыток; `TelegramRetryAfter` ждёт указанное время
    - _Требования: 1.4, 17.1_
  - [x] 4.4 Реализовать `app/core/repositories/users.py` (`UsersRepo`)
    - Методы согласно `Protocol` из design: `get_by_tg_id`, `upsert_from_tg` (INSERT … ON CONFLICT DO UPDATE только изменённых полей), `update_last_seen`, `set_status`, `set_wallet`, `clear_wallet`, `set_referrer`, `increment_referrals`, `mark_blocked_bot`, `count_active_since`, `iterate_for_broadcast`
    - Все операции через `async with session.begin()`
    - _Требования: 1.1, 1.2, 1.3, 1.6, 1.7, 2.1, 3.2, 3.4, 5.1, 5.3, 5.5, 5.7, 7.2, 8.3, 9.1, 9.3_
  - [x] 4.5 Реализовать `app/core/repositories/audit.py` (`AuditRepo`) и `payments.py`, `broadcasts.py`
    - `AuditRepo`: `insert(record)`, `list_page(page, page_size=50)` с `COUNT(*) OVER ()`, `delete_older_than(cutoff)`
    - `PaymentsRepo`: `create_pending`, `mark_paid`, `mark_expired`, `mark_mismatch`, `find_pending_older_than`, `exists_by_charge_id`
    - `BroadcastsRepo`: `create_running`, `update_counters`, `finish`, `cancel`
    - _Требования: 6.1, 6.3, 6.4, 8.4, 13.3, 13.4, 13.5_
  - [ ]* 4.6 Integration-тесты репозиториев в `tests/integration/test_repos.py`
    - Testcontainers Postgres; проверить `upsert_from_tg` идемпотентность + partial update; `count_active_since` на индексе `last_seen_at`; уникальность `telegram_id`; уникальность `(provider, tx_hash_or_charge_id)`
    - _Требования: 1.2, 1.3, 1.6, 9.1, 9.3, 13.5_

- [x] 5. Веб-слой, health-check, graceful shutdown
  - [x] 5.1 Реализовать `app/web.py` (aiohttp-приложение)
    - `build_web_app(services) -> web.Application`; маршруты регистрируются условно: `/healthz` всегда, `/tg/webhook/{secret}` если `tg_mode=webhook`, `/tonconnect-manifest.json` и `/tonconnect/callback` если `feature_ton_connector`
    - Настройка `aiogram.webhook.aiohttp_server.setup_application`
    - _Требования: 16.1, 16.2, 17.3_
  - [x] 5.2 Реализовать `app/core/healthcheck.py`
    - Хендлер `async def healthz(request)`: параллельные `asyncio.wait_for(db_ping, 1.0)` и `asyncio.wait_for(redis_ping, 1.0)` через `asyncio.gather(return_exceptions=True)`
    - Ответ JSON `{"db": "available|unavailable", "cache": "available|unavailable"}`, статус 200 или 503; общий бюджет ≤ 2с
    - _Требования: 17.3, 17.4_
  - [ ]* 5.3 Integration-тест health-check в `tests/integration/test_healthz.py`
    - Testcontainers Postgres+Redis; успешный случай → 200; остановка Redis → `cache:unavailable` + 503
    - _Требования: 17.3, 17.4_
  - [x] 5.4 Логирование версий при старте и graceful shutdown в `app/__main__.py`
    - До `start_polling`/`setup_application`: `log.info("startup.versions", ...)` + `audit.record_info(event="startup", details={...})` с версиями `aiogram`, `sqlalchemy`, `redis`, `pytonconnect` (если включён)
    - Обработчик `SIGTERM`/`SIGINT`: снять webhook / остановить polling → `asyncio.wait_for(gather(pending_handlers), timeout=10)` → `scheduler.shutdown(wait=True)` → остановить `BroadcastWorker` → закрыть pool БД и Redis
    - _Требования: 17.5_

- [x] 6. Bot/Dispatcher, middleware-конвейер, базовые хендлеры
  - [x] 6.1 Реализовать `app/bot.py` (фабрика Bot/Dispatcher)
    - `def build_bot(settings) -> Bot` с `parse_mode=ParseMode.HTML`
    - `def build_dispatcher(services) -> Dispatcher`: `RedisStorage.from_url(settings.redis_url)` для FSM
    - Регистрация middleware в порядке: `RegistrationMiddleware` → `StatusGateMiddleware` → `AntispamMiddleware` (если `feature_antispam`) → `I18nMiddleware` (если `feature_i18n`) → `SubscriptionsMiddleware` (если `feature_subscriptions`) → handler
    - Условная регистрация Router'ов по флагам: `core_router` всегда, `admin_router` всегда (админка — ядро), `ton_router` при `feature_ton_connector`, `referrals_router`/`broadcasts_router`/`stats_router`/`payments_router` по своим флагам
    - _Требования: 15.1, 15.2, 16.2, 16.3_
  - [x] 6.2 Реализовать `app/core/middlewares/registration.py` (`Модуль_Регистрации`)
    - `outer_middleware` на `Message` и `CallbackQuery`: парсинг `ref_<id>` из `/start`-аргумента, вызов `RegistrationService.ensure_user(tg_user, ref_arg, now)`
    - Сервис `app/core/services/registration.py`: upsert через `UsersRepo.upsert_from_tg`, обновление `last_seen_at`, частичный апдейт изменённых полей, логика реф-ссылки (проверить `referrer_id != telegram_id`, что пригласивший существует и что пользователь новый)
    - Ретраи через `with_retry(db_retry, attempts=3, delays=(1,1,1))`; при окончательной ошибке — ответ пользователю + `audit.record_error`
    - В `data["user"]` положить актуальный `User` для нижестоящих middleware
    - _Требования: 1.1, 1.2, 1.3, 1.4, 1.5, 1.7, 1.8, 7.2_
  - [x] 6.3 Реализовать `app/core/middlewares/status_gate.py`
    - Читает `user = data["user"]`; обрабатывает ветки `banned` (Redis-throttle `notify:ban:{id}` TTL 86400 через `SET NX`), `muted & now<muted_until` (throttle `notify:mute:{id}` TTL 600), `muted & now>=muted_until` (авто-снятие в транзакции), `pending_captcha` (делегирование в Antispam)
    - При блокирующем статусе — отправить локализованное уведомление и НЕ вызывать `handler`
    - _Требования: 2.4, 5.2, 5.4, 5.5_
  - [ ]* 6.4 Functional-тест регистрации `tests/functional/test_registration.py`
    - Моки `Bot` + реальный Postgres+Redis: `/start` → `users` содержит запись; повторный `/start` → обновлён `last_seen_at`; `/start ref_<другой_id>` → `referrer_id` сохранён, `referrals_count` пригласившего +1; `/start ref_<свой_id>` → игнорируется; `/start ref_<несуществующий>` → игнорируется
    - _Требования: 1.1, 1.2, 1.7, 1.8, 7.2_
  - [x] 6.5 Реализовать `app/core/handlers/start.py`, `profile.py`, `cancel.py`
    - `/start`: приветственное сообщение, упоминание доступных команд по включённым фичам (i18n — локализованное)
    - `/profile`: рендер профиля (`telegram_id`, display_name, language_code, created_at, status, `ton_address` если `feature_ton_connector`, `referrals_count` если `feature_referrals`); inline-клавиатура «Сменить язык» (только при `feature_i18n`) и «Отвязать кошелёк» (только при наличии `ton_address`); CallbackQuery-хендлеры для обоих действий (смена языка валидирует `supported_locales`)
    - `/cancel`: `state.clear()` + подтверждающее сообщение
    - Все хендлеры регистрируются в одном `core_router`
    - _Требования: 2.1, 2.2, 2.3, 2.5, 10.3, 14.2_
  - [x] 6.6 Реализовать `app/core/services/fsm.py` (обёртка над RedisStorage)
    - Обёртка с health-probe Redis (`ping` с таймаутом 300мс) перед каждым `set_state/set_data`
    - При `RedisError` → локализованное сообщение «Диалоги временно недоступны» + `audit.record_error(source=Redis)`, не ронять процесс
    - TTL FSM-ключей = `fsm_timeout_minutes * 60`
    - _Требования: 14.1, 14.3, 14.4_

- [x] 7. Чекпоинт Stage 1 — ядро запускается
  - Убедиться, что `docker compose up` поднимает Postgres + Redis + бот; `/healthz` отдаёт 200; `/start`, `/profile`, `/cancel` работают; все уже написанные тесты проходят; ask the user if questions arise.

---

### Stage 2 — Админ-панель

- [x] 8. Журнал действий (AuditLog)
  - [x] 8.1 Реализовать `app/core/services/audit.py`
    - Методы: `record_moderation`, `record_error` (с усечением `message` до 1000 символов + суффикс `... [truncated]`), `record_warning`, `record_info`, `list_page(page, page_size=50) -> AuditPage{items, page, total_pages, total}`
    - Запись через `with_retry(db_retry, attempts=3, delays=(1,1,1))`; при исчерпании — fallback: `log.error("audit_drop", ...)` в stdout + отправка первому `superadmin_ids[0]` через отдельный `Bot`-сендер; никогда не блокирует исходное действие
    - _Требования: 6.1, 6.2, 6.6_
  - [ ]* 8.2 Unit-тесты `AuditLog` в `tests/unit/test_audit.py`
    - Усечение длинных сообщений; падение БД → fallback в stdout + попытка уведомления суперадмина; `list_page` возвращает корректный `total_pages`
    - _Требования: 6.2, 6.3, 6.6_

- [x] 9. Планировщик и ежедневная очистка журнала
  - [x] 9.1 Реализовать `app/scheduler/jobs.py` и запуск APScheduler в `app/__main__.py`
    - `AsyncIOScheduler`; добавить job `audit_cleanup`: cron `03:00` UTC ежедневно → `AuditRepo.delete_older_than(now - 90 days)` → `audit.record_info(event="audit_cleanup", details={"deleted": N})`
    - Scheduler стартует после инициализации сервисов и останавливается `wait=True` в graceful shutdown
    - _Требования: 6.4_
  - [ ]* 9.2 Integration-тест `audit_cleanup` в `tests/integration/test_audit_cleanup.py`
    - Заполнить записями разной давности; вызвать job вручную; проверить удаление старше 90 дней + `info`-запись
    - _Требования: 6.4_

- [x] 10. Модуль авторизации
  - [x] 10.1 Реализовать `app/admin/authorization.py`
    - `class Authorization`: `get_role` с Redis-кэшем `auth:role:{id}` TTL 60с (cache-aside), `require_admin`, `require_superadmin`, `invalidate(telegram_id)` (DEL ключа)
    - Seed суперадминов: на старте для каждого `id in settings.superadmin_ids` — `UPSERT users.role = 'superadmin'` (только повышение, не понижение)
    - При ошибке БД — fail-closed (возвращать `None`/`user`)
    - _Требования: 4.1, 4.2, 4.4, 4.7_
  - [x] 10.2 Реализовать фильтры `app/admin/filters.py`
    - `IsAdminFilter(Filter)`: `auth.get_role(event.from_user.id) in (admin, superadmin)`
    - `IsSuperadminFilter(Filter)`: `auth.get_role(...) == superadmin`
    - Применяются на уровне `admin_router.message.filter(IsAdminFilter())` — непрошедшие фильтр апдейты идут к default-хендлеру «Команда не найдена» + `audit.record_warning(event="admin_cmd_unauthorized", actor_id, details={"cmd": text})`
    - _Требования: 4.2, 4.3_
  - [ ]* 10.3 Unit-тесты `Authorization` в `tests/unit/test_authorization.py`
    - Seed суперадминов на старте; cache hit/miss; `invalidate` сбрасывает кэш; fail-closed при недоступной БД
    - _Требования: 4.1, 4.4, 4.7_

- [x] 11. Менеджер пользователей и админ-хендлеры
  - [x] 11.1 Реализовать `app/admin/user_manager.py`
    - Функции `ban`, `unban`, `mute` (парсинг duration: `10m/2h/1d`, валидация 1мин…30дн), `unmute`, `kick` (вызов `bot.ban_chat_member` затем `unban_chat_member(only_if_banned=False)`), `grant_admin`, `revoke_admin`
    - Каждая операция: `Authorization.require_admin` (или `require_superadmin` для grant/revoke), поиск `target` (→ «Пользователь не найден» при отсутствии), защита от модерации админа/суперадмина (→ «Недостаточно прав»), транзакция БД, `audit.record_moderation`, `auth.invalidate(target_id)` для ban/grant/revoke, локализованное подтверждение
    - Throttle уведомлений через Redis: `notify:ban:{id}` SET NX TTL 86400, `notify:mute:{id}` TTL 600
    - _Требования: 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 5.8, 5.9_
  - [x] 11.2 Реализовать `app/admin/handlers.py`
    - Хендлеры `/ban`, `/unban`, `/mute`, `/unmute`, `/kick`, `/grant_admin`, `/revoke_admin`, `/audit [page]` на `admin_router`
    - Парсинг аргументов через aiogram `CommandObject.args`; валидация; вызов соответствующего метода `UserManager`
    - `/audit`: доступен только суперадмину (`IsSuperadminFilter`) — иначе на уровне Router'а не попадает → unauthorized warning + «Команда не найдена»; вызывает `AuditLog.list_page`, рендерит страницу с навигацией (inline-кнопки «← Пред», «След →», «Страница X из Y»); время отклика ≤ 3с обеспечивается индексом `ix_action_log_created_at`
    - _Требования: 4.3, 4.5, 4.6, 5.1–5.9, 6.3, 6.5_
  - [ ]* 11.3 Functional-тесты админ-сценариев в `tests/functional/test_admin.py`
    - `/ban <id> причина` → `status=banned`, запись в `action_log`, уведомление цели один раз за 24 часа, остальные сообщения игнорируются
    - Попытка `/ban` от обычного пользователя → «Команда не найдена» + warning в журнале
    - `/mute <id> 10m` → в течение 10 минут сообщения игнорируются, после истечения `StatusGate` возвращает `active`
    - `/grant_admin` от обычного админа → отклонено + запись в журнал
    - `/audit` от не-суперадмина → «Команда не найдена»
    - _Требования: 4.3, 4.6, 5.2, 5.4, 5.5, 5.8, 6.3, 6.5_

- [x] 12. Чекпоинт Stage 2 — админ-панель
  - Ensure all tests pass, ask the user if questions arise.

---

### Stage 3 — TON Connect

- [x] 13. Манифест и хранилище сессий
  - [x] 13.1 Реализовать `app/ton/manifest.py`
    - Хендлер `/tonconnect-manifest.json` на aiohttp: отдаёт JSON `{url, name, iconUrl, termsOfUseUrl, privacyPolicyUrl}` из `settings`
    - Файл `tonconnect-manifest.json.example` в корне репо
    - _Требования: 3.1, 16.4_
  - [x] 13.2 Реализовать `app/ton/session_store.py`
    - Класс `RedisSessionStore(IStorage)` совместимый с `pytonconnect`: `set_item`, `get_item`, `remove_item` с ключом `tc:session:{telegram_id}`, TTL 600с
    - _Требования: 3.5_

- [x] 14. Коннектор и проверка подписи
  - [x] 14.1 Реализовать `app/ton/verifier.py`
    - `verify_proof(payload, signature, wallet_pubkey, telegram_id, redis)`: разбор `tg:<id>:<ts>:<nonce>`, проверка `telegram_id`, окна `issued_at ∈ [now-10m, now+1m]`, наличия nonce в `tc:nonce:{id}` + его удаление, ed25519-верификация
    - Возвращает `user_friendly_address` или бросает `InvalidProof`
    - _Требования: 3.2, 3.3_
  - [x]* 14.2 Unit-тесты `verify_proof` в `tests/unit/test_ton_verifier.py`
    - Валидный proof → адрес; неверная подпись / просроченный ts / неверный tg_id / повторно использованный nonce → `InvalidProof`
    - _Требования: 3.3_
  - [x] 14.3 Реализовать `app/ton/connector.py` (`TonConnector`)
    - `start_connection(telegram_id) -> StartResult{deeplink, qr_base64, expires_at}`: генерация one-time nonce в `tc:nonce:{id}`, `pytonconnect.TonConnect(manifest_url, RedisSessionStore).connect()`
    - `await_connection(telegram_id) -> ConnectionResult`: ожидание callback, `verify_proof`, если ок — `users.set_wallet(address, wallet_name, now)` с проверкой «один активный кошелёк»; если не ок — `audit.record_error(source="TON Connect", ...)`, Failure
    - `disconnect(telegram_id)`: `pytonconnect.disconnect()`, `users.clear_wallet`, удалить `tc:session:{id}`
    - _Требования: 3.1, 3.2, 3.3, 3.4, 3.6_

- [x] 15. Хендлеры и планировщик TON
  - [x] 15.1 Реализовать `app/ton/handlers.py`
    - `/connect_wallet`: если у пользователя уже есть `ton_address` → «У вас уже привязан кошелёк, используйте /disconnect_wallet»; иначе `start_connection` и ответ с deeplink + QR; запуск `await_connection` как background task
    - `/disconnect_wallet`: делегирование `TonConnector.disconnect`; подтверждение пользователю
    - Кнопка «Отвязать кошелёк» в профиле уже реализована в 6.5 — подключить её здесь к `TonConnector.disconnect`
    - _Требования: 2.3, 3.1, 3.4, 3.6_
  - [x] 15.2 Добавить APScheduler-job `tc_session_cleanup`
    - Каждую минуту: `SCAN tc:session:*` → для истёкших вызывать `pytonconnect.disconnect` и удалять ключ; редактировать оригинальное сообщение пользователю «Сессия истекла, запустите /connect_wallet заново»
    - _Требования: 3.5_
  - [x]* 15.3 Contract-тесты TON в `tests/contract/test_ton_connect.py`
    - Моки `pytonconnect.TonConnect`; полный успешный сценарий connect→proof→save; отказы (невалидная подпись, просроченная сессия, несоответствие tg_id)
    - _Требования: 3.2, 3.3, 3.5_

- [x] 16. Чекпоинт Stage 3 — TON Connect
  - Ensure all tests pass, ask the user if questions arise.

---

### Stage 4 — UX и защита (i18n, антиспам, подписки)

- [ ] 17. Модуль локализации
  - [x] 17.1 Реализовать каталоги переводов
    - `app/locales/ru.yml`, `app/locales/en.yml` — плоский YAML ключ → строка; покрыть все строки ядра, админки, TON-коннектора из уже написанных хендлеров
    - _Требования: 10.1, 10.4_
  - [x] 17.2 Реализовать `app/features/i18n/loader.py` и `middleware.py`
    - `Loader`: загрузка YAML при старте, валидация что в `default_locale` есть все ключи (иначе fail-fast), `translate(key, lang) -> str` с fallback на `default_locale`, при отсутствии ключа в `default_locale` → вернуть ключ + `audit.record_warning(event="missing_translation", details={"key": ...})`
    - `I18nMiddleware`: нормализует `user.language_code` по первичному subtag (`en-US`→`en`), кладёт `data["_"] = partial(translate, lang=...)` для хендлеров
    - _Требования: 10.1, 10.2, 10.3, 10.4, 15.3_
  - [ ]* 17.3 Unit-тесты i18n в `tests/unit/test_i18n.py`
    - Нормализация `en-US`; fallback при отсутствии ключа в выбранном языке; fail-fast при повреждённом `default_locale`; warning при отсутствии ключа везде
    - _Требования: 10.2, 10.4, 15.3_

- [x] 18. Модуль антиспама и капча
  - [x] 18.1 Реализовать `app/features/antispam/ratelimit.py`
    - Sliding-window через Redis ZSET: `ZADD rl:{id} ts ts` + `ZREMRANGEBYSCORE rl:{id} 0 ts-3s` + `ZCARD` — если > 5 за 3с → `SET rl:block:{id} 1 EX 30`
    - Middleware проверяет `rl:block:{id}` и игнорирует апдейты, отвечая «Слишком много сообщений» (throttle самого ответа); пишет warning в `audit` при первом срабатывании блока
    - _Требования: 11.3, 11.4_
  - [x] 18.2 Реализовать `app/features/antispam/captcha.py`
    - Генерация простой числовой капчи (4 inline-кнопки с вариантами); хранение в `captcha:challenge:{id}` (hash: `{correct, tries, expires_at}`) TTL 60с
    - Хендлер проверки ответа: верный → снять `status=pending_captcha`; неверный → `tries+=1`, при 3 подряд → `SET captcha:block:{id} 1 EX 300` + ban-подобное поведение на 300с + warning в `audit`
    - Таймаут 60с без ответа → `status=pending_captcha`, показать капчу заново при следующем сообщении
    - _Требования: 11.1, 11.2_
  - [x] 18.3 Реализовать `app/features/antispam/middleware.py`
    - Порядок внутри middleware: (1) проверка `captcha:block`/`rl:block` — прерывание; (2) при новом пользователе (или `status=pending_captcha`) — запустить/напомнить капчу, прервать хендлер; (3) иначе — rate-limit через 18.1
    - _Требования: 11.1, 11.2, 11.3_
  - [ ]* 18.4 Functional-тесты антиспама в `tests/functional/test_antispam.py`
    - Новый пользователь → капча → верный ответ → доступ; 3 неверных → блок 300с; 6 сообщений за 2с → `rl:block` + игнор 30с
    - _Требования: 11.1, 11.2, 11.3_

- [x] 19. Модуль подписок
  - [x] 19.1 Реализовать `app/features/subscriptions/checker.py`
    - `async def check(user_id) -> list[MissingChannel]`: для каждого канала из `required_channels` — сначала Redis `subs:ok:{user}:{channel}` hit → считаем подписанным; иначе `bot.get_chat_member(channel, user_id)` с таймаутом 5с; на `member/administrator/creator` → кэшируем TTL 300с; на `TelegramForbiddenError/BadRequest/timeout` → `audit.record_error(source="Telegram API")` и пропускаем канал
    - _Требования: 12.1, 12.4_
  - [x] 19.2 Реализовать `app/features/subscriptions/middleware.py`
    - Пропускает `/start`, `/help` без проверки; для остальных команд вызывает `checker.check`; если есть `MissingChannel` — сохраняет `pending_cmd = {command, args}` в FSM-контексте, отправляет inline-клавиатуру со ссылками + кнопкой «Проверить подписку», прерывает цепочку
    - CallbackQuery «Проверить подписку»: повторить `check`; если ок → достать `pending_cmd` из FSM и продиспатчить его через `dispatcher.propagate_event` или ручной вызов хендлера
    - _Требования: 12.1, 12.2, 12.3_
  - [ ]* 19.3 Functional-тест подписок в `tests/functional/test_subscriptions.py`
    - Не подписан → список каналов + блок; после подписки и нажатия «Проверить» → исходная команда выполняется; канал без прав бота → пропускается + error в журнал
    - _Требования: 12.2, 12.3, 12.4_

- [x] 20. Чекпоинт Stage 4 — UX и защита
  - Ensure all tests pass, ask the user if questions arise.

---

### Stage 5 — Рост (рефералы, рассылки, статистика)

- [ ] 21. Реферальная система
  - [x] 21.1 Реализовать `app/features/referrals/handlers.py`
    - `/ref`: получить `bot_username` один раз при старте через `bot.me()`, закэшировать; вернуть `https://t.me/{bot_username}?start=ref_{telegram_id}`
    - `/referrals`: вернуть `referrals_count` и `last_referral_at` из БД (человекочитаемый формат)
    - Сам инкремент счётчика уже выполняется в `Модуль_Регистрации` (задача 6.2) при первом `/start ref_<id>`
    - _Требования: 7.1, 7.2, 7.3, 7.4_
  - [ ]* 21.2 Functional-тест рефералов в `tests/functional/test_referrals.py`
    - `/ref` у A возвращает корректную ссылку; B приходит по ссылке → у A `referrals_count=1`, `last_referral_at` обновлён; `/referrals` у A отображает счётчик
    - _Требования: 7.1, 7.2, 7.4_

- [x] 22. Модуль рассылок
  - [x] 22.1 Реализовать producer в `app/features/broadcasts/producer.py`
    - FSM-диалог `/broadcast`: шаги «введите текст» (1..4096) → «фильтр (`all`/`active_30d`/`lang:<code>`)» → «подтвердить» → `LPUSH bcast:queue` JSON `{id, created_by, text, filter, created_at}`; ограничение ≤10 задач в очереди через `LLEN`
    - Команда `/broadcast_cancel <id>`: `SET bcast:cancel:{id} 1 EX 3600`
    - _Требования: 8.1, 8.5, 8.6_
  - [x] 22.2 Реализовать consumer `app/features/broadcasts/worker.py` (`BroadcastWorker`)
    - Asyncio-task со стартом в `app/__main__.py` при `feature_broadcasts`; цикл `BRPOP bcast:queue` таймаут 5с
    - На каждую задачу: создать `Broadcast(status=running, started_at=now)`; токен-бакет 30 req/s; курсор `users.iterate_for_broadcast(filter + not_blocked=True)`; перед каждой отправкой — проверить `bcast:cancel:{id}` (если стоит — прерваться в течение 5с, `status=cancelled`, промежуточный отчёт)
    - Обработка ошибок: `TelegramForbiddenError` → `users.mark_blocked_bot` + `blocked++`; `TelegramRetryAfter` → sleep(retry_after) и повтор той же итерации (не считать failed); прочее → `failed++` + `audit.record_error`
    - По завершении: `status=completed`, `finished_at=now`, отправить инициатору отчёт `всего/доставлено/не доставлено/заблокировали`
    - _Требования: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_
  - [ ]* 22.3 Integration-тест рассылок в `tests/integration/test_broadcasts.py`
    - Очередь из 2 задач → вторая получает «Позиция в очереди: 2»
    - Mock `Bot` возвращает `TelegramForbiddenError` для 1 пользователя → флаг `is_blocked_bot=true` выставлен, рассылка продолжена, отчёт корректен
    - `TelegramRetryAfter(1)` → sleep + retry → не считается failed
    - Отмена через `bcast:cancel:{id}` → воркер прекращает отправку в пределах 5с
    - _Требования: 8.2, 8.3, 8.4, 8.6_

- [x] 23. Статистика
  - [x] 23.1 Реализовать `app/features/stats/service.py` и хендлер `/stats`
    - `get_overview(now)`: один блок `async with session.begin()` с read-only транзакцией, 5 агрегирующих запросов (`total_users`, `active_24h/7d/30d` по `last_seen_at`, `banned` по `status`, `wallets` по `ton_address IS NOT NULL`)
    - `get_registrations_by_day(from, to)`: `SELECT date_trunc('day', created_at), count(*) FROM users WHERE created_at BETWEEN :from AND :to GROUP BY 1 ORDER BY 1`
    - Хендлер доступен только админам (`IsAdminFilter`); рендер текстовый; время отклика ≤ 3с обеспечивается индексами (уже созданы в 3.2)
    - _Требования: 9.1, 9.2, 9.3_
  - [ ]* 23.2 Integration-тест статистики в `tests/integration/test_stats.py`
    - Заполнить 100 пользователей с разными `last_seen_at`, `status`, `ton_address` → значения в отчёте сходятся; распределение по дням на указанном периоде корректно
    - _Требования: 9.1, 9.2, 9.3_

- [x] 24. Чекпоинт Stage 5 — рост и аналитика
  - Ensure all tests pass, ask the user if questions arise.

---

### Stage 6 — Монетизация (Stars + TON)

- [x] 25. Платежи Telegram Stars
  - [x] 25.1 Реализовать `app/features/payments/stars.py`
    - `create_stars_invoice(user_id, amount, purpose) -> link`: генерация `payload_id` (UUID), `PaymentsRepo.create_pending(provider=stars, ...)`, `bot.create_invoice_link(title, description, payload=str(payload_id), currency="XTR", prices=[LabeledPrice(label, amount)])`
    - Handler `PreCheckoutQuery`: `bot.answer_pre_checkout_query(ok=True)` (базовая валидация payload_id)
    - Handler `successful_payment` на `message.successful_payment`: идемпотентно — `PaymentsRepo.exists_by_charge_id(stars, charge_id)` → выход; иначе `mark_paid(payload_id, charge_id, paid_at=now)` и вызов зарегистрированного хука
    - _Требования: 13.1, 13.3, 13.5_
  - [ ]* 25.2 Functional-тест Stars в `tests/functional/test_payments_stars.py`
    - `/buy` → инвойс создан, запись `pending`; `successful_payment` → `paid`, хук вызван один раз; повторный `successful_payment` с тем же charge_id → никаких изменений
    - _Требования: 13.1, 13.3, 13.5_

- [x] 26. Платежи TON
  - [x] 26.1 Реализовать `app/features/payments/ton.py`
    - `create_ton_payment(user, amount, purpose)`: убедиться, что кошелёк привязан; через `TonConnector` отправить `sendTransaction` на `settings.ton_receive_address` с `payload = str(payload_id)`; `PaymentsRepo.create_pending(provider=ton, expected_amount=amount, expires_at=now+15m)`
    - `TonApiClient` (обёртка над TonCenter/TonAPI) с методами `get_incoming_transactions(address, after=now-15m)` и `find_by_payload(payload_id)`; ретраи `with_retry(attempts=3, delays=(1,2,4))`
    - _Требования: 13.2, 13.3_
  - [x] 26.2 Реализовать APScheduler-job `ton_payments_poll`
    - Cron: каждую минуту; выбирает `Payment.status=pending, provider=ton, expires_at>now`; для каждого — поиск tx по `payload_id`; совпадение суммы → `mark_paid(tx_hash, paid_at)` + вызов хука; несовпадение → `mark_mismatch`, уведомить пользователя; если `created_at <= now-15m` без находки → `mark_expired`, уведомить пользователя, прекратить опрос
    - Идемпотентность через unique index `(provider, tx_hash_or_charge_id)` (создан в 3.2)
    - _Требования: 13.2, 13.3, 13.4, 13.5_
  - [ ]* 26.3 Contract-тесты TON-платежей в `tests/contract/test_payments_ton.py`
    - Моки TonCenter/TonAPI: tx найдена с верной суммой → `paid`; tx с неверной суммой → `mismatch`; tx не появилась за 15 мин → `expired`; повторный запуск job не создаёт дубликатов
    - _Требования: 13.2, 13.4, 13.5_

- [x] 27. Чекпоинт Stage 6 — платежи
  - Ensure all tests pass, ask the user if questions arise.

---

### Stage 7 — Эксплуатация

- [x] 28. Финализация контейнеризации
  - [x] 28.1 Доработать `Dockerfile` (финальная версия)
    - Multi-stage, `pip install --no-cache-dir`, health-check `HEALTHCHECK CMD curl -f http://localhost:8080/healthz || exit 1`, non-root user, переменные `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`
    - _Требования: 17.3_
  - [x] 28.2 Доработать `docker-compose.yml`
    - Прод-профиль с webhook (пример Nginx/Caddy как комментарий); dev-профиль с polling; `restart: unless-stopped`; envfile-разделение prod/dev
    - _Требования: 16.1, 17.3_

- [x] 29. CI через GitHub Actions
  - [x] 29.1 Создать `.github/workflows/ci.yml`
    - Jobs: `lint` (ruff, mypy), `test` (pytest с postgres/redis service-контейнерами, `-m "not contract"` для быстрого прогона + отдельный job `contract` для contract-тестов), `migrations` (`alembic upgrade head && alembic downgrade base && alembic upgrade head` на временной Postgres)
    - Кэш pip, запуск на `push`/`pull_request` в `main`
    - _Требования: 16.1, 16.4_
  - [ ]* 29.2 Создать `.github/workflows/docker.yml` (опционально)
    - Сборка образа и публикация в GHCR при пуше тега `v*`
    - _Требования: 17_

- [x] 30. Документация
  - [x] 30.1 Написать `README.md` с поэтапной инструкцией
    - Секции: «Быстрый старт (docker compose)», «Переменные окружения по этапам (1→6)», «Запуск миграций», «Переключение polling↔webhook», «Добавление нового модуля», «Тестирование (unit/integration/functional/contract)», «Структура проекта», ссылки на `requirements.md` и `design.md`
    - Явно перечислить переменные env по этапам, как в таблице «Поэтапное развёртывание» дизайна
    - _Требования: 15.4, 16.2, 16.4_

- [x] 31. Финальный чекпоинт — готов к продакшну
  - Ensure all tests pass, ask the user if questions arise.

## Примечания

- Задачи, помеченные `*`, являются тестовыми/опциональными — их можно пропустить для ускорения MVP, основная функциональность будет работать.
- Топ-уровневые задачи не помечаются `*`, только подзадачи.
- Ядро (Stage 1) и админка (Stage 2) — обязательны. TON (Stage 3) и далее — по потребности проекта, включаются флагами `feature_*`.
- Каждая подзадача ссылается на конкретные требования из `requirements.md` для трассируемости.
- Порядок middleware критичен: регистрация → статусы → антиспам → i18n → подписки → хендлер. Любое изменение порядка требует повторной проверки требований 1, 2.4, 5.2, 5.4, 11, 12.
- Идемпотентность платежей (13.5) обеспечивается на уровне БД unique-индексом и на уровне сервиса проверкой `exists_by_charge_id` перед записью.
- Все операции записи в БД обёрнуты в `with_retry` (Требования 1.4, 6.6, 17.1); Telegram-вызовы — аналогично, с отдельной веткой `TelegramRetryAfter`.
- Журнал действий никогда не блокирует модерацию: при исчерпании ретраев — fallback в stdout + уведомление суперадмина.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "4.1", "4.2"] },
    { "id": 3, "tasks": ["2.4", "3.3", "4.3", "4.4", "4.5", "5.2", "6.6"] },
    { "id": 4, "tasks": ["3.4", "4.6", "5.1", "5.3", "6.1", "6.2", "6.3"] },
    { "id": 5, "tasks": ["5.4", "6.4", "6.5"] },
    { "id": 6, "tasks": ["8.1", "9.1", "10.1"] },
    { "id": 7, "tasks": ["8.2", "9.2", "10.2", "10.3"] },
    { "id": 8, "tasks": ["11.1"] },
    { "id": 9, "tasks": ["11.2"] },
    { "id": 10, "tasks": ["11.3"] },
    { "id": 11, "tasks": ["13.1", "13.2", "17.1"] },
    { "id": 12, "tasks": ["14.1", "17.2"] },
    { "id": 13, "tasks": ["14.2", "14.3", "17.3", "18.1", "18.2"] },
    { "id": 14, "tasks": ["15.1", "15.2", "18.3", "19.1"] },
    { "id": 15, "tasks": ["15.3", "18.4", "19.2"] },
    { "id": 16, "tasks": ["19.3", "21.1", "22.1", "23.1"] },
    { "id": 17, "tasks": ["21.2", "22.2", "23.2"] },
    { "id": 18, "tasks": ["22.3", "25.1", "26.1"] },
    { "id": 19, "tasks": ["25.2", "26.2"] },
    { "id": 20, "tasks": ["26.3", "28.1", "28.2", "30.1"] },
    { "id": 21, "tasks": ["29.1", "29.2"] }
  ]
}
```
