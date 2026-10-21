import json
import boto3
import logging
import time
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger()


def normalize_phone(raw: str) -> str:
    """Canonical phone identity: digits only, no '+'. Keeps history shared
    between channels (WhatsApp and SMS from the same number)."""
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _parse_webhook_entry(raw) -> Dict:
    """Decode `whatsAppWebhookEntry` into the Meta webhook body.

    Per the AWS End User Messaging Social docs, `whatsAppWebhookEntry` arrives as
    a *JSON string* ("The JSON received from WhatsApp in the
    `whatsAppWebhookEntry` is received as a JSON string and can be converted to
    JSON"). Test fixtures often pass it already decoded, so accept both a string
    and a mapping — anything else is treated as an empty webhook.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("whatsAppWebhookEntry is not valid JSON: %s", raw[:200])
    return {}


def _extract_changes(webhook_entry: Dict) -> list:
    """Flatten `changes` out of a Meta webhook body.

    The real body nests them one level down, under `entry[]`:

        {"object": "whatsapp_business_account",
         "entry": [{"id": "...", "changes": [{"field": "messages", ...}]}]}

    The pre-AgentCore POC's test fixture (`poc-legacy/mock_send.py`) used a
    flattened `{"changes": [...]}` shape instead. Accept both so real WhatsApp
    traffic and the existing fixtures both parse.
    """
    if not isinstance(webhook_entry, dict):
        return []
    changes = webhook_entry.get("changes")
    if isinstance(changes, list):
        return changes

    flattened = []
    for entry in webhook_entry.get("entry", []) or []:
        if isinstance(entry, dict):
            flattened.extend(entry.get("changes", []) or [])
    return flattened


class WhatsappMessage:
    def __init__(
        self,
        meta_phone_number: Dict,
        message: Dict,
        metadata: Optional[Dict] = None,
        meta_api_version: str = "v20.0",
    ) -> None:
        if metadata is None:
            metadata = {}
        self.meta_phone_number = meta_phone_number or {}
        self.phone_number_arn = self.meta_phone_number.get("arn", "")
        self.meta_phone_number_id = self.meta_phone_number.get("metaPhoneNumberId", "")
        self.metadata = metadata
        self.message = message
        # Prefer the ARN-derived id (what `send_whatsapp_message` needs). When the
        # SNS context has no matching MetaPhoneNumberIds entry — e.g. a test
        # fixture, or a WABA whose numbers are not yet mapped — fall back to the
        # Meta phone_number_id carried in the webhook metadata so replies can
        # still be addressed.
        self.phone_number_id = (
            self.phone_number_arn.split(":")[-1].replace("/", "-")
            or str(metadata.get("phone_number_id", ""))
        )
        self.timestamp = message.get("timestamp", "")
        self.phone_number = message.get("from", "")
        # Channel-agnostic aliases: the normalizer (utils/messages.py) uses
        # these, so channel-specific code stays in this class only.
        self.sender = normalize_phone(self.phone_number)
        self.message_type = message.get("type", "")
        self.meta_api_version = meta_api_version
        self.message_id = message.get("id", "")
    
    def get_text(self) -> str:
        """Retrieve the text body from the message."""
        return self.message.get("text", {}).get("body", "")
    
    def build_whatsapp_row(self, phone_number, messages, role, meta_phone_number_id, 
                           id, phone_number_id, timestamp, system_prompt):
        try:
            current_day = datetime.now().strftime("%Y/%m/%d")
            return {
                    "phone_number": phone_number,
                    "session_time": int(time.time()),
                    "day": current_day,
                    "meta_phone_number_id": meta_phone_number_id,
                    "id": id,
                    "phone_number_id": phone_number_id,
                    "timestamp": timestamp,
                    "messages": messages,
                    "system_prompt": system_prompt
            }
        except Exception as e:
            logger.error(f"Error building WhatsApp row: {str(e)}")
            raise


class WhatsappService:
    def __init__(self, sns_message: Dict, client: Optional[boto3.client] = None) -> None:
        self.context = sns_message.get("context", {})
        self.meta_phone_number_ids = self.context.get("MetaPhoneNumberIds", [])
        self.meta_waba_ids = self.context.get("MetaWabaIds", [])
        self.webhook_entry = _parse_webhook_entry(sns_message.get("whatsAppWebhookEntry"))
        self.message_timestamp = sns_message.get("message_timestamp", "")
        self.changes = _extract_changes(self.webhook_entry)
        self.messages = []
        self.client = client if client else boto3.client("socialmessaging")

        for change in self.changes:
            value = change.get("value", {})
            field = change.get("field", "")
            if field == "messages":
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id", "")
                phone_number = self.get_phone_number_arn(phone_number_id)
                for message in value.get("messages", []):
                    self.messages.append(
                        WhatsappMessage(phone_number or {}, message, metadata)
                    )
            else:
                logger.info(f"Unhandled field: {field}")

    def get_phone_number_arn(self, phone_number_id: str) -> Optional[Dict]:
        """Retrieve the phone number ARN based on the phone number ID."""
        for phone_number in self.meta_phone_number_ids:
            if phone_number.get("metaPhoneNumberId") == phone_number_id:
                return phone_number
        return None