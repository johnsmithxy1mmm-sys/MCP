"""Watches + alert queue — the push/subscription model.

An agent registers a *watch* (an edge threshold + optional filters); the server
evaluates watches against live opportunities and queues *alerts* when one crosses
the threshold. The agent retrieves fired alerts with a cheap poll instead of
re-running the flagship scanner on a loop.

This changes the economics (billable subscriptions + alerts, not just per-scan)
and the usage model (react to opportunities, don't poll for them). Alerts are
computed on the server (push) and drained by the client (pull) — which works
over stateless MCP and is serverless-friendly, without server-initiated SSE.
# TODO: also emit MCP resource-update notifications for true server push.

SQLite by default (WATCH_DB_URL).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .models import Opportunity

# Per-client cap on active watches, so one caller can't register unbounded
# standing scans (each watch is evaluated on every fire).
WATCH_MAX_PER_CLIENT = int(os.getenv("WATCH_MAX_PER_CLIENT", "50"))
# Redis hygiene: dedup-set TTL (an opp re-alerts after this — acceptable) and a
# cap on a client's alert queue (a client that never polls can't grow memory
# without bound; oldest alerts are dropped first).
WATCH_SEEN_TTL = int(os.getenv("WATCH_SEEN_TTL_SECONDS", str(7 * 24 * 3600)))
ALERT_QUEUE_MAX = int(os.getenv("ALERT_QUEUE_MAX", "1000"))


class WatchLimitError(RuntimeError):
    """Raised when a client already holds the maximum number of active watches."""


def _sqlite_path(db_url: str) -> str:
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///"):]
    if db_url.startswith("sqlite://"):
        return db_url[len("sqlite://"):]
    return str(Path.cwd() / "watches.db")


def _opp_sig(opp: Opportunity) -> str:
    key = opp.kind.value + "|" + opp.title + "|" + json.dumps(
        [(l.venue.value, l.market_id, l.side.value) for l in opp.legs], sort_keys=True
    )
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _opp_payload(opp: Opportunity) -> dict:
    return {
        "kind": opp.kind.value,
        "title": opp.title,
        "category": opp.category,
        "realizable_edge": opp.realizable_edge,
        "annualized_edge": opp.annualized_edge,
        "max_size_usd": opp.max_size_usd,
        "legs": [{"venue": l.venue.value, "market_id": l.market_id, "side": l.side.value} for l in opp.legs],
    }


class WatchStore:
    def __init__(self, db_url: str | None = None):
        self._url = db_url or os.getenv("WATCH_DB_URL", "sqlite:///watches.db")
        self._path = _sqlite_path(self._url)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    min_edge REAL NOT NULL,
                    category TEXT,
                    kind TEXT,
                    event TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    watch_id TEXT NOT NULL,
                    opp_sig TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (watch_id, opp_sig)
                )
                """
            )

    # -- watches -----------------------------------------------------------
    def count_active(self, client_id: str) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM watches WHERE client_id = ? AND active = 1",
                (client_id,),
            ).fetchone()[0]

    def create_watch(
        self, client_id: str, min_edge: float,
        category: str | None = None, kind: str | None = None, event: str | None = None,
    ) -> str:
        if self.count_active(client_id) >= WATCH_MAX_PER_CLIENT:
            raise WatchLimitError(
                f"active watch limit reached ({WATCH_MAX_PER_CLIENT}); "
                "cancel a watch before adding another"
            )
        watch_id = "w_" + uuid.uuid4().hex[:12]
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO watches (watch_id, client_id, min_edge, category, kind, event, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (watch_id, client_id, min_edge, category, kind, event,
                 datetime.now(timezone.utc).isoformat()),
            )
        return watch_id

    def list_watches(self, client_id: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watches WHERE client_id = ? AND active = 1 ORDER BY created_at",
                (client_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def cancel_watch(self, client_id: str, watch_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watches SET active = 0 WHERE watch_id = ? AND client_id = ?",
                (watch_id, client_id),
            )
            return cur.rowcount > 0

    # -- fire + drain ------------------------------------------------------
    def fire(self, opps: list[Opportunity]) -> int:
        """Evaluate all active watches against `opps`, queue new matching alerts.

        Deduped by (watch_id, opp signature) so a standing opportunity alerts once.
        Newly-queued alerts are pushed to the webhook (best effort) so a client
        gets them without polling. Returns the number of new alerts queued.
        """
        fired = 0
        new_alerts: list[dict] = []
        with self._lock, self._connect() as conn:
            watches = conn.execute("SELECT * FROM watches WHERE active = 1").fetchall()
            for w in watches:
                for opp in opps:
                    if opp.realizable_edge < w["min_edge"]:
                        continue
                    if w["category"] and (opp.category or "") != w["category"]:
                        continue
                    if w["kind"] and opp.kind.value != w["kind"]:
                        continue
                    if w["event"] and w["event"].lower() not in opp.title.lower():
                        continue
                    ts = datetime.now(timezone.utc).isoformat()
                    payload = _opp_payload(opp)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO alerts (watch_id, opp_sig, client_id, ts, payload) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (w["watch_id"], _opp_sig(opp), w["client_id"], ts, json.dumps(payload)),
                    )
                    if cur.rowcount:
                        fired += cur.rowcount
                        new_alerts.append({
                            "watch_id": w["watch_id"], "client_id": w["client_id"],
                            "ts": ts, "opportunity": payload,
                        })
        if new_alerts:
            self._push(new_alerts)
        return fired

    @staticmethod
    def _push(new_alerts: list[dict]) -> None:
        """Best-effort webhook push of newly-queued alerts (never raises)."""
        try:
            from .notifier import notify_alerts

            notify_alerts(new_alerts)
        except Exception:  # push must never break firing
            pass

    def peek(self, client_id: str) -> list[dict]:
        """Return this client's undelivered alerts WITHOUT marking them delivered.

        Backs the ``alerts://{client_id}`` resource (subscribe/peek), so reading
        the resource doesn't consume alerts the way ``drain`` (poll_alerts) does.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT watch_id, ts, payload FROM alerts "
                "WHERE client_id = ? AND delivered = 0 ORDER BY ts",
                (client_id,),
            ).fetchall()
        return [
            {"watch_id": r["watch_id"], "ts": r["ts"], "opportunity": json.loads(r["payload"])}
            for r in rows
        ]

    def drain(self, client_id: str) -> list[dict]:
        """Return this client's undelivered alerts and mark them delivered."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT watch_id, opp_sig, ts, payload FROM alerts "
                "WHERE client_id = ? AND delivered = 0 ORDER BY ts",
                (client_id,),
            ).fetchall()
            if rows:
                conn.execute(
                    "UPDATE alerts SET delivered = 1 WHERE client_id = ? AND delivered = 0",
                    (client_id,),
                )
        return [
            {"watch_id": r["watch_id"], "ts": r["ts"], "opportunity": json.loads(r["payload"])}
            for r in rows
        ]


