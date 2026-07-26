"""Фаза 2.7: конкурентность. Каждый store — параллельные операции, проверка инварианта."""
import threading, tempfile, os, sys
tmp = tempfile.mkdtemp()
R = []
def check(name, inv, ok, detail=""):
    R.append((name, ok)); print(f"{'  ok ' if ok else 'FAIL'} {name}: {inv}" + (f" | {detail}" if detail else ""))

def run(n, fn):
    b = threading.Barrier(n)
    def w(i):
        b.wait()
        try: fn(i)
        except Exception as e: pass
    ts=[threading.Thread(target=w,args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]

from core.models import Leg, Opportunity, OpportunityKind, Side, Venue

def opp(t="x", edge=0.05):
    return Opportunity(kind=OpportunityKind.CROSS_VENUE, title=t, category="crypto",
        realizable_edge=edge, max_size_usd=1000,
        legs=[Leg(venue=Venue.KALSHI, market_id="m1", side=Side.YES)])

# --- 1. Лимит открытых бумажных сделок (K4) ---
from core.leaderboard import PaperTradeStore
os.environ["LEADERBOARD_MAX_OPEN"]="5"
lb = PaperTradeStore(f"sqlite:///{tmp}/lb.db")
run(20, lambda i: lb.record("c", 100, [{"venue":"kalshi","market_id":f"m{i}","side":"yes","entry_price":0.5}]))
n_open = lb.count()
check("leaderboard.open_cap", "открытых сделок <= LEADERBOARD_MAX_OPEN(5)", n_open<=5, f"фактически {n_open}")

# --- 2. Лимит watch на клиента ---
from core.watches import WatchStore
import core.watches as W
W.WATCH_MAX_PER_CLIENT = 5
ws = WatchStore(f"sqlite:///{tmp}/w.db")
run(20, lambda i: ws.create_watch("c", 0.01))
n_w = ws.count_active("c")
check("watches.per_client_cap", "активных watch <= WATCH_MAX_PER_CLIENT(5)", n_w<=5, f"фактически {n_w}")

# --- 3. Deliver-once для алертов ---
ws2 = WatchStore(f"sqlite:///{tmp}/w2.db")
ws2.create_watch("c2", 0.0)
ws2.fire([opp("a1")])
seen=[]
lk=threading.Lock()
def drain(i):
    got = ws2.drain("c2")
    with lk: seen.extend(got)
run(8, drain)
check("watches.deliver_once", "алерт выдан ровно один раз", len(seen)==1, f"выдан {len(seen)} раз")

# --- 4. Заморозка house-прогноза после резолюции (K1) ---
from core.houseforecast import HouseForecastStore
hf = HouseForecastStore(f"sqlite:///{tmp}/h.db")
hf.record("m1","kalshi","t",0.70,0.60)
def race_hf(i):
    if i%2: hf.resolve({"m1":1})
    else:   hf.record("m1","kalshi","t",0.99,0.99)   # попытка переписать
run(16, race_hf)
import sqlite3
with hf._connect() as c:
    row = c.execute("SELECT house_prob, resolved FROM house_forecasts WHERE market_id='m1'").fetchone()
check("houseforecast.freeze", "оценённый прогноз неизменяем",
      not (row["resolved"]==1 and abs(row["house_prob"]-0.99)<1e-9),
      f"resolved={row['resolved']} house_prob={row['house_prob']}")

# --- 5. Реконсиляция: двойной учёт при параллельном resolve ---
from core.reconciliation import ReconciliationStore
rs = ReconciliationStore(f"sqlite:///{tmp}/r.db")
rs.record(opp("t1"), {"m1":0.6})
run(8, lambda i: rs.resolve({"m1":1}))
m = rs.metrics()
check("reconciliation.resolve_once", "запись разрешается один раз",
      m["resolved_count"]==1, f"resolved_count={m['resolved_count']}")

# --- 6. Учёт жизни возможностей (F2) ---
from core.persistence import PersistenceStore
ps = PersistenceStore(f"sqlite:///{tmp}/p.db")
run(8, lambda i: ps.observe([opp("live")]))
with ps._connect() as c:
    sight = c.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    life  = c.execute("SELECT COUNT(*) FROM lifespans").fetchone()[0]
check("persistence.observe", "одна возможность = одна запись наблюдения, 0 закрытых жизней",
      sight==1 and life==0, f"sightings={sight} lifespans={life}")

# --- 7. Метеринг: ровно одна запись на вызов ---
from predmarket_mcp.billing.metering import LocalBackend, UsageRecord
mb = LocalBackend(f"sqlite:///{tmp}/m.db")   # сырой путь теперь корректно отвергается (INV-010)
run(20, lambda i: mb.record(UsageRecord("find_mispricing",0.05,"USDC","ok",1.0,"c","x402",{})))
check("metering.count", "20 параллельных записей = 20 строк", mb.count()==20, f"строк {mb.count()}")

print()
bad=[n for n,ok in R if not ok]
print(f"ИТОГ: {len(R)-len(bad)}/{len(R)} инвариантов удержались; нарушено: {bad or 'нет'}")
