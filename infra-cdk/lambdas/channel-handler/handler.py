"""Inbound channel handler — SNS -> AgentCore Runtime -> channel.

This is the "messaging brick" FAST does not ship. It replaces the POC's
``lambdas/lambda_function.py``, which hosted the agent in-process; here the agent
lives in the AgentCore Runtime and this Lambda is a thin, stateless adapter:

    AWS End User Messaging (WhatsApp/SMS)
        -> SNS (customer-managed KMS key)
        -> this Lambda
             1. normalize the envelope            (messages.py)
             2. map the phone to a Cognito user   (contacts table)
             3. InvokeAgentRuntime  (SigV4 + runtime-user-id header)
             4. dispatch the reply                (dispatcher.py, MOCK_CHANNELS)

Because the runtime is addressed by the *user's* identity, conversation history
lives in AgentCore Memory per user — so a technician's WhatsApp and SMS messages
share one conversation, and history survives container restarts (the POC needed
a DynamoDB row per identity+day to approximate this).

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

import dispatcher
from messages import parse_sns_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "")
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)
_dynamodb = boto3.resource("dynamodb", region_name=_REGION)


# ---------------------------------------------------------------------------
# Identity resolution: phone -> Cognito user
# ---------------------------------------------------------------------------

def _resolve_contact(phone_identity: str):
    """Look up the contact row that owns an inbound phone number.

    Returns the DynamoDB item (with ``actor_id``, ``role``, optional
    ``cognito_username``) or ``None`` for an unknown number. Unknown senders are
    still served — they are attributed to a deterministic synthetic actor so the
    demo works before Cognito users exist — but they are never treated as experts.
    """
    if not CONTACTS_TABLE or not phone_identity:
        return None
    try:
        from boto3.dynamodb.conditions import Key

        resp = _dynamodb.Table(CONTACTS_TABLE).query(
            IndexName="phone_identity-index",
            KeyConditionExpression=Key("phone_identity").eq(phone_identity),
            Limit=1,
        )
        items = resp.get("Items") or []
        return items[0] if items else None
    except Exception as err:
        logger.error("contact lookup failed for %s: %s", phone_identity, err)
        return None


def _identity_for(phone_identity: str):
    """Return (actor_id, role) for an inbound number."""
    contact = _resolve_contact(phone_identity)
    if contact:
        return contact["actor_id"], str(contact.get("role", "technician")).lower()
    # Deterministic synthetic actor: stable across messages so history accumulates
    # into one conversation even before the number is provisioned in Cognito.
    return f"phone:{phone_identity}", "technician"


def _session_id(actor_id: str) -> str:
    """One conversation per actor per UTC day.

    Mirrors the POC's 'one row per identity + day' behaviour, but as a runtime
    session id. Must be at least 33 characters for AgentCore.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    base = f"{actor_id}-{day}"
    return (base + "-" + "0" * 33)[:128]


# ---------------------------------------------------------------------------
# Runtime invocation
# ---------------------------------------------------------------------------

def _invoke_runtime(actor_id: str, text: str, channel: str) -> str:
    """Call the conformity agent and return its final reply text.

    The agent streams events; we concatenate the ``data`` deltas and prefer the
    final result payload when present. <thinking> blocks are already stripped by
    the runtime before streaming.
    """
    payload = json.dumps({
        "prompt": text,
        "runtimeSessionId": _session_id(actor_id),
        "channel": channel,
    }).encode("utf-8")

    response = _agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=_session_id(actor_id),
        runtimeUserId=actor_id,          # SigV4: propagated user identity
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )

    body = response["response"]
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    return _extract_reply(raw)


def _extract_reply(raw: str) -> str:
    """Pull assistant text out of the runtime's event stream.

    Handles three shapes, in order of preference:
      * a final AgentResult event carrying the whole message;
      * a stream of ``data`` deltas (concatenated);
      * a single JSON object (non-streaming response).
    """
    full_text = ""
    result_text = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(event.get("data"), str):
            full_text += event["data"]

        # Strands emits a terminal event with the aggregated result.
        result = event.get("result")
        if isinstance(result, dict):
            message = result.get("message") or {}
            for block in message.get("content", []):
                if isinstance(block, dict) and "text" in block:
                    result_text += block["text"]
        elif isinstance(event.get("message"), dict):
            for block in event["message"].get("content", []):
                if isinstance(block, dict) and "text" in block:
                    result_text += block["text"]

    return (result_text or full_text).strip()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def process_record(record: dict):
    sns = record.get("Sns", {})
    raw = sns.get("Message", "{}")
    if not raw:
        logger.error("Empty SNS record: %s", record)
        return None

    # RawMessageDelivery (if enabled) hands the envelope directly; otherwise it is
    # a JSON string inside Sns.Message. parse_sns_message accepts both.
    try:
        sns_message = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("SNS message is not valid JSON")
        return None

    inbound = parse_sns_message(sns_message)
    if not inbound:
        logger.error("No inbound message could be parsed from the SNS record")
        return None

    results = []
    for message in inbound:
        if not message.is_text:
            logger.error("Unsupported message type: %s", message.message_type)
            continue

        actor_id, role = _identity_for(message.identity)
        logger.info("on %s resolved %s as %s (actor=%s)",
                    message.channel, message.sender, role, actor_id)

        try:
            reply_text = _invoke_runtime(actor_id, message.text, message.channel)
        except Exception as err:
            logger.exception("Agent invocation failed for %s", actor_id)
            reply_text = "Sorry, something went wrong while handling your message."

        dispatch_result = dispatcher.send_reply(
            channel=message.channel,
            sender=message.identity,
            text=reply_text,
            phone_number_id=message.channel_meta.get("phone_number_id", ""),
            message_id=message.message_id,
            origination_number=message.recipient,
        )
        results.append({
            "channel": message.channel,
            "identity": message.identity,
            "reply": reply_text,
            "dispatch": dispatch_result,
        })
    return results


def lambda_handler(event, context):
    try:
        records = event.get("Records", [])
        results = []
        for record in records:
            results.append(process_record(record))
        return {"statusCode": 200, "body": json.dumps(results, default=str)}
    except Exception as err:
        logger.error("Error processing event: %s", err)
        return {"statusCode": 500, "body": json.dumps("Error processing request")}
