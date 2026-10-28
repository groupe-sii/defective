"""Pre-Token Generation Lambda (V3) for M2M flows — conformity assistant.

Adapted from the upstream template's ``infra-cdk/lambdas/pretoken-v3/index.py``.

Injects CUSTOM claims into M2M access tokens so the AgentCore Gateway can
evaluate the Cedar policy in ``gateway/policies/policy.cedar``. This Lambda fires
on BOTH user login and M2M token generation; only M2M (Client Credentials) flows
are processed here.

Claims injected:
    - user_id: the authenticated user's Cognito sub (UUID)
    - role:    "technician" or "expert", resolved from the conformity-contacts
               DynamoDB table (see below)

WHY A DYNAMODB LOOKUP (vs the upstream template's hardcoded UUID map):
​The upstream template ships a hardcoded ``USER_ROLE_MAP`` that requires a two-step deploy (look up
subs, paste them in, redeploy). The conformity assistant already has a contacts
table — the same one the channel handler uses to map a phone number to a Cognito
user. We read the role from it live, so promoting someone to expert is a data
change (put_item), not a redeploy.

The verified_user_id (Cognito sub) is read from clientMetadata, passed via
aws_client_metadata in the Cognito /oauth2/token call (see
patterns/conformity-agent/tools/gateway.py -> utils/auth.py).
"""

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "")
# Lambda defines AWS_REGION; AWS_DEFAULT_REGION is reserved and cannot be set.
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))

# Fail safe: an unknown user is the least-privileged role, never an expert.
DEFAULT_ROLE = "technician"

_dynamodb = None


def _table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb.Table(CONTACTS_TABLE)


def _resolve_role(user_id: str) -> str:
    """Look up the role for a Cognito sub in the contacts table."""
    if not CONTACTS_TABLE or not user_id:
        return DEFAULT_ROLE
    try:
        item = _table().get_item(Key={"actor_id": user_id}).get("Item") or {}
        role = str(item.get("role", "")).lower()
        return role if role == "expert" else DEFAULT_ROLE
    except Exception as err:
        logger.error("role lookup failed for %s: %s", user_id, err)
        return DEFAULT_ROLE


def lambda_handler(event: dict, context: dict) -> dict:
    """Cognito V3 Pre-Token Generation trigger handler."""
    logger.info("[PRE-TOKEN] Trigger source: %s", event.get("triggerSource"))

    if event["triggerSource"] != "TokenGeneration_ClientCredentials":
        logger.info("[PRE-TOKEN] Not a Client Credentials flow - skipping")
        return event

    meta = event["request"].get("clientMetadata", {})
    user_id = meta.get("verified_user_id", "")

    if not user_id:
        logger.info("[PRE-TOKEN] No verified_user_id in metadata")
        return event

    role = _resolve_role(user_id)
    logger.info("[PRE-TOKEN] Assigned role=%s for user_id=%s", role, user_id)

    # The Gateway JWT authorizer maps these to Cedar principal tags:
    #   "user_id" -> principal.getTag("user_id")
    #   "role"    -> principal.getTag("role")
    event["response"]["claimsAndScopeOverrideDetails"] = {
        "accessTokenGeneration": {
            "claimsToAddOrOverride": {
                "user_id": user_id,
                "role": role,
            }
        }
    }
    logger.info("[PRE-TOKEN] Claims injected successfully")
    return event
