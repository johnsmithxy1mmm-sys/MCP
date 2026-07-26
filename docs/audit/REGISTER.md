# Сводный реестр находок

Аудит `predmarket-mcp`, ветка `audit/2026-07-26`, база `5474990`.
Сортировка: (вероятность в проде) × (необратимость последствий).

| ID | Severity | Класс | Файл | Confidence | Репро | Статус |
|---|---|---|---|---|---|---|
| INV-001 | Critical | Гонка / деньги | billing/middleware.py:135 | high | `repro/repro_double_settle.py` | открыта |
| INV-002 | Critical | Контроль доступа | resources.py:31,57 · tools.py:69 | high | `repro/repro_idor.py` | открыта |
| INV-003 | High | Ошибки / деньги | billing/middleware.py:192 | high | `repro/repro_metering_fail.py` | открыта |
| INV-007 | Medium | Гонка | core/anchor.py:231 | low | — | SUSPECTED |
| INV-006 | Low | Семантика API | core/algorithms.py:216 | medium | — | SUSPECTED |

Фаза 2 (арсенал атак) не запускалась — ожидается сверка модели.
