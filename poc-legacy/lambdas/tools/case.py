import json
import logging
import os
import time
import uuid
from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Key
from strands import tool

from tools.context import get_sender

logger = logging.getLogger(__name__)

CASES_TABLE = os.environ["CASES_TABLE"]
_dynamodb = boto3.resource("dynamodb")

STATUS_PENDING = "PENDING_VALIDATION"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_NOTIFIED = "CONTRACTOR_NOTIFIED"


def _jdump(value) -> str:
    return json.dumps(value, default=str)


def _table():
    return _dynamodb.Table(CASES_TABLE)


def _new_case_id() -> str:
    day = datetime.utcnow().strftime("%Y%m%d")
    return f"NC-{day}-{uuid.uuid4().hex[:6]}"


def _clean(item: dict) -> dict:
    return {k: v for k, v in item.items() if v not in (None, "")}


def _load_case(case_id: str):
    resp = _table().get_item(Key={"case_id": case_id})
    return resp.get("Item")


def _visible_to_sender(case: dict) -> bool:
    sender = get_sender()
    if sender["role"] == "expert":
        return True
    return case.get("created_by") == sender.get("identity")


@tool
def create_case(equipment: str, description: str, procedure: str,
                contractor_email: str = "", location: str = "",
                urgency: str = "", kb_references: str = "") -> str:
    """
        Register a new non-conformity case with a DRAFT procedure that awaits
        expert validation. Call after search_ncr_history and once the draft
        procedure and contractor e-mail are known.

        Args:
            equipment: Equipment id/type, e.g. "centrifugal pump P-101".
            description: Observed non-conformity, what/where/when.
            procedure: The draft procedure as numbered steps.
            contractor_email: Contractor/provider e-mail that will receive the procedure.
            location: Where the equipment is (optional).
            urgency: low / medium / high (optional).
            kb_references: Comma-separated refs/sources of the similar reports used (optional).

        Returns JSON with the generated case_id and status.
    """
    sender = get_sender()
    case_id = _new_case_id()
    item = _clean(
        {
            "case_id": case_id,
            "created_by": sender.get("identity"),
            "channel": sender.get("channel"),
            "equipment": equipment,
            "description": description,
            "procedure": procedure,
            "contractor_email": contractor_email,
            "location": location,
            "urgency": urgency,
            "kb_references": kb_references,
            "status": STATUS_PENDING,
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )
    try:
        _table().put_item(Item=item)
    except Exception as err:
        logger.error("create_case failed: %s", err)
        return _jdump({"error": f"failed to create case: {err}"})
    logger.info("Case created: %s by %s", case_id, sender.get("identity"))
    return _jdump({"case_id": case_id, "status": STATUS_PENDING,
                   "created_by": sender.get("identity")})


@tool
def get_case(case_id: str) -> str:
    """
        Get a non-conformity case by id: status, draft procedure, validation
        decision and contractor e-mail state. Technicians only see their own
        cases; the conformity expert can see any case.
    """
    case = _load_case(case_id)
    if case is None:
        return _jdump({"error": f"case {case_id} not found"})
    if not _visible_to_sender(case):
        return _jdump({"error": f"case {case_id} not found"})
    return _jdump(case)


@tool
def list_cases(status: str = "") -> str:
    """
        List non-conformity cases.

        For the conformity expert (session sender_role=expert): pass
        status="PENDING_VALIDATION" to get the validation queue; with no
        status it returns that same queue.

        For a technician: returns their own cases (optionally filtered by status).
    """
    sender = get_sender()
    try:
        if sender["role"] == "expert":
            status = status or STATUS_PENDING
            resp = _table().query(IndexName="status-index",
                                  KeyConditionExpression=Key("status").eq(status))
        else:
            kwargs = {"IndexName": "created_by-index",
                      "KeyConditionExpression": Key("created_by").eq(sender.get("identity"))}
            if status:
                kwargs["KeyConditionExpression"] &= Key("status").eq(status)
            resp = _table().query(**kwargs)
        items = sorted(resp.get("Items", []), key=lambda i: i.get("created_at", 0), reverse=True)
    except Exception as err:
        logger.error("list_cases failed: %s", err)
        return _jdump({"error": f"failed to list cases: {err}"})
    summary = [
        _clean({k: c.get(k) for k in ("case_id", "equipment", "status", "created_at",
                                      "created_by", "urgency")})
        for c in items
    ]
    return _jdump({"count": len(summary), "cases": summary})


@tool
def record_validation(case_id: str, decision: str, comment: str = "") -> str:
    """
        EXPERT ONLY: record the conformity expert's validation decision on a case.

        Args:
            case_id: The case being validated.
            decision: "APPROVED" or "REJECTED".
            comment: The expert's justification/comment (optional but recommended).

        Returns JSON with the updated case status, or an error if the current
        sender is not a configured expert or the case is not awaiting validation.
    """
    sender = get_sender()
    if sender.get("role") != "expert":
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
        "approver": sender.get("identity"),
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
    logger.info("Case %s %s by expert %s", case_id, decision, sender.get("identity"))
    return _jdump({"case_id": case_id, "status": decision,
                   "approver": sender.get("identity"), "comment": comment})
