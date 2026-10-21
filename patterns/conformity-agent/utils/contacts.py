"""Contact / identity resolution for the conformity agent.

Two lookups, both backed by AgentCore Identity + a small DynamoDB table:

  1. ``resolve_role(identity)`` — is this user a technician or an expert?
     Reads the ``role`` attribute from the ``conformity-contacts`` table, keyed by
     the authenticated Cognito ``sub``.

  2. ``resolve_identity_for_phone(phone)`` — which Cognito user owns this phone
     number? Used by the channel-handler Lambda (``infra-cdk/lambdas/channel-
     handler``) to turn an inbound WhatsApp/SMS number into a real Cognito
     identity, because field technicians interact over chat and never log in.

Both functions fail safe: a missing row yields the least-privileged answer
(technician), never expert. Enforcement is doubled at the Gateway (Cedar +
per-tool Lambda check), so this is defence-in-depth, not the only gate.
"""

import logging
import os

logger = logging.getLogger(__name__)

CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "")
_ACTOR_ROLE_ATTR = "role"
_PHONE_INDEX_ATTR = "phone_identity"
_ROLE_TECHNICIAN = "technician"

_dynamodb = None


def _table():
    """Lazily build the DynamoDB table handle (keeps import side-effect free)."""
    global _dynamodb
    if _dynamodb is None:
        import boto3

        _dynamodb = boto3.resource("dynamodb",
                                   region_name=os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
    return _dynamodb.Table(CONTACTS_TABLE)


def resolve_role(identity: str) -> str:
    """Return 'expert' or 'technician' for a Cognito identity (sub)."""
    if not CONTACTS_TABLE or not identity:
        return _ROLE_TECHNICIAN
    try:
        resp = _table().get_item(Key={"actor_id": identity})
        item = resp.get("Item") or {}
        role = str(item.get(_ACTOR_ROLE_ATTR, "")).lower()
        return "expert" if role == "expert" else _ROLE_TECHNICIAN
    except Exception as err:
        logger.error("resolve_role failed for %s: %s", identity, err)
        return _ROLE_TECHNICIAN


def resolve_identity_for_phone(phone_identity: str):
    """Map a normalized phone (digits only) to its contact row.

    Returns the item (with ``actor_id``, ``role``, optional ``cognito_username``)
    or ``None`` when the number is unknown. Requires the ``phone_identity-index``
    GSI on the contacts table.
    """
    if not CONTACTS_TABLE or not phone_identity:
        return None
    try:
        from boto3.dynamodb.conditions import Key

        resp = _table().query(
            IndexName="phone_identity-index",
            KeyConditionExpression=Key(_PHONE_INDEX_ATTR).eq(phone_identity),
            Limit=1,
        )
        items = resp.get("Items") or []
        return items[0] if items else None
    except Exception as err:
        logger.error("resolve_identity_for_phone failed for %s: %s", phone_identity, err)
        return None
