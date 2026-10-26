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

import base64
import json
import contextvars
import logging
import os
import re
from datetime import datetime, timezone

import boto3
import botocore

import dedupe
import dispatcher
import transcript
from messages import parse_sns_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ["RUNTIME_ARN"]
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "")
CASES_TABLE = os.environ.get("CASES_TABLE", "")
TRANSCRIPT_TABLE = os.environ.get("TRANSCRIPT_TABLE", "")
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)
_dynamodb = boto3.resource("dynamodb", region_name=_REGION)

# Logged at import so the deployed SDK is identifiable from CloudWatch — the
# SigV4 behaviour of the InvokeAgentRuntime call below depends on it (see
# requirements.txt).
logger.info("channel-handler starting: boto3=%s botocore=%s", boto3.__version__, botocore.__version__)

# The caller identity reaches the agent container as a *custom-prefixed* header.
# AgentCore rejects `x-amzn-*` names in a runtime allowlist unless they carry the
# `X-Amzn-Bedrock-AgentCore-Runtime-Custom-` prefix, and `Authorization` is only
# allowed when the runtime has a customJWTAuthorizer (this one is IAM SigV4).
# Keep in sync with the runtime's requestHeaderConfiguration in
# infra-cdk/lib/backend-construct.ts and the reader in
# patterns/conformity-agent/utils/auth.py.
_USER_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id"

# The actor for the invocation currently being processed. A contextvar rather
# than a closure because the before-sign handler below is registered exactly
# once per container (see the note under _invoke_with_user_id).
_CURRENT_ACTOR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "channel_handler_actor", default=""
)

_SIGN_EVENT = "before-sign.bedrock-agentcore.InvokeAgentRuntime"


def _add_user_id_header(request, **_kwargs):
    """Attach the current caller's identity to the SigV4-signed request."""
    actor = _CURRENT_ACTOR.get()
    if actor:
        request.headers.add_header(_USER_ID_HEADER, actor)


# Registered ONCE per container, deliberately never unregistered.
#
# boto3 does not expose arbitrary headers on InvokeAgentRuntime, so the identity
# header has to be injected inside botocore's signing pipeline: it must be
# present *before* SigV4 runs, otherwise the service computes a different
# signature. ``register_first`` is what puts it ahead of the signer.
#
# The earlier code registered a fresh handler per call and "unregistered" it in a
# finally block. ``register_first`` returns None, so
# ``unregister(event_name, None)`` silently removed nothing: one handler piled up
# per invocation in a warm container. From the second call on, the header was
# added twice, the signer and the wire disagreed about the duplicated value, and
# InvokeAgentRuntime failed with
#
#   AccessDeniedException: The request signature we calculated does not match
#   the signature you provided
#
# which is why a cold start appeared to "fix" it. One handler + a contextvar has
# neither problem; a stale actor cannot leak because _invoke_with_user_id resets
# the contextvar in its finally block.
_agentcore.meta.events.register_first(_SIGN_EVENT, _add_user_id_header)


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


class RuntimeReplyError(RuntimeError):
    """The runtime streamed a failure (or nothing) instead of an answer."""


