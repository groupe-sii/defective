"""Outbound notification tools — AgentCore Gateway Lambda target.

Ported from the POC's ``lambdas/tools/mail.py`` and ``lambdas/tools/notify.py``:

    send_contractor_email     — mail the validated procedure to the contractor
                                (approval-gated; MOCK_EMAIL logs instead of SES)
    request_expert_validation — ping the conformity expert that a draft awaits
                                validation (mock channel)

Both tools read/write the same ``NonConformityCases`` table as the case tools.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CASES_TABLE = os.environ["CASES_TABLE"]
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
SENDER_IDENTITY = os.environ.get("CONTRACTOR_EMAIL_FROM", "noreply@example.com")
EXPERT_CONTACTS = os.environ.get("EXPERT_CONTACTS", "")
TRANSCRIPT_TABLE = os.environ.get("TRANSCRIPT_TABLE", "")

_dynamodb = boto3.resource("dynamodb", region_name=_REGION)

STATUS_PENDING = "PENDING_VALIDATION"
STATUS_APPROVED = "APPROVED"
STATUS_NOTIFIED = "CONTRACTOR_NOTIFIED"


def _transcript_table():
    if not TRANSCRIPT_TABLE:
        return None
    return _dynamodb.Table(TRANSCRIPT_TABLE)


def _table():
    return _dynamodb.Table(CASES_TABLE)


def _load_case(case_id: str):
    return _table().get_item(Key={"case_id": case_id}).get("Item")


def send_contractor_email(case_id: str, subject: str = "", body: str = "",
                           contractor_email: str = "") -> str:
    """Send the validated procedure e-mail to the equipment contractor.

    ``contractor_email`` is optional: it lets the technician supply the address
    late (after case creation). It is persisted on the case before sending, so
    follow-up calls and status checks see it.

    ``subject`` and ``body`` are optional too: when omitted they are derived
    from the case (subject = "Case <id>: <equipment>", body = the stored
    procedure). This keeps the tool usable even when the agent does not have
    the procedure text in its current context.
    """
    case = _load_case(case_id)
    if case is None:
        return json.dumps({"error": f"case {case_id} not found"})
    if case.get("status") not in (STATUS_APPROVED, STATUS_NOTIFIED):
        return json.dumps({
            "error": "refused: case is not APPROVED by the conformity expert "
                     f"(status={case.get('status')}). Validation must happen first."
        })
    to_email = case.get("contractor_email") or (contractor_email or "").strip()
    if not to_email:
        return json.dumps({"error": "case has no contractor e-mail recorded; "
                                    "ask the technician for it and call again "
                                    "with it as contractor_email"})
    case["contractor_email"] = to_email

    if not subject:
        subject = f"Case {case_id}: {case.get('equipment', 'non-conformity')}"
    if not body:
        body = case.get("procedure") or "(no procedure recorded)"

    mocked = os.getenv("MOCK_EMAIL", "true").lower() == "true"
    now = int(time.time())

    if mocked:
        logger.info("MOCK_EMAIL case=%s mode=mock to=%s subject=%s\n--- body ---\n%s\n------------",
                    case_id, to_email, subject, body)
        mode = "mock"
    else:
        try:
            boto3.client("sesv2", region_name=_REGION).send_email(
                FromEmailAddress=SENDER_IDENTITY,
                Destination={"ToAddresses": [to_email]},
                Content={"Simple": {"Subject": {"Data": subject},
                                    "Body": {"Text": {"Data": body}}}},
            )
            mode = "ses"
        except Exception as err:
            logger.error("SES send failed for case %s: %s", case_id, err)
            return json.dumps({"error": f"failed to send e-mail: {err}"})

    case["status"] = STATUS_NOTIFIED
    case["contractor_notification"] = {"mode": mode, "subject": subject,
                                       "to": to_email, "at": now}
    case["updated_at"] = now
    try:
        _table().put_item(Item=case)
    except Exception as err:
        logger.error("Failed to update case %s after e-mail: %s", case_id, err)

    return json.dumps({"case_id": case_id, "sent": True, "mode": mode,
                       "to": to_email, "subject": subject,
                       "status": STATUS_NOTIFIED})


def request_expert_validation(case_id: str, summary: str = "") -> str:
    """Notify the conformity expert that a case awaits validation."""
    case = _load_case(case_id)
    if case is None:
        return json.dumps({"error": f"case {case_id} not found"})
    if case.get("status") != STATUS_PENDING:
        return json.dumps({"error": f"case {case_id} is not awaiting validation "
                                    f"(status={case.get('status')})"})

    ping = (
        f"Case {case_id} awaits your validation "
        f"({case.get('equipment', 'equipment')}). {summary}\n"
        f"Reply in your chat with: approve {case_id} "
        f"or reject {case_id} <reason>"
    )
    if case.get("image_url"):
        ping += f"\nImage: {case.get('image_url')}"
    # The push to the expert is mocked (same policy as the POC): the real
    # outbound notification arrives when the channel is provisioned.
    logger.info("NOTIFY_EXPERT (mock): contacts=%s :: %s", EXPERT_CONTACTS, ping)

    # Mirror the (mocked) push into the transcript table so the expert's phone
    # in the demo emulator lights up with it. In production the real push is
    # delivered by the channel and this write is unnecessary.
    ttable = _transcript_table()
    if ttable is not None:
        from datetime import datetime, timezone, timedelta
        ts = datetime.now(timezone.utc).isoformat()
        for raw in (EXPERT_CONTACTS or "").split(","):
            expert_id = raw.strip()
            if not expert_id:
                continue
            try:
                ttable.put_item(Item={
                    "identity": expert_id,
                    "ts": f"{ts}#assistant",
                    "role": "assistant",
                    "channel": "sms",
                    "text": ping,
                    "created_at": ts,
                    "expires_at": int((datetime.now(timezone.utc) +
                                       timedelta(days=7)).timestamp()),
                })
            except Exception as err:  # noqa: BLE001 - notification must not fail the tool
                logger.warning("transcript mirror failed for %s: %s", expert_id, err)

    return json.dumps({
        "case_id": case_id,
        "notified": True,
        "mode": "mock",
        "expert_contacts": EXPERT_CONTACTS,
        "message": ping,
    })


TOOLS = {
    "send_contractor_email": send_contractor_email,
    "request_expert_validation": request_expert_validation,
}


def handler(event, context):
    """Gateway Lambda target handler for the notification tools."""
    logger.info("Received event: %s", json.dumps(event))
    try:
        original = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = original[original.index("___") + 3:]
        fn = TOOLS.get(tool_name)
        if fn is None:
            logger.error("Unexpected tool name: %s", tool_name)
            return {"error": f"This Lambda does not implement '{tool_name}'"}

        # Strip the caller-identity arguments added by the Gateway request
        # interceptor (gateway/interceptor/interceptor_lambda.py). These tools
        # need no identity, and they would otherwise break fn(**event).
        event.pop("_caller_identity", None)
        event.pop("_caller_role", None)

        result = fn(**event)
        return {"content": [{"type": "text", "text": result}]}
    except Exception as err:
        logger.error("Error processing request: %s", err)
        return {"error": f"Internal server error: {err}"}
