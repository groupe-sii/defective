import json
import logging
import os
import time

import boto3
from strands import tool

from tools.case import STATUS_APPROVED, STATUS_NOTIFIED, _load_case, _table

logger = logging.getLogger(__name__)

SENDER_IDENTITY = os.environ.get("CONTRACTOR_EMAIL_FROM", "noreply@example.com")
dynamodb_client = boto3.client("sesv2")  # only used when MOCK_EMAIL != true


@tool
def send_contractor_email(case_id: str, subject: str, body: str) -> str:
    """
        Send the procedure e-mail to the equipment contractor/provider.

        Only allowed once the case status is APPROVED by the conformity expert;
        the recipient is the contractor e-mail stored on the case (it cannot be
        overridden here). In mock mode (default) the full e-mail is logged
        instead of being sent, and the case still moves to CONTRACTOR_NOTIFIED.

        Args:
            case_id: The approved case the e-mail refers to.
            subject: E-mail subject, include the case id.
            body: E-mail body with the final validated procedure.
    """
    case = _load_case(case_id)
    if case is None:
        return json.dumps({"error": f"case {case_id} not found"})
    if case.get("status") not in (STATUS_APPROVED, STATUS_NOTIFIED):
        return json.dumps({
            "error": "refused: case is not APPROVED by the conformity expert "
                     f"(status={case.get('status')}). Validation must happen first."
        })
    to_email = case.get("contractor_email")
    if not to_email:
        return json.dumps({"error": "case has no contractor e-mail recorded; "
                                    "ask the technician for it and update the case"})

    mocked = os.getenv("MOCK_EMAIL", "true").lower() == "true"
    now = int(time.time())

    if mocked:
        logger.info("MOCK_EMAIL case=%s mode=mock to=%s subject=%s\n--- body ---\n%s\n------------",
                    case_id, to_email, subject, body)
        mode = "mock"
    else:
        try:
            dynamodb_client.send_email(
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
