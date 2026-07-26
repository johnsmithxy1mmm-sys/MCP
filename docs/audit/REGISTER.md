# Сводный реестр находок

`predmarket-mcp` · ветка `audit/2026-07-26` · база `5474990` · фазы 0–2 завершены.
Сортировка: (вероятность срабатывания в проде) × (необратимость последствий).

| ID | Sev | Класс | Файл:строка | Conf | Репро |
|---|---|---|---|---|---|
| INV-001 | **Critical** | Гонка / деньги | billing/middleware.py:135 | high | `repro_double_settle.py` |
| INV-002 | **Critical** | Контроль доступа | resources.py:31,57 · tools.py:69 | high | `repro_idor.py` |
| INV-004 | **Critical** | DoS через данные площадки | algorithms.py:257 | high | `repro_hostile_input.py` |
| INV-003 | High | Ошибки / деньги | billing/middleware.py:192 | high | `repro_metering_fail.py` |
| INV-005 | High | Валидация границы | models.py:46 · adapters/polymarket.py:94 | high | `repro_hostile_input.py` |
| INV-008 | High | Краш мимо границы | adapters/polymarket.py:58 | high | `repro_hostile_input.py` |
| INV-009 | Medium | Гонка / лимиты | watches.py:127 | high | `repro_concurrency.py` |
| INV-010 | Medium | Конфиг / потеря денег | billing/metering.py:62 · x402.py:59 | high | `repro_hostile_input.py` |
| INV-007 | Medium | Гонка / газ | anchor.py:231 | low | — |
| INV-011 | Low | Округление | models.py:188 | high | `repro_properties.py` |
| INV-012 | Low | Округление | algorithms.py:219 | high | `repro_properties.py` |
| INV-006 | Low | Семантика API | algorithms.py:216 | medium | — |
| TEST-01 | High | Качество тестов | — | high | `repro_mutation.py` |

## Мутационный анализ (аудит тестов)

3 из 8 хирургических мутаций критического пути **выжили** — тесты их не заметили:

| Мутация | Что это значит |
|---|---|
| `units = min(ног)` → `max(ног)` | Правило «баскет ограничен тончайшей ногой» не покрыто ни одним тестом |
| Снять `is_used()` перед расчётом | Тест на повтор платежа не различает, какой из двух барьеров сработал — то есть не проверяет, что расчёт **не дошёл** до фасилитатора |
| Снять первую проверку владельца в `resources.py` (alerts) | У проверки владельца алертов нет ни одного теста |

Убиты (тесты сработали): занижение комиссии, завышение выплаты dutch-book,
отключение платного гейта, снятие клэмпа размера сделки, разрешение перезаписи
оценённого прогноза.
