"""Outbound reply dispatcher.

Replaces the WhatsApp-only `text_reply`. One entry point per channel, all
honoring the same mock policy:

    MOCK_CHANNELS (CSV, default "whatsapp,sms,email,rcs")
      - channel listed   -> log the reply (no send, no AWS dependency)
      - channel absent   -> really send

Each channel flips independently, so real SMS can go live while WhatsApp stays
mocked. Channel support ranges from real (WhatsApp, SMS) to stubbed (RCS), and
the stubs are intentionally small placeholders for Phase 3.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"
CHANNEL_RCS = "rcs"

ALL_CHANNELS = (CHANNEL_WHATSAPP, CHANNEL_SMS, CHANNEL_EMAIL, CHANNEL_RCS)

DEFAULT_MOCK_CHANNELS = ",".join(ALL_CHANNELS)

# Minimal E.164 dialling-code -> ISO country map, only for countries this fork
# actually has an origination for. Longest prefix wins, so add longer codes
# before reusing a shorter one.
_COUNTRY_BY_DIAL_CODE = {
    "33": "FR",
    "1": "US",
}


def country_for(number: str) -> str:
    """ISO country for an E.164 number in digits ('33670022195' -> 'FR')."""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    for code in sorted(_COUNTRY_BY_DIAL_CODE, key=len, reverse=True):
        if digits.startswith(code):
            return _COUNTRY_BY_DIAL_CODE[code]
    return ""


def origination_for_country(number: str) -> str:
    """Origination identity to send *to* `number`, or "" for the default.

    EUM has no origination that reaches every country: the demo simulator is
    US-only, France needs the CONFORMITE sender ID (a sender ID cannot message a
    US simulator destination, and vice versa). Overrides come from
    `conformity.sms.origination_by_country`, deployed as JSON.
    """
    try:
        overrides = json.loads(os.getenv("SMS_ORIGINATION_BY_COUNTRY", "") or "{}")
    except json.JSONDecodeError:
        logger.error("SMS_ORIGINATION_BY_COUNTRY is not valid JSON; using the default")
        return ""
    return overrides.get(country_for(number), "")


def mocked_channels() -> set:
    """Channels to keep in demo mode, from MOCK_CHANNELS (CSV)."""
    raw = os.getenv("MOCK_CHANNELS", DEFAULT_MOCK_CHANNELS)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_mocked(channel: str) -> bool:
    return str(channel or "").lower() in mocked_channels()


def send_reply(channel: str, sender: str, text: str, **meta) -> dict:
    """Deliver an agent reply on the channel it came from.

    `sender` is the normalized identity (digits). `meta` carries whatever the
    inbound message provided (e.g. WhatsApp phone_number_id/message_id) so the
    channel implementation can set context/reply-to correctly.
    """
    channel = str(channel or "").lower()
    if is_mocked(channel):
        logger.info("MOCK_%s: reply to %s: %s", channel.upper(), sender, text)
        return {"mocked": True, "channel": channel, "to": sender, "text": text}

    if channel == CHANNEL_WHATSAPP:
        return _send_whatsapp(sender, text, **meta)
    if channel == CHANNEL_SMS:
        return _send_sms(sender, text, **meta)
    if channel == CHANNEL_EMAIL:
        return _send_email(sender, text, **meta)
    if channel == CHANNEL_RCS:
        return _send_rcs(sender, text, **meta)

    logger.error("No dispatcher for channel %r; logging reply instead", channel)
    return {"mocked": True, "channel": channel, "to": sender, "text": text}


def _send_whatsapp(sender: str, text: str, phone_number_id: str = "", message_id: str = "", **_ignored) -> dict:
    import boto3

    message_object = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "context": {"message_id": message_id},
        "to": f"+{sender}",
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    client = boto3.client("socialmessaging")
    response = client.send_whatsapp_message(
        originationPhoneNumberId=phone_number_id,
        metaApiVersion="v20.0",
        message=bytes(json.dumps(message_object), "utf-8"),
    )
    logger.info("WhatsApp reply sent to %s: %s", sender, response)
    return response


def _send_sms(sender: str, text: str, origination_identity: str = "",
              origination_number: str = "", **_ignored) -> dict:
    """Send an SMS via AWS End User Messaging (Pinpoint SMS Voice v2).

    `OriginationIdentity` accepts a phone-number id/ARN, a pool id, or an
    alphanumeric sender ID. France has no long codes through the API, but
    alphanumeric sender IDs need no registration there (only the rule that
    since 2026-03-01 they must be a-z/A-Z/0-9 with no dashes).

    Config wins over the inbound recipient: the deployed origination is the
    stable identity, while a demo envelope's destinationNumber is synthetic.
    """
    import boto3

    identity = (
        origination_identity
        or origination_for_country(sender)
        or os.getenv("SMS_ORIGINATION_IDENTITY", "")
        or origination_number
    )
    if not identity:
        raise RuntimeError(
            "No SMS origination identity: set conformity.sms.origination_identity "
            "in config.yaml (or keep 'sms' in MOCK_CHANNELS)"
        )

    client = boto3.client("pinpoint-sms-voice-v2")
    response = client.send_text_message(
        DestinationPhoneNumber=f"+{sender}",
        OriginationIdentity=identity,
        MessageBody=text,
        MessageType=os.getenv("SMS_MESSAGE_TYPE", "TRANSACTIONAL"),
    )
    logger.info("SMS reply sent to %s from %s: %s", sender, identity, response)
    return response


def _send_email(sender: str, text: str, subject: str = "", to_email: str = "", **_ignored) -> dict:
    """Send the reply as an e-mail (SES v2)."""
    import boto3

    client = boto3.client("sesv2")
    response = client.send_email(
        FromEmailAddress=os.getenv("CONTRACTOR_EMAIL_FROM", "noreply@example.com"),
        Destination={"ToAddresses": [to_email or sender]},
        Content={"Simple": {
            "Subject": {"Data": subject or "Conformity assistant"},
            "Body": {"Text": {"Data": text}},
        }},
    )
    logger.info("E-mail reply sent to %s: %s", to_email or sender, response)
    return response


def _send_rcs(sender: str, text: str, **_ignored) -> dict:
    """RCS is stubbed for Phase 3: no agent/agent-id is provisioned yet."""
    logger.error("RCS channel requested but not implemented yet (Phase 3)")
    return {"mocked": True, "channel": CHANNEL_RCS, "to": sender,
            "text": text, "pending": "rcs-not-provisioned"}
