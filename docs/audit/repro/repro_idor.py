"""INV-002 (ИСПРАВЛЕНА): попытка прочитать чужие алерты/портфель.

До правки: подставить `x-client-id: <жертва>` и запросить `portfolio://<жертва>` —
обе стороны проверки владения были подконтрольны атакующему, доступ разрешался.
После: URI, называющего чужого принципала, не существует, а `me` резолвится из
выведенной идентичности.
"""
import asyncio, json
from unittest.mock import patch
from fastmcp import Client
from predmarket_mcp.server import mcp
from predmarket_mcp import deps

async def main():
    from core.models import Leg, Side, Venue
    deps.create_watch("victim-corp", 0.0)
    deps.commit_paper_trade("victim-corp",
        [Leg(venue=Venue.KALSHI, market_id="kx-btc-100k-eoy26", side=Side.YES)], 5000)

    attacker = {"x-client-id": "victim-corp"}
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=attacker):
        async with Client(mcp) as c:
            for uri in ("alerts://victim-corp", "portfolio://victim-corp"):
                try:
                    await c.read_resource(uri)
                    print(f"  ПРОЧИТАНО {uri}  <-- УТЕЧКА")
                except Exception as e:
                    print(f"  недоступно {uri}: {type(e).__name__} (URI не существует)")
            pf = json.loads((await c.read_resource("portfolio://me"))[0].text)
            al = json.loads((await c.read_resource("alerts://me"))[0].text)
    print()
    print("ИНВАРИАНТ 'клиент видит только своё':",
          "СОБЛЮДЁН" if (pf["trades"] == [] and al["count"] == 0)
          else f"НАРУШЕН (сделок {len(pf['trades'])}, алертов {al['count']})")
    print(f"  идентичность вызывающего: {pf['identity']} (proven={pf['identity_proven']})")
asyncio.run(main())
