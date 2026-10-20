import json
import logging
import os

from strands import tool

from tools.case import STATUS_PENDING, _load_case

logger = logging.getLogger(__name__)

EXPERT_CONTACTS = os.environ.get("EXPERT_CONTACTS", "")


@tool
def request_expert_validation(case_id: str, summary: str = "") -> str:
    """
        Notify the conformity expert that a case awaits validation of its draft
        procedure. Call right after create_case.

        Args:
            case_id: The case awaiting validation.
            summary: One-line summary of the draft procedure for the expert.

        Returns JSON confirming the notification (currently delivered through the
        demo mock channel; the expert then replies in their own chat with the agent).
    """
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
    # Demo (Phase 1): the notification channel to the expert is mocked - the
    # real outbound push (WhatsApp template / SMS) arrives with Phase 3 sends.
    logger.info("NOTIFY_EXPERT (mock): contacts=%s :: %s", EXPERT_CONTACTS, ping)

    return json.dumps({
        "case_id": case_id,
        "notified": True,
        "mode": "mock",
        "expert_contacts": EXPERT_CONTACTS,
        "message": ping,
    })