class RedisWatchStore:
    """Redis-backed watch store for multi-instance deployments.

    Same surface as :class:`WatchStore`, but watches and alert queues live in
    Redis, so a watch created on instance A and an alert fired on instance B are
    both visible everywhere. Alert dedup uses ``SADD`` (atomic, once per
    watch+opportunity); each client's undelivered alerts are a Redis list that
    ``drain`` pops transactionally.
    """

    def __init__(self, redis_client):
        self._redis = redis_client

    # -- keys -------------------------------------------------------------
    @staticmethod
    def _wkey(watch_id): return f"w:{watch_id}"
    @staticmethod
    def _client_set(client_id): return f"wc:{client_id}"
    @staticmethod
    def _seen(watch_id): return f"aseen:{watch_id}"
    @staticmethod
    def _queue(client_id): return f"aq:{client_id}"

    # -- watches ----------------------------------------------------------
    def count_active(self, client_id: str) -> int:
        return int(self._redis.scard(self._client_set(client_id)))

    def create_watch(self, client_id, min_edge, category=None, kind=None, event=None) -> str:
        if self.count_active(client_id) >= WATCH_MAX_PER_CLIENT:
            raise WatchLimitError(
                f"active watch limit reached ({WATCH_MAX_PER_CLIENT}); "
                "cancel a watch before adding another")
        watch_id = "w_" + uuid.uuid4().hex[:12]
        self._redis.hset(self._wkey(watch_id), mapping={
            "watch_id": watch_id, "client_id": client_id, "min_edge": str(min_edge),
            "category": category or "", "kind": kind or "", "event": event or "",
            "active": "1", "created_at": datetime.now(timezone.utc).isoformat()})
        self._redis.sadd(self._client_set(client_id), watch_id)
        self._redis.sadd("watches:active", watch_id)
        return watch_id

    def list_watches(self, client_id: str) -> list[dict]:
        out = []
        for wid in self._redis.smembers(self._client_set(client_id)):
            h = self._redis.hgetall(self._wkey(wid))
            if h:
                out.append(self._decode(h))
        return sorted(out, key=lambda w: w.get("created_at", ""))

    def cancel_watch(self, client_id: str, watch_id: str) -> bool:
        h = self._redis.hgetall(self._wkey(watch_id))
        if not h or h.get("client_id") != client_id or h.get("active") != "1":
            return False  # unknown, not yours, or already cancelled
        # Full cleanup: ids are random uuids, so nothing references a cancelled
        # watch — leaving the hash + dedup set behind would leak Redis memory.
        self._redis.srem(self._client_set(client_id), watch_id)
        self._redis.srem("watches:active", watch_id)
        self._redis.delete(self._wkey(watch_id))
        self._redis.delete(self._seen(watch_id))
        return True

    @staticmethod
    def _decode(h: dict) -> dict:
        return {
            "watch_id": h.get("watch_id"), "client_id": h.get("client_id"),
            "min_edge": float(h.get("min_edge", 0) or 0),
            "category": h.get("category") or None, "kind": h.get("kind") or None,
            "event": h.get("event") or None,
            "active": int(h.get("active", 0) or 0), "created_at": h.get("created_at"),
        }

    # -- fire + drain -----------------------------------------------------
    def fire(self, opps: list[Opportunity]) -> int:
        fired = 0
        new_alerts: list[dict] = []
        for wid in list(self._redis.smembers("watches:active")):
            h = self._redis.hgetall(self._wkey(wid))
            if not h or h.get("active") != "1":
                continue
            w = self._decode(h)
            for opp in opps:
                if opp.realizable_edge < w["min_edge"]:
                    continue
                if w["category"] and (opp.category or "") != w["category"]:
                    continue
                if w["kind"] and opp.kind.value != w["kind"]:
                    continue
                if w["event"] and w["event"].lower() not in opp.title.lower():
                    continue
                if not self._redis.sadd(self._seen(wid), _opp_sig(opp)):
                    continue  # already alerted for this watch+opportunity
                # Refresh the dedup-set TTL so it expires only after inactivity.
                self._redis.expire(self._seen(wid), WATCH_SEEN_TTL)
                ts = datetime.now(timezone.utc).isoformat()
                payload = _opp_payload(opp)
                item = {"watch_id": wid, "ts": ts, "opportunity": payload}
                queue = self._queue(w["client_id"])
                self._redis.rpush(queue, json.dumps(item))
                # Cap the queue: a client that never polls keeps only the newest N.
                self._redis.ltrim(queue, -ALERT_QUEUE_MAX, -1)
                fired += 1
                new_alerts.append({**item, "client_id": w["client_id"]})
        if new_alerts:
            WatchStore._push(new_alerts)
        return fired

    def peek(self, client_id: str) -> list[dict]:
        items = self._redis.lrange(self._queue(client_id), 0, -1)
        return [json.loads(i) for i in items]

    def drain(self, client_id: str) -> list[dict]:
        key = self._queue(client_id)
        pipe = self._redis.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        items = pipe.execute()[0]
        return [json.loads(i) for i in items]


