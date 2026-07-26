"""Фаза 2.2: точечный мутационный анализ критических модулей.
Вопрос не 'работает ли код', а 'падают ли тесты, когда логику ломают'."""
import subprocess, pathlib, shutil, tempfile

MUT = [
 ("algorithms: ceil комиссии -> floor (занижение платы)",
  "core/algorithms.py", "return math.ceil(raw * 100.0) / 100.0", "return math.floor(raw * 100.0) / 100.0",
  "tests/test_intelligence.py"),
 ("algorithms: units = min(ног) -> max(ног) (завышение исполнимого)",
  "core/algorithms.py", "units = min(leg_contracts)", "units = max(leg_contracts)",
  "tests/test_intelligence.py tests/test_tools.py"),
 ("algorithms: выплата all-NO (N-1) -> N (завышение арбитража)",
  "core/algorithms.py", "return float(len(legs) - 1)", "return float(len(legs))",
  "tests/test_intelligence.py"),
 ("x402: снять проверку повторного использования платежа",
  "src/predmarket_mcp/billing/middleware.py", "if self.nonces.is_used(fingerprint):", "if False:",
  "tests/test_billing.py"),
 ("x402: платный гейт всегда пропускает",
  "src/predmarket_mcp/billing/middleware.py", "return self.settings.paid_enabled and self.settings.payment_rail == \"x402\"", "return False",
  "tests/test_billing.py tests/test_money.py"),
 ("leaderboard: снять клэмп размера сделки",
  "core/leaderboard.py", "size = min(max_size, max(0.0, float(size_usd)))", "size = float(size_usd)",
  "tests/test_leaderboard.py"),
 ("houseforecast: разрешить перезапись оценённого прогноза",
  "core/houseforecast.py", "WHERE house_forecasts.resolved = 0", "",
  "tests/test_houseview.py"),
 ("resources: снять проверку владельца портфеля",
  "src/predmarket_mcp/resources.py", "if client_id != caller_id():", "if False:",
  "tests/test_leaderboard.py tests/test_watches.py"),
]
survived=[]
for name, f, old, new, tests in MUT:
    p = pathlib.Path(f); orig = p.read_text()
    if old not in orig:
        print(f"  ??  {name}: паттерн не найден — пропуск"); continue
    p.write_text(orig.replace(old, new, 1))
    try:
        r = subprocess.run(f"uv run pytest {tests} -q -x --no-header 2>&1 | tail -1",
                           shell=True, capture_output=True, text=True, timeout=300)
        killed = "failed" in r.stdout or "error" in r.stdout.lower()
        print(f"  {'убит' if killed else 'ВЫЖИЛ'}  {name}")
        if not killed: survived.append(name)
    finally:
        p.write_text(orig)
print()
print(f"ИТОГ мутаций: выжило {len(survived)}/{len(MUT)}")
for s in survived: print(f"   ВЫЖИВШИЙ: {s}")
