"""Case lifecycle tools — AgentCore Gateway Lambda target.

Ported from the POC's ``lambdas/tools/case.py``. Implements four tools over the
``NonConformityCases`` DynamoDB table:

    create_case        — register a draft case (status PENDING_VALIDATION)
    get_case           — read one case (technicians: own cases only)
    list_cases         — validation queue (expert) / own cases (technician)
    record_validation  — EXPERT ONLY: APPROVED / REJECTED

ROLE ENFORCEMENT (replaces the POC's ``tools/context.py`` module global):

  Layer 1 — Cedar policy at the Gateway. The action
    ``case___record_validation`` is permitted only when
    ``principal.getTag("role") == "expert"``. A denied tool is not even listed to
    the agent, and Cedar cannot be bypassed by prompt injection.
  Layer 2 — this Lambda re-checks the caller's role. The identity arrives as the
    ``_caller_identity`` tool argument, injected by the Gateway request
    interceptor (``gateway/interceptor/interceptor_lambda.py``) from the
    authenticated M2M JWT. The role is then re-derived from the contacts table
    keyed on that identity; the JWT's ``role`` claim is only a fallback. Defence
    in depth: if the policy were misconfigured, the tool still refuses
    non-experts.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
"""

import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CASES_TABLE = os.environ["CASES_TABLE"]
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "")
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
_dynamodb = boto3.resource("dynamodb", region_name=_REGION)

STATUS_PENDING = "PENDING_VALIDATION"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_NOTIFIED = "CONTRACTOR_NOTIFIED"


def _jdump(value) -> str:
    return json.dumps(value, default=str)


def _table():
    return _dynamodb.Table(CASES_TABLE)


def _new_case_id() -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"NC-{day}-{uuid.uuid4().hex[:6]}"


def _clean(item: dict) -> dict:
    return {k: v for k, v in item.items() if v not in (None, "")}


def _load_case(case_id: str):
    resp = _table().get_item(Key={"case_id": case_id})
    return resp.get("Item")


# ---------------------------------------------------------------------------
# Caller identity / role — read from the Gateway context, fall back to DynamoDB
# ---------------------------------------------------------------------------

def _caller() -> dict:
    """Caller identity for the current tool invocation.

    ``identity`` comes from the Gateway request interceptor, which copies the
    authenticated JWT's ``user_id`` claim into the tool arguments as
    ``_caller_identity`` (see ``gateway/interceptor/interceptor_lambda.py``).
    The model never supplies it: the interceptor overwrites the key after the
    model's arguments arrive, and ``handler`` pops it before the tool runs.

    ``role`` is then re-derived from the contacts table keyed on that identity.
    That table is the authority — the JWT's ``role`` claim (also injected, as
    ``_caller_role``) is only a fallback for when the lookup fails or the row is
    missing. So a stale or tampered claim can never grant expert rights that the
    table does not.

    Returns ``{"identity": ..., "role": ...}``. Role defaults to "technician".
    """
    custom = _GATEWAY_CONTEXT.get() or {}
    identity = str(custom.get("user_id", "") or "")
    claimed_role = str(custom.get("role", "") or "").lower()

    if identity and CONTACTS_TABLE:
        try:
            item = _dynamodb.Table(CONTACTS_TABLE).get_item(
                Key={"actor_id": identity}
            ).get("Item") or {}
            role = str(item.get("role", "") or "").lower()
            if role:
                return {"identity": identity, "role": role}
        except Exception as err:
            logger.error("contacts lookup failed for %s: %s", identity, err)

    return {"identity": identity, "role": claimed_role or "technician"}


# Set by the handler before dispatching to a tool (contextvar so concurrent
# invocations in a reused container never share it).
_GATEWAY_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "gateway_context", default={}
)


def _visible_to_caller(case: dict, caller: dict) -> bool:
    if caller.get("role") == "expert":
        return True
    return case.get("created_by") == caller.get("identity")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def create_case(equipment: str, description: str, procedure: str,
                contractor_email: str = "", location: str = "",
                urgency: str = "", kb_references: str = "") -> str:
    """Register a new non-conformity case with a DRAFT procedure."""
    caller = _caller()
    case_id = _new_case_id()
    now = int(time.time())
    item = _clean(
        {
            "case_id": case_id,
            "created_by": caller.get("identity"),
            "channel": caller.get("channel", ""),
            "equipment": equipment,
            "description": description,
            "procedure": procedure,
            "contractor_email": contractor_email,
            "location": location,
            "urgency": urgency,
            "kb_references": kb_references,
            "status": STATUS_PENDING,
            "created_at": now,
            "updated_at": now,
        }
    )
    try:
        _table().put_item(Item=item)
    except Exception as err:
        logger.error("create_case failed: %s", err)
        return _jdump({"error": f"failed to create case: {err}"})
    logger.info("Case created: %s by %s", case_id, caller.get("identity"))
    return _jdump({"case_id": case_id, "status": STATUS_PENDING,
                   "created_by": caller.get("identity")})


