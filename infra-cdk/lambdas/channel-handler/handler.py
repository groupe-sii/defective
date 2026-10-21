"""Inbound channel handler — SNS -> AgentCore Runtime -> channel.

This is the "messaging brick" FAST does not ship. It replaces the POC's
``lambdas/lambda_function.py``, which hosted the agent in-process; here the agent
lives in the AgentCore Runtime and this Lambda is a thin, stateless adapter:

    AWS End User Messaging (WhatsApp/SMS)
        -> SNS (customer-managed KMS key)
        -> this Lambda
             1. normalize the envelope            (messages.py)
             2. map the phone to a Cognito user   (contacts table)
             3. InvokeAgentRuntime  (SigV4 + custom user-id header)
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
import re
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

# The caller identity reaches the agent container as a *custom-prefixed* header.
# AgentCore rejects `x-amzn-*` names in a runtime allowlist unless they carry the
# `X-Amzn-Bedrock-AgentCore-Runtime-Custom-` prefix, and `Authorization` is only
# allowed when the runtime has a customJWTAuthorizer (this one is IAM SigV4).
# Keep in sync with the runtime's requestHeaderConfiguration in
# infra-cdk/lib/backend-construct.ts and the reader in
# patterns/conformity-agent/utils/auth.py.
_USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id"


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

    The id is also used as an AgentCore *Memory* sessionId, which is validated
    against:

        [a-zA-Z0-9][a-zA-Z0-9-_]*

    So it may contain only letters, digits, hyphen and underscore, and must not
    start with a separator. That rules out the ``phone:<number>`` synthetic actor
    id verbatim — the colon is rejected:

        ValidationException: Value at 'sessionId' failed to satisfy constraint:
        Member must satisfy regular expression pattern: [a-zA-Z0-9][a-zA-Z0-9-_]*

    Any disallowed character is therefore replaced with ``-``, which keeps the id
    deterministic (the same actor always maps to the same session) while staying
    valid. Hashing is deliberately avoided: a readable id is worth more when
    debugging Memory.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", str(actor_id))
    # Guard the first character: the pattern forbids leading separators.
    if not safe or not safe[0].isalnum():
        safe = f"a{safe}"
    base = f"{safe}-{day}"
    # The 33-char pad is what satisfies AgentCore's minimum session-id length.
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

    response = _invoke_with_user_id(
        payload=payload,
        session_id=_session_id(actor_id),
        actor_id=actor_id,
    )

    body = response["response"]
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    return _extract_reply(raw)


def _invoke_with_user_id(payload: bytes, session_id: str, actor_id: str) -> dict:
    """Call InvokeAgentRuntime with the caller identity attached.

    Two distinct channels carry the identity, and both are needed:

      * ``runtimeUserId=actor_id`` — a first-class API parameter. AgentCore uses
        it to scope Memory, so conversation history is per person.
      * the allowlisted ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id``
        header — what the agent reads from ``context.request_headers`` (see
        ``patterns/conformity-agent/utils/auth.py``). boto3 does not expose
        arbitrary headers on this API, so it is injected with a botocore
        ``before-sign`` handler, exactly as the AgentCore docs show.
    """
    events = _agentcore.meta.events
    event_name = "before-sign.bedrock-agentcore.InvokeAgentRuntime"

    def _add_user_id_header(request, **_kwargs):
        request.headers.add_header(_USER_ID_HEADER, actor_id)

    handler = events.register_first(event_name, _add_user_id_header)
    try:
        return _agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            contentType="application/json",
            accept="application/json",
            payload=payload,
        )
    finally:
        # Idempotent and cheap, and it keeps a warm Lambda from accumulating
        # handlers across invocations.
        events.unregister(event_name, handler)


def _iter_sse_events(raw: str):
    """Yield the JSON payloads of a Strands AgentCore event stream.

    The runtime answers with Server-Sent Events, so each payload line is
    ``data: {json}`` — not a bare ``{json}`` line. FAST's own frontend client
    (`frontend/src/lib/agentcore-client/parsers/strands.ts`) is the reference
    for this format:

        data: {"data": "partial text"}                        # text delta
        data: {"current_tool_use": {...}, "delta": {...}}     # tool use
        data: {"message": {"role": "user"|"assistant", ...}}  # whole message
        data: {"result": {"stop_reason": "end_turn"}}         # terminal

    A non-streaming response (plain JSON body) is also tolerated: if no SSE
    framing is present the whole body is parsed as one object.
    """
    saw_sse = False
    fallback_events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            saw_sse = True
            line = line[len("data:"):].strip()
            if not line or line == "[DONE]":
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
            continue
        if line.startswith(("event:", "id:", "retry:", ":")):
            # Other SSE fields carry no payload we care about.
            continue
        # Not SSE-framed: hold it back in case the whole body is one JSON object.
        try:
            fallback_events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    if saw_sse:
        return
    if fallback_events:
        # Only one of these can be the real body; a bare-JSON response is a
        # single object. Prefer the last (whole-body) parse over per-line noise.
        if len(fallback_events) > 1:
            try:
                yield json.loads(raw)
                return
            except json.JSONDecodeError:
                pass
        for event in fallback_events:
            yield event


def _extract_reply(raw: str) -> str:
    """Pull the assistant's final text out of the runtime's event stream.

    Prefers the concatenated ``data`` deltas (the streamed assistant text). The
    terminal ``{"result": {"stop_reason": ...}}`` event carries no text, and
    ``message`` events can be *user* messages holding tool results — including
    those would splice internal tool output into what the technician receives,
    so only assistant text is collected from them, and only as a fallback for
    runtimes that don't stream deltas.
    """
    streamed = ""
    messages_text = ""

    for event in _iter_sse_events(raw):
        if not isinstance(event, dict):
            continue

        # Primary: streamed assistant text deltas.
        if isinstance(event.get("data"), str):
            streamed += event["data"]

        # Fallback: a complete assistant message (no deltas seen).
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    messages_text += block["text"]

    return (streamed or messages_text).strip()


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
