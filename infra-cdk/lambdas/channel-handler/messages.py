"""Channel-agnostic inbound message model.

An SNS record can carry a WhatsApp webhook (whatsAppWebhookEntry) or an SMS
payload (originationNumber/messageBody). `parse_sns_message` detects the shape
and returns a list of InboundMessage, so nothing downstream needs to know which
channel a message arrived on.

History is keyed by the channel-agnostic identity (digits only, no "+"), so the
same human talking over WhatsApp and SMS shares one conversation.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"
CHANNEL_RCS = "rcs"


@dataclass
class InboundMessage:
    """One inbound message, normalized across channels."""

    channel: str
    sender: str                      # E.164 digits, no "+" (identity key)
    text: str
    message_id: str = ""
    recipient: str = ""              # your number/WABA the message was sent to
    message_type: str = "text"
    image_url: str = ""              # optional attached media (presigned S3 URL)
    channel_meta: Dict = field(default_factory=dict)

    @property
    def identity(self) -> str:
        """Canonical conversation key: same for WhatsApp and SMS."""
        return normalize_identity(self.sender)

    @property
    def is_text(self) -> bool:
        return self.message_type == "text"

    @property
    def is_image(self) -> bool:
        return self.message_type == "image"


def normalize_identity(raw: str) -> str:
    """Digits-only, no '+'. '+55 11 98765-4321' and '5511987654321' collapse."""
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _whatsapp_messages(sns_message: Dict) -> List[InboundMessage]:
    """Parse the WhatsApp webhook envelope (unchanged shape from the sample)."""
    from whatsapp import WhatsappService

    service = WhatsappService(sns_message)
    return [
        InboundMessage(
            channel=CHANNEL_WHATSAPP,
            sender=normalize_identity(m.sender),
            text=m.get_text(),
            message_id=m.message_id,
            # NOT normalized: this is the AWS origination id
            # (`phone-number-id-...`), an opaque identifier passed straight to
            # `send_whatsapp_message(originationPhoneNumberId=...)`. Stripping
            # non-digits would corrupt it.
            recipient=m.phone_number_id,
            message_type=m.message_type,
            channel_meta={
                "meta_phone_number_id": m.meta_phone_number_id,
                "phone_number_id": m.phone_number_id,
                "meta_phone_number": m.meta_phone_number,
                "timestamp": m.timestamp,
            },
        )
        for m in service.messages
    ]


def _sms_messages(sns_message: Dict) -> List[InboundMessage]:
    """Parse the AWS End User Messaging SMS inbound payload.

    Top-level keys per the End User Messaging docs:
    originationNumber (sender), destinationNumber (our number), messageBody,
    inboundMessageId, messageKeyword, previousPublishedMessageId.
    """
    body = sns_message.get("messageBody")
    image_url = (sns_message.get("imageUrl") or sns_message.get("image_url")
                 or sns_message.get("mediaUrl") or sns_message.get("media_url") or "")
    if body is None and not image_url:
        logger.error("SMS payload without messageBody: %s", sns_message)
        return []
    if body is None:
        body = ""
    return [
        InboundMessage(
            channel=CHANNEL_SMS,
            sender=normalize_identity(sns_message.get("originationNumber")),
            text=str(body),
            message_id=str(sns_message.get("inboundMessageId") or ""),
            recipient=normalize_identity(sns_message.get("destinationNumber")),
            message_type="image" if image_url else "text",
            image_url=str(image_url),
            channel_meta={
                "keyword": sns_message.get("messageKeyword"),
                "media_content_type": sns_message.get("mediaContentType")
                                     or sns_message.get("media_content_type"),
            },
        )
    ]


def detect_channel(sns_message: Dict) -> Optional[str]:
    if sns_message.get("whatsAppWebhookEntry"):
        return CHANNEL_WHATSAPP
    if sns_message.get("originationNumber") and sns_message.get("inboundMessageId"):
        return CHANNEL_SMS
    return None


def parse_sns_message(message: Dict) -> List[InboundMessage]:
    """Normalize one parsed SNS message body into InboundMessage objects."""
    channel = detect_channel(message)
    try:
        if channel == CHANNEL_WHATSAPP:
            return _whatsapp_messages(message)
        if channel == CHANNEL_SMS:
            return _sms_messages(message)
    except Exception as err:
        logger.error("Failed to parse %s inbound message: %s", channel, err)
        return []
    logger.error("Unrecognized inbound message shape: %s", json.dumps(message, default=str)[:500])
    return []
