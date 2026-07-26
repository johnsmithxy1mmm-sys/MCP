"""Фаза 2.5/2.8: враждебный вход от площадок + фаззинг парсеров + ReDoS."""
import httpx, time, json, math
FAILS=[]
def chk(name, ok, detail=""):
    (print(f"  ok  {name}" + (f" | {detail}" if detail else "")) if ok
     else (FAILS.append(name), print(f"FAIL {name} | {detail}")))

# ---------- A. Враждебные ответы площадок ----------
from core.adapters.polymarket import PolymarketAdapter
from core.adapters.kalshi import KalshiAdapter
from core.adapters import base, AdapterError

HOSTILE = {
  "null вместо списка":       lambda r: httpx.Response(200, json=None),
  "объект вместо списка":     lambda r: httpx.Response(200, json={"unexpected":"shape"}),
  "строка вместо числа":      lambda r: httpx.Response(200, json=[{"conditionId":"c","question":"q","outcomePrices":'["abc","def"]',"outcomes":'["Yes","No"]'}]),
  "цена вне диапазона":       lambda r: httpx.Response(200, json=[{"conditionId":"c","question":"q","outcomePrices":'["999","-5"]',"outcomes":'["Yes","No"]'}]),
  "None внутри массива":      lambda r: httpx.Response(200, json=[None, {"conditionId":"c","question":"q","outcomePrices":'["0.5","0.5"]',"outcomes":'["Yes","No"]'}]),
  "глубокая вложенность":     lambda r: httpx.Response(200, json=json.loads("["*40 + "]"*40)),
  "битый JSON":               lambda r: httpx.Response(200, text="{not json"),
  "HTTP 500":                 lambda r: httpx.Response(500, text="boom"),
  "HTTP 429":                 lambda r: httpx.Response(429, text="slow down"),
}
for name, handler in HOSTILE.items():
    base._CLIENTS.clear()
    from core import circuit; circuit._BREAKERS.clear()
    orig = base.make_client
    base.make_client = lambda url, timeout=None: httpx.Client(base_url=url, transport=httpx.MockTransport(handler))
    try:
        ms = PolymarketAdapter().fetch_markets()
        bad = [m for m in ms if not (0.0 <= m.yes_price <= 1.0)]
        chk(f"polymarket / {name}", not bad, f"{len(ms)} рынков" + (f", ВНЕ [0,1]: {[m.yes_price for m in bad]}" if bad else ""))
    except AdapterError:
        chk(f"polymarket / {name}", True, "AdapterError (штатная деградация)")
    except Exception as e:
        chk(f"polymarket / {name}", False, f"НЕОЖИДАННОЕ {type(e).__name__}: {str(e)[:70]}")
    finally:
        base.make_client = orig

# ---------- B. Фаззинг парсера утверждений (ReDoS/краш) ----------
from core.entailment import parse_claim
from core.models import Market, Venue
def mk(t): return Market(venue=Venue.KALSHI, market_id="m", title=t, yes_price=0.5, no_price=0.5)
EVIL = [
    "above " + "9"*5000,                       # огромное число
    "$" + "1,"*3000 + "0",                     # длинная группировка
    "above " * 2000 + "100k",                  # повтор комаратора
    "\x00above 100k\x00",                      # нулевые байты
    "above 100k " + "‮"*500,              # RTL-переопределение
    "😀"*3000 + " above 100k",                  # эмодзи/суррогаты
    " ".join(["2026"]*3000) + " above 100k",   # много годов
    "above 1e400",                             # переполнение float
    "above -0",                                # минус ноль
    "above 100000000000000000000000000000k",   # запредельный порог
]
for i, t in enumerate(EVIL):
    t0=time.monotonic()
    try:
        c = parse_claim(mk(t)); dt=time.monotonic()-t0
        bad_inf = c is not None and not math.isfinite(c.threshold)
        chk(f"parse_claim / вход #{i}", dt < 2.0 and not bad_inf,
            f"{dt*1000:.0f}ms" + (" ПОРОГ=inf" if bad_inf else ""))
    except Exception as e:
        chk(f"parse_claim / вход #{i}", False, f"{type(e).__name__}: {str(e)[:60]}")

# ---------- C. Матчер на длинных строках ----------
from core.algorithms import title_similarity
t0=time.monotonic(); title_similarity("a"*20000, "b"*20000); dt=time.monotonic()-t0
chk("title_similarity / 20k символов", dt<3.0, f"{dt*1000:.0f}ms")

print()
print(f"ИТОГ враждебного входа: {len(FAILS)} провалов" + (f": {FAILS}" if FAILS else ""))
