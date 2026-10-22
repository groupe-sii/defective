"""Inbound message deduplication.

SNS redelivers on Lambda errors, and a channel provider can send the same
inbound webhook twice. Until now a duplicate only produced a duplicate log line;
now that outbound replies are really sent, a duplicate produces a duplicate SMS
(or a duplicate case), so the handler claims each `message_id` before doing any
work.

Claim = a conditional PutItem on a small table. The first arrival wins; every
retry sees `ConditionalCheckFailedException` and is dropped. Items expire via TTL,
so the table stays tiny.

Best-effort by design: if the table is missing or throttled, the message is
*processed* (fail open). Missing one duplicate is better than dropping a real
non-conformity report.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 1


def _table():
    name = os.getenv("DEDUPE_TABLE", "")
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def claim(message_id: str) -> bool:
    """True if this is the first sighting of `message_id`, False if a duplicate."""
    if not message_id:
        return True
    table = _table()
    if table is None:
        return True
    try:
        ttl_days = int(os.getenv("DEDUPE_TTL_DAYS", DEFAULT_TTL_DAYS))
        table.put_item(
            Item={
                "message_id": str(message_id),
                "expires_at": int(
                    (datetime.now(timezone.utc) + timedelta(days=ttl_days)).timestamp()
                ),
            },
            ConditionExpression="attribute_not_exists(message_id)",
        )
        return True
    except Exception as err:
        if type(err).__name__ == "ConditionalCheckFailedException":
            return False
        # Fail open on infrastructure problems.
        logger.warning("Dedupe unavailable for %s (%s); processing anyway", message_id, err)
        return True
