"""Repro: что видит клиент, если запись метеринга упала ПОСЛЕ успешного вызова."""
import asyncio
from fastmcp import Client
from predmarket_mcp.server import mcp
from predmarket_mcp.billing.middleware import get_billing

async def main():
    billing = get_billing()
    orig = billing.metering.record
    def boom(usage):                     # диск полон / БД заблокирована
        raise OSError("disk I/O error")
    billing.metering.record = boom
    try:
        async with Client(mcp) as c:
            try:
                r = (await c.call_tool("find_mispricing", {"min_edge": 0.02})).data
                print("клиент получил РЕЗУЛЬТАТ, возможностей:", r.get("count"))
            except Exception as e:
                print(f"клиент получил ОШИБКУ: {type(e).__name__}: {str(e)[:90]}")
                print("(инструмент при этом отработал успешно — результат потерян)")
    finally:
        billing.metering.record = orig
asyncio.run(main())