def _session_id(actor_id: str) -> str:
    """One conversation per actor per UTC day.

    Mirrors the POC's 'one row per identity + day' behaviour, but as a runtime
    session id. Must be at least 33 characters for AgentCore.

    The id is also used as an AgentCore *Memory* sessionId, which is validated
    against:

        [a-zA-Z0-9][a-zA-Z0-9-_]*

    So it may contain only letters, digits, hyphen and underscore, and must not
    start with a separator. That rules out the ``phone:<number>`` synthetic actor
    id verbatim â€” the colon is rejected:

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

    """The runtime streamed a failure (or nothing) instead of an answer."""


def _latest_image_url(identity: str, within_minutes: int = 10) -> str:
    """Return the most recent image URL recorded for this identity, if recent."""
    if not TRANSCRIPT_TABLE:
        return ""
    try:
        from datetime import datetime, timezone, timedelta
        table = boto3.resource("dynamodb", region_name=_REGION).Table(TRANSCRIPT_TABLE)
        resp = table.query(
            KeyConditionExpression="#i = :i",
            ExpressionAttributeNames={"#i": "identity"},
            ExpressionAttributeValues={":i": identity},
            ScanIndexForward=False,
            Limit=20,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        for item in resp.get("Items", []):
            url = item.get("image_url")
            if not url:
                continue
            try:
                ts = datetime.fromisoformat(item.get("ts", "").split("#")[0])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    return str(url)
            except (ValueError, TypeError):
                return str(url)
        return ""
    except Exception as err:  # noqa: BLE001
        logger.warning("latest image lookup failed for %s: %s", identity, err)
        return ""



def _download_image(url: str, timeout: int = 15):
    """Download an image and return (base64_str, format) or (None, None)."""
    if not url:
        return None, None
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "conformity-handler/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
        fmt = "jpeg"
        # Detect from magic bytes first (S3 may return application/octet-stream).
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            fmt = "png"
        elif data[:3] == b"\xff\xd8\xff":
            fmt = "jpeg"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            fmt = "gif"
        elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = "webp"
        elif "png" in ctype:
            fmt = "png"
        elif "webp" in ctype:
            fmt = "webp"
        elif "gif" in ctype:
            fmt = "gif"
        elif "jpeg" in ctype or "jpg" in ctype:
            fmt = "jpeg"
        if not data or len(data) > 4 * 1024 * 1024:
            logger.warning("image too large or empty: %d bytes", len(data))
            return None, None
        return base64.b64encode(data).decode("ascii"), fmt
    except Exception as err:  # noqa: BLE001
        logger.warning("image download failed for %s: %s", url, err)
        return None, None
    """One conversation per actor per UTC day.

    Mirrors the POC's 'one row per identity + day' behaviour, but as a runtime
    session id. Must be at least 33 characters for AgentCore.

    The id is also used as an AgentCore *Memory* sessionId, which is validated
    against:

        [a-zA-Z0-9][a-zA-Z0-9-_]*

    So it may contain only letters, digits, hyphen and underscore, and must not
    start with a separator. That rules out the ``phone:<number>`` synthetic actor
    id verbatim - the colon is rejected:

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
        safe = "a" + safe
    base = "%s-%s" % (safe, day)
    # The 33-char pad is what satisfies AgentCore's minimum session-id length.
    return (base + "-" + "0" * 33)[:128]


# ---------------------------------------------------------------------------
# Runtime invocation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_STATUS_QUERY_RE = re.compile(
    r"(\bstatus\b|\bstatut\b|\bestado do caso\b|\bstate of\b|\bany news\b"
    r"|\bdes nouvelles\b|\balguma novidade\b|\bou en est\b)",
    re.IGNORECASE,
)

_EMAIL_QUERY_RE = re.compile(
    r"(send\s+(the\s+)?e[- ]?mail|envoyer?\s+(le\s+)?mail|enviar?\s+(o\s+)?e[- ]?mail"
    r"|e[- ]?mail\s+(to\s+the\s+)?contractor|mail\s+au\s+prestataire)",
    re.IGNORECASE,
)
def _live_case_block(identity: str, role: str) -> str:
    """Fetch the caller's current cases and format as an authoritative block.

    Used to make status answers deterministic: the model cannot contradict live
    data that is sitting in its prompt, even in a warm session that would
    otherwise parrot an earlier "PENDING_VALIDATION" reply.
    """
    if not CASES_TABLE:
        return ""
    try:
        from boto3.dynamodb.conditions import Key
        table = boto3.resource("dynamodb", region_name=_REGION).Table(CASES_TABLE)
        if role == "expert":
            resp = table.query(
                IndexName="status-index",
                KeyConditionExpression=Key("status").eq("PENDING_VALIDATION"),
            )
            items = resp.get("Items", [])
            if not items:
                return ""
            lines = ["[Live case data (authoritative, do not contradict):]",
                     "Pending validation queue:"]
            for c in items[:20]:
                lines.append("  - %s | %s | %s" % (c.get("case_id"), c.get("equipment"), c.get("status")))
            return "\n".join(lines)
        resp = table.query(
            IndexName="created_by-index",
            KeyConditionExpression=Key("created_by").eq("phone:%s" % identity.split(":")[-1].strip()),
        )
        items = sorted(resp.get("Items", []),
                       key=lambda i: i.get("created_at", 0), reverse=True)
        if not items:
            return "[Live case data (authoritative, do not contradict): no cases for this technician.]"
        lines = ["[Live case data (authoritative, do not contradict):]"]
        for c in items[:10]:
            lines.append("  - %s | %s | %s" % (c.get("case_id"), c.get("equipment"), c.get("status")))
        return "\n".join(lines)
    except Exception as err:  # noqa: BLE001
        logger.warning("live case fetch failed for %s: %s", identity, err)
        return ""


