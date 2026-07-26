# 00 — Модель системы (фаза 0)

Аудит: `predmarket-mcp`, ветка `audit/2026-07-26`, база `5474990`.
Суждений о качестве в этом документе нет — только карта.

## 1. Что это

MCP-сервер (FastMCP, streamable-HTTP), продающий агентам аналитику
предсказательных рынков поштучно. **Исполнения сделок и кастодиальности нет** —
это сужает поверхность до данных, денег за вызовы и репутации.

Размер: 9 472 строк кода, 4 623 строк тестов, 431 тест, 50 модулей `core/`.

## 2. Точки входа

| Класс | Поверхность | Аутентификация |
|---|---|---|
| MCP tools | 12 (4 free / 8 paid) | нет (x402-гейт по флагу) |
| MCP resources | 11 URI-шаблонов | **самопроверка по заголовку** (см. §3) |
| MCP prompt | 1 | нет |
| HTTP | `/health`, `/metrics`, `/pubkey`, `/track-record`, `/audit/{id}` | нет (публичные) |
| HTTP | `/revenue` | `X-Admin-Token`, constant-time |

Слои на пути запроса: `RateLimitMiddleware` (ASGI, внешний) → `X402Middleware`
(ASGI, гейт оплаты) → FastMCP → `MeteringMiddleware` (tool-level) → `tools.py` →
`deps.py` → `core/`.

## 3. Граф доверия (что приходит извне и не контролируется)

| Источник | Куда попадает | Валидация |
|---|---|---|
| **`x-client-id` (заголовок)** | `_client_id()` → владелец watch/alerts/portfolio/leaderboard | **нет; значение принимается как есть** |
| `authorization` (заголовок) | fallback-идентификатор (sha256) | приоритет ниже `x-client-id` |
| `x-forwarded-for` | rate-limit по IP | нет (доверяем прокси) |
| `X-PAYMENT` (заголовок) | декод base64→JSON → фасилитатор | структурная (Mock) / внешняя (Http) |
| Аргументы tool-вызовов | pydantic-схемы | типы + границы; `market_id`/`venue` — свободные строки |
| `bundle_id` в `/audit/{id}` | SQL-параметр | параметризован |
| **Ответы площадок** (Polymarket/Kalshi/Manifold/Deribit) | нормализация → вся аналитика | форма ответа **предполагается**, не контрактно проверена |
| Ответ nowcast-фида (K7) | house probability | `float`, клэмп [0,1] |
| Ответ Anthropic (аналитик) | resolution_risk / basis | клэмп [0,1] |
| Ответ фасилитатора x402 | квитанция | `isValid`/`success` |
| WSS-тики | история цен | `normalize_tick`, клэмп |
| ENV (≈100 переменных) | всё | частично (`_env_float` глотает мусор в дефолт) |
| Системное время | TTL, холдинг, окна | wall clock + monotonic смешаны по модулям |

## 4. Граф состояния

**Персистентно (15 таблиц, 11 отдельных SQLite-файлов):**

| Хранилище | Таблица | Что теряется при порче |
|---|---|---|
| `RECON_DB_URL` | `flagged` | track record — репутация |
| `HOUSE_DB_URL` | `house_forecasts` | доказательство «мы точнее рынка» |
| `LEADERBOARD_DB_URL` | `paper_trades` | доказанная альфа клиентов |
| `METERING_DB_URL` | `usage`, `receipts`, `used_payments` | **деньги** + защита от повтора |
| `ACCURACY_DB_URL` | `venue_samples` | веса площадок |
| `MATCHLEARN_DB_URL` | `match_obs` | калибровка матчера |
| `PERSISTENCE_DB_URL` | `sightings`, `lifespans` | модель выживаемости |
| `AUDIT_DB_URL` | `audit_bundles` | подписанные доказательства |
| `ANCHOR_DB_URL` | `anchors` | якоря в чейне |
| `WATCH_DB_URL` | `watches`, `alerts` | подписки клиентов |
| `HISTORY_DB_URL` | `price_history` | история (SQLite или Postgres/Timescale) |

**В памяти (переживает только процесс):** 12 `lru_cache`-синглтонов хранилищ;
TTL-кэши рынков (30 с) и стаканов (5 с) в `live.py`; LRU эмбеддингов; кэш
калибратора по числу семплов; кэш nowcast (60 с); токен-бакеты рейт-лимита;
состояния circuit breaker; пул httpx-клиентов по хостам.

**Общее между инстансами (только при `REDIS_URL`):** replay-guard, рейт-лимит,
watch/alerts. Всё остальное — локальное; **при >1 инстансе расходится**.

## 5. Граф побочных эффектов

| Эффект | Обратим? | Где |
|---|---|---|
| **Расчёт USDC у фасилитатора** | **НЕТ** | `x402.HttpFacilitator.verify_and_settle` |
| **Транзакция в чейн (якорь)** | **НЕТ** | `anchor.EvmChainAnchor.submit` |
| Запись в 15 таблиц | да (бэкап) | все store |
| POST на вебхук клиента | нет (ушло) | `notifier.notify_alerts` |
| Запросы к платным API (Anthropic/Voyage) | нет (деньги оператора) | `analyst`, `embeddings` |
| Запросы к площадкам | да | адаптеры |
| Логи | — | `configure_logging` |

## 6. Конкурентность

- **4 фоновых демон-потока:** alert-engine (30 с), resolution-engine (3600 с),
  stream-ingestor (WSS), pg-loop (выделенный asyncio-луп для asyncpg).
- **Пул потоков ASGI:** каждый `anyio.to_thread.run_sync` (метеринг, проверка
  платежа, чтения track-record) — параллельные исполнения одного кода.
- **19 модулей с `threading.Lock`**, но лок защищает **только своё хранилище**;
  межхранилищной атомарности нет нигде.
- **Точки read-modify-write без атомарности:** `check_x402` (is_used → settle →
  mark_used), `PersistenceStore.observe`, `PaperTradeStore.record` (счёт открытых
  → вставка), `AnchorStore.maybe_anchor` (latest → submit → save).

## 7. Hotspots (частота правок = где кучкуются баги)

```
34  src/predmarket_mcp/deps.py     ← шов между MCP и core, самый горячий
26  src/predmarket_mcp/tools.py
16  tests/conftest.py              ← изоляция состояния тестов
11  core/mock.py · core/live.py · core/algorithms.py   ← деньги/edge
10  src/predmarket_mcp/resources.py ← поверхность доступа к данным
 7  src/predmarket_mcp/billing/middleware.py
 6  src/predmarket_mcp/billing/x402.py
```

11 из 61 коммита (18%) — исправления/аудиты. Три предыдущих аудит-прохода уже
находили здесь реальные баги (OverflowError в платном вызове, размерностно
неверный edge, накрутка лидерборда, замороженный газ) — плотность дефектов в
`deps.py`/`algorithms.py`/`x402.py` подтверждена исторически.

## 8. Границы аудита (объявлены заранее)

Недоступно из этого окружения и **не будет проверено**:
- живые API площадок (нет egress) — форма ответов остаётся предположением;
- реальный x402-фасилитатор и движение USDC;
- реальная EVM-сеть для якорения;
- поведение под настоящей нагрузкой и на >1 инстансе;
- Postgres/Timescale-путь (asyncpg) на живой БД.
