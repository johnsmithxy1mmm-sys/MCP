"""Repro: settle-once инвариант при двух одновременных запросах с ОДНИМ подписанным платежом."""
import threading, tempfile, os
from predmarket_mcp.billing.middleware import BillingContext
from predmarket_mcp.billing.x402 import Facilitator, Receipt, encode_payment_header
from predmarket_mcp.config import Settings

settle_calls = []
lock = threading.Lock()

class CountingFacilitator(Facilitator):
    """Считает, сколько раз реально дошло до расчёта (= списаний с плательщика)."""
    def verify_and_settle(self, payment, requirement):
        with lock:
            settle_calls.append(requirement.tool_name)
        return Receipt("r"+str(len(settle_calls)), requirement.tool_name,
                       requirement.price_usd, "USDC", "base", "payer", "op", "0xtx")

tmp = tempfile.mkdtemp()
b = BillingContext(Settings(paid_enabled=True, payment_rail="x402",
                            metering_db_url=f"sqlite:///{tmp}/m.db"))
b.facilitator = CountingFacilitator()

payment = {"scheme": "exact", "network": "base-sepolia",
           "payload": {"signature": "0xSIG", "authorization": {"value": "5000000", "from": "0xA"}}}
headers = {"x-payment": encode_payment_header(payment)}
calls = [("find_mispricing", {"min_edge": 0.02})]

results = []
barrier = threading.Barrier(2)
def worker():
    barrier.wait()                     # максимизируем перекрытие
    results.append(b.check_x402(calls, headers).ok)

ts = [threading.Thread(target=worker) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]

print(f"расчётов у фасилитатора (списаний с плательщика): {len(settle_calls)}")
print(f"успешных вызовов отдано клиенту:                  {sum(results)}")
print(f"квитанций сохранено:                              {b.receipts.count()}")
print()
print("ИНВАРИАНТ 'один подписанный платёж -> максимум один расчёт':",
      "СОБЛЮДЁН" if len(settle_calls) <= 1 else f"НАРУШЕН ({len(settle_calls)} списания)")