def _augmented_prompt(text: str, identity: str = "", role: str = "") -> str:
    """Append a per-turn directive + live data to status/email queries.

    Memory replays the whole session, so a later "status?" or "send the email"
    in a warm session is answered by imitating the model's own earlier wording
    instead of calling the tool. A directive attached to the CURRENT user
    message outranks the replayed history; the live data block makes the answer
    correct even if the model still refuses to call the tool.
    """
    is_status = bool(_STATUS_QUERY_RE.search(text))
    is_email = bool(_EMAIL_QUERY_RE.search(text))
    if not (is_status or is_email):
        return text
    parts = [text.rstrip()]
    if is_status:
        parts.append(
            "[System directive: this question requires live data. Call list_cases "
            "(and get_case when a case id is mentioned) BEFORE answering - earlier "
            "replies in this conversation may be stale. Report the concrete case id "
            "and its current status. Do not contradict the live case data below.]")
    if is_email:
        parts.append(
            "[System directive: to send the contractor e-mail, call "
            "send_contractor_email(case_id) - subject and body are OPTIONAL and "
            "default to the procedure stored on the case. Do NOT ask the user for "
            "the procedure or the subject; pass only case_id (and contractor_email "
            "if the user just supplied one). Do not contradict the live case data below.]")
    live = _live_case_block(identity, role)
    if live:
        parts.append(live)
    return "\n\n".join(parts)

