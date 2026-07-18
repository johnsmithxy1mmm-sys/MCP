"""Alert push transport (B2): webhook delivery for fired alerts.

OFF by default. When ``ALERT_WEBHOOK_URL`` is set, newly-queued alerts are POSTed
there as they fire, so a client gets true server-initiated push instead of
polling. This is the stateless-friendly counterpart to MCP resource-update
notifications (which need a live session): the ``alerts://{client_id}`` resource
covers the pull/subscribe side, this covers the push side.

Graceful degradation: no URL configured, or ``httpx`` missing, or the POST fails
— the alert is still queued for the next ``poll_alerts``; delivery is best effort
and never raises into the fire path.

  ALERT_WEBHOOK_URL     https://… endpoint to POST fired alerts to
  ALERT_WEBHOOK_SECRET  optional bearer token sent as Authorization
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("predmarket.notifier")


def webhook_url() -> str | None:
    return os.getenv("ALERT_WEBHOOK_URL") or None


def notify_alerts(alerts: list[dict]) -> bool:
    """POST ``alerts`` to the configured webhook. Returns True if delivered.

    No-op (returns False) when unconfigured or on any failure — the caller's
    alert queue is the source of truth, so a dropped push just means the client
    picks the alert up on its next poll.
    """
    url = webhook_url()
    if not url or not alerts:
        return False
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dep in practice
        return False
    import json

    body = json.dumps({"alerts": alerts}).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.getenv("ALERT_WEBHOOK_SECRET")
    if secret:
        # HMAC-SHA256 over the exact body so the receiver can verify authenticity
        # (a bearer token alone can be replayed; a signature binds to the payload).
        import hashlib
        import hmac

        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Signature"] = f"sha256={sig}"
        headers["Authorization"] = f"Bearer {secret}"
    try:
        with httpx.Client(timeout=float(os.getenv("HTTP_TIMEOUT", "12"))) as client:
            resp = client.post(url, content=body, headers=headers)
        return 200 <= resp.status_code < 300
    except Exception:
        log.warning("alert webhook delivery failed (%d alerts queued for poll)", len(alerts))
        return False
