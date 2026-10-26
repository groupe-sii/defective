"""Conversation transcript store.

The channel handler already knows everything a transcript needs: who spoke, on
which channel, what they said, and what the assistant answered. Persisting that
turns the handler's ephemeral knowledge into something the ops console (Phase C)
and the local phone emulator can render — instead of scraping CloudWatch logs,
which split multi-line replies across events and lose structure.

One item per message:

    identity   (PK, digits)       the person; channel-independent
    ts         (SK, ISO + role)   sortable, collision-free
    role                          "user" | "assistant"
    channel, text, actor_id, message_id, dispatch

TTL keeps it cheap: demo transcripts expire after `TRANSCRIPT_TTL_DAYS`.

Writing a transcript is best-effort. A transcript outage must never fail a
message the technician has already received, so failures are logged and
swallowed.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 7


def _table():
    name = os.getenv("TRANSCRIPT_TABLE", "")
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def record(identity: str, role: str, text: str, channel: str,
           actor_id: str = "", message_id: str = "", dispatch=None,
           image_url: str = "") -> None:
    """Append one message to the transcript (best-effort, never raises)."""
    table = _table()
    if table is None:
        return
    try:
        now = datetime.now(timezone.utc)
        ttl_days = int(os.getenv("TRANSCRIPT_TTL_DAYS", DEFAULT_TTL_DAYS))
        item = {
            "identity": str(identity),
            "ts": f"{now.isoformat()}#{role}",
            "role": role,
            "channel": channel,
            "text": text,
            "created_at": now.isoformat(),
            "expires_at": int((now + timedelta(days=ttl_days)).timestamp()),
        }
        if actor_id:
            item["actor_id"] = actor_id
        if message_id:
            item["message_id"] = message_id
        if dispatch is not None:
            item["dispatch"] = str(dispatch)[:1000]
        if image_url:
            item["image_url"] = str(image_url)[:2000]
        table.put_item(Item=item)
    except Exception:
        logger.exception("Transcript write failed for %s", identity)