@lru_cache(maxsize=1)
def get_watches():
    """Redis-backed store when REDIS_URL is set (multi-instance), else SQLite."""
    try:
        from predmarket_mcp.kv import get_redis

        client = get_redis()
        if client is not None:
            return RedisWatchStore(client)
    except Exception:
        pass
    return WatchStore()


class AlertEngine:
    """Background thread that fires watches on an interval (server-side push).

    OFF by default (``ALERT_ENGINE`` unset) so offline tests and serverless
    deploys stay fully synchronous — there, ``poll_alerts`` fires inline. When
    enabled, this loop does the firing and ``poll_alerts`` becomes drain-only, so
    a slow scan never blocks a client's cheap poll. Degrades gracefully: a scan
    error is swallowed and retried next tick; it never crashes the server.
    """

    def __init__(self, store: WatchStore, scan_fn, interval: float | None = None):
        self._store = store
        self._scan_fn = scan_fn
        self._interval = interval if interval is not None else float(
            os.getenv("ALERT_ENGINE_INTERVAL", "30")
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def enabled() -> bool:
        return os.getenv("ALERT_ENGINE", "").lower() in ("1", "on", "true", "yes")

    def start(self) -> "AlertEngine":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alert-engine", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> int:
        """Run one scan+fire cycle (also used directly by tests)."""
        try:
            return self._store.fire(self._scan_fn())
        except Exception:
            return 0  # never let a bad scan kill the loop

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            # Sleep in small slices so stop() is responsive.
            waited = 0.0
            while waited < self._interval and not self._stop.is_set():
                time.sleep(min(0.5, self._interval - waited))
                waited += 0.5


_ENGINE: AlertEngine | None = None
_ENGINE_LOCK = threading.Lock()


def start_alert_engine(scan_fn) -> AlertEngine | None:
    """Start the background engine if ``ALERT_ENGINE`` is set. Idempotent."""
    global _ENGINE
    if not AlertEngine.enabled():
        return None
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = AlertEngine(get_watches(), scan_fn).start()
    return _ENGINE


def alert_engine_running() -> bool:
    return _ENGINE is not None