def _invoke_runtime(actor_id: str, text: str, channel: str, role: str = "",
                    image_url: str = "", image_b64: str = None,
                    image_format: str = "jpeg") -> str:
    """Call the conformity agent and return its final reply text.

    The agent streams events; we concatenate the ``data`` deltas and prefer the
    final result payload when present. <thinking> blocks are already stripped by
    the runtime before streaming.
    """
    payload = json.dumps({
        "prompt": _augmented_prompt(text, actor_id, role),
        "image_url": image_url,
        "image_b64": image_b64 or "",
        "image_format": image_format,
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

      * ``runtimeUserId=actor_id`` â€” a first-class API parameter. AgentCore uses
        it to scope Memory, so conversation history is per person.
      * the allowlisted ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id``
        header â€” what the agent reads from ``context.request_headers`` (see
        ``patterns/conformity-agent/utils/auth.py``). boto3 does not expose
        arbitrary headers on this API, so it is injected by the module-level
        ``before-sign`` handler from ``_CURRENT_ACTOR``.

    The handler is registered at import, not here: re-registering it per call
    leaks handlers (``register_first`` returns None, so it cannot be
    unregistered) and the duplicate header breaks SigV4. This function only sets
    and resets the contextvar the handler reads.
    """
    token = _CURRENT_ACTOR.set(actor_id)
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
        _CURRENT_ACTOR.reset(token)


def _iter_sse_events(raw: str):
    """Yield the JSON payloads of a Strands AgentCore event stream.

    The runtime answers with Server-Sent Events, so each payload line is
    ``data: {json}`` â€” not a bare ``{json}`` line.

    The format below is what this deployment actually emits (captured from
    InvokeAgentRuntime against the live runtime):

        data: {"init_event_loop": true}
        data: {"start": true}
        data: {"event": {"contentBlockDelta": {"delta": {"text": " How"}}}}
        data: {"data": "How", "delta": {"text": " How"}, ...}   # delta event
        data: {"event": {"contentBlockDelta": {"delta": {"text": "!"}}}}
        data: {"data": "!", "delta": {"text": "!"}, ...}
        data: {"event": {"messageStop": {"stopReason": "end_turn"}}}
        data: {"message": {"role": "assistant", "content": [{"text": "..."}]}}
        data: {"result": "Hello! How can I assist you today?\\n"}  # terminal

    Two details matter and are easy to get wrong (both were observed breaking
    the reply):

      * ``data`` deltas are per-token and *drop leading whitespace* â€” a delta
        whose ``delta.text`` is ``" How"`` arrives as ``data: "How"``.
        Concatenating ``data`` therefore yields ``Hello!HowcanI...``. Use
        ``delta.text``, or prefer ``result``.
      * ``result`` is a plain **string** (not the ``{"stop_reason": ...}`` dict
        FAST's frontend parser expects) and holds the complete answer.

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


def _strip_thinking(text: str) -> str:
    """Remove ``<thinking>...</thinking>`` blocks from the model's output.

    Nova Pro emits its reasoning inline in the assistant text and the runtime
    does **not** strip it, so every reply arrives wrapped like::

        <thinking> The user wants ... </thinking>

        I need more information about the pump.

    That reasoning is for the operator, not the technician on WhatsApp, so it is
    dropped here â€” everything outside the tags is kept. An unterminated block
    (the streaming tail can cut one off) is dropped to the end of the string.
    """
    without_blocks = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    # A stray opening tag with no closer means the rest is still reasoning.
    without_blocks = re.sub(r"<thinking>.*\Z", "", without_blocks, flags=re.DOTALL)
    return without_blocks.strip()


def _extract_reply(raw: str) -> str:
    """Pull the assistant's final text out of the runtime's event stream.

    Preference order, most reliable first:

      1. ``{"result": "<full answer>"}`` â€” the terminal event. The runtime sends
         a plain **string** here (Fast's frontend parser looks for a
         ``{"stop_reason": ...}`` dict instead, so it misses it), and it is the
         complete, correctly-spaced answer.
      2. Streamed deltas, using ``delta.text`` â€” **not** ``data``. Both appear
         on each delta event, but ``data`` is the token with leading whitespace
         stripped (``" How"`` arrives as ``"How"``), so concatenating ``data``
         loses every word boundary: ``Hello!HowcanI...``.
      3. A complete ``message`` event â€” only when it is an **assistant** message.
         ``message`` events also carry *user* messages holding tool results, and
         splicing those into the reply would leak internal tool output to the
         technician.

    ``<thinking>`` blocks are stripped from whichever source wins.

    Raises ``RuntimeReplyError`` when the stream reports a failure and yields no
    text. The runtime signals mid-stream errors (e.g. the model producing an
    invalid tool-use sequence) with ``{"status": "error", "error": ...}`` /
    ``force_stop`` and no ``result``, which would otherwise be dispatched as an
    empty message to the technician.
    """
    result_text = ""
    streamed = ""
    messages_text = ""
    error_text = ""

    for event in _iter_sse_events(raw):
        if not isinstance(event, dict):
            continue

        # 1. Terminal result. A string is the whole answer; a dict (as FAST's
        #    frontend parser expects) carries no text and is ignored.
        if isinstance(event.get("result"), str) and event["result"].strip():
            result_text = event["result"]

        # 2. Streamed assistant text deltas, spaced correctly.
        delta = event.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            streamed += delta["text"]

        # 3. Complete assistant message (fallback for a non-streaming runtime).
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    messages_text += block["text"]

        # 4. Mid-stream failure, recorded in case no text arrives.
        if event.get("status") == "error" and isinstance(event.get("error"), str):
            error_text = event["error"]

    reply = _strip_thinking(result_text or streamed or messages_text)
    if not reply:
        raise RuntimeReplyError(error_text or "the agent returned an empty reply")
    return reply


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
        if not (message.is_text or message.is_image):
            logger.error("Unsupported message type: %s", message.message_type)
            continue

        # Claim the provider message id before doing any work: SNS retries and
        # provider redeliveries would otherwise send the same SMS reply twice.
        if not dedupe.claim(message.message_id):
            logger.info("duplicate %s (%s) from %s ignored",
                        message.message_id, message.channel, message.sender)
            continue

        # Image attached to this turn, or carried over from a recent image-only
        # turn (the agent's memory is text-only, so re-attach the photo).
        image_url = message.image_url
        if not image_url and message.is_text:
            image_url = _latest_image_url(message.identity)

        image_b64, image_fmt = _download_image(image_url) if image_url else (None, None)

        actor_id, role = _identity_for(message.identity)
        logger.info("on %s resolved %s as %s (actor=%s)%s",
                    message.channel, message.sender, role, actor_id,
                    " [image]" if image_url else "")
        transcript.record(message.identity, "user", message.text, message.channel,
                          actor_id=actor_id, message_id=message.message_id,
                          image_url=image_url)

        try:
            reply_text = _invoke_runtime(actor_id, message.text, message.channel, role=role,
                                         image_url=image_url, image_b64=image_b64,
                                         image_format=image_fmt)
        except RuntimeReplyError as err:
            # The runtime ran but produced no usable answer (e.g. Nova Pro's
            # intermittent invalid-tool-use stream error). Say so plainly rather
            # than sending an empty message, and log the model's own reason.
            logger.error("Runtime produced no reply for %s: %s", actor_id, err)
            reply_text = (
                "Sorry, I could not process that just now. "
                "Please send it again in a moment."
            )
        except Exception:
            logger.exception("Agent invocation failed for %s", actor_id)
            reply_text = "Sorry, something went wrong while handling your message."

        try:
            dispatch_result = dispatcher.send_reply(
                channel=message.channel,
                sender=message.identity,
                text=reply_text,
                phone_number_id=message.channel_meta.get("phone_number_id", ""),
                message_id=message.message_id,
                origination_number=message.recipient,
            )
        except Exception as err:
            # A delivery failure (e.g. sandbox destination not verified) must not
            # fail the whole SNS batch: the agent already answered, and SNS would
            # redeliver every record in the batch.
            logger.exception("Dispatch failed on %s to %s", message.channel, message.identity)
            dispatch_result = {"error": f"{type(err).__name__}: {err}"}

        # One stable line per answered turn: ops/debugging, and the phone
        # emulator can show a reply even before the transcript table is read.
        logger.info("REPLY %s %s: %s", message.channel, message.identity, reply_text)
        transcript.record(message.identity, "assistant", reply_text, message.channel,
                          actor_id=actor_id, message_id=message.message_id,
                          dispatch=dispatch_result)
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