def get_case(case_id: str) -> str:
    """Get a non-conformity case by id."""
    caller = _caller()
    case = _load_case(case_id)
    if case is None:
        return _jdump({"error": f"case {case_id} not found"})
    if not _visible_to_caller(case, caller):
        return _jdump({"error": f"case {case_id} not found"})
    return _jdump(case)


def list_cases(status: str = "") -> str:
    """List non-conformity cases (validation queue for experts)."""
    caller = _caller()
    try:
        if caller.get("role") == "expert":
            status = status or STATUS_PENDING
            resp = _table().query(IndexName="status-index",
                                  KeyConditionExpression=Key("status").eq(status))
            items = resp.get("Items", [])
            note = ""
        else:
            # Query by created_by ONLY. The created_by-index has a single key
            # attribute, so adding `status` to KeyConditionExpression makes
            # DynamoDB reject the query (ValidationException) - observed live:
            # the model passed status=PENDING_VALIDATION, the tool errored,
            # and the model then answered "status" from memory. Filter the
            # status in Python instead.
            resp = _table().query(
                IndexName="created_by-index",
                KeyConditionExpression=Key("created_by").eq(caller.get("identity")))
            items = resp.get("Items", [])
            note = ""
            if status:
                filtered = [c for c in items if c.get("status") == status]
                if filtered:
                    items = filtered
                elif items:
                    # A guessed status filter that matches nothing must not
                    # hide the real answer: fall back to all of the caller's
                    # cases and say so.
                    note = (f"no case has status {status}; "
                            "all of your cases are listed with their current status")
        items = sorted(items,
                       key=lambda i: i.get("created_at", 0), reverse=True)
    except Exception as err:
        logger.error("list_cases failed: %s", err)
        return _jdump({"error": f"failed to list cases: {err}"})
    summary = [
        _clean({k: c.get(k) for k in ("case_id", "equipment", "status", "created_at",
                                      "created_by", "urgency")})
        for c in items
    ]
    payload = {"count": len(summary), "cases": summary}
    if note:
        payload["note"] = note
    return _jdump(payload)


def record_validation(case_id: str, decision: str, comment: str = "") -> str:
    """EXPERT ONLY: record the conformity expert's validation decision."""
    caller = _caller()
    if caller.get("role") != "expert":
        return _jdump({"error": "forbidden: only the configured conformity expert "
                                "can validate cases"})
    decision = (decision or "").strip().upper()
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        return _jdump({"error": "decision must be APPROVED or REJECTED"})
    case = _load_case(case_id)
    if case is None:
        return _jdump({"error": f"case {case_id} not found"})
    if case.get("status") != STATUS_PENDING:
        return _jdump({"error": f"case {case_id} is not awaiting validation "
                                f"(status={case.get('status')})"})

    now = int(time.time())
    case["status"] = decision
    case["validation"] = {
        "approver": caller.get("identity"),
        "decision": decision,
        "comment": comment,
        "at": now,
    }
    case["updated_at"] = now
    try:
        _table().put_item(Item=case)
    except Exception as err:
        logger.error("record_validation failed: %s", err)
        return _jdump({"error": f"failed to record validation: {err}"})
    logger.info("Case %s %s by expert %s", case_id, decision, caller.get("identity"))
    return _jdump({"case_id": case_id, "status": decision,
                   "approver": caller.get("identity"), "comment": comment})


TOOLS = {
    "create_case": create_case,
    "get_case": get_case,
    "list_cases": list_cases,
    "record_validation": record_validation,
}


def handler(event, context):
    """Gateway Lambda target handler for the case lifecycle tools."""
    logger.info("Received event: %s", json.dumps(event))
    try:
        custom = context.client_context.custom
        original = custom["bedrockAgentCoreToolName"]
        tool_name = original[original.index("___") + 3:]

        # The Gateway request interceptor injects the authenticated caller's
        # identity as arguments. Pop them so they never reach the tool
        # signatures, and expose them — plus the Gateway's own context — to the
        # tools through a task-local contextvar.
        injected = {
            "user_id": str(event.pop("_caller_identity", "") or ""),
            "role": str(event.pop("_caller_role", "") or "").lower(),
        }
        _GATEWAY_CONTEXT.set(
            {**dict(custom), **{k: v for k, v in injected.items() if v}}
        )

        fn = TOOLS.get(tool_name)
        if fn is None:
            logger.error("Unexpected tool name: %s", tool_name)
            return {"error": f"This Lambda does not implement '{tool_name}'"}

        result = fn(**event)
        return {"content": [{"type": "text", "text": result}]}
    except Exception as err:
        logger.error("Error processing request: %s", err)
        return {"error": f"Internal server error: {err}"}
