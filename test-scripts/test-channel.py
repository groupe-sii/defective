#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Channel test driver — exercises the Conformity Agent the way a real phone does.

The POC's `mock_send.py` published a fake inbound event to a *POC* SNS topic.
This is the AgentCore port of that idea, and it works the same way:

    build an inbound envelope (WhatsApp webhook or SMS payload)
      -> publish it to the stack's inbound SNS topic
        -> the channel-handler Lambda normalizes it, maps the phone to a user,
           calls the AgentCore Runtime, and dispatches the (mocked) reply

That means this script tests the *whole* production path, not just the agent —
the same property `mock_send.py` had. The only mocked part is the outbound send,
controlled by the stack's MOCK_CHANNELS setting.

Usage:
    # Make sure the agent stack is deployed and you have AWS credentials.
    # A technician reports a non-conformity over WhatsApp:
    uv run test-scripts/test-channel.py "Pump P-101 is leaking at the seal" \\
        --phone 5521988887777 --channel whatsapp

    # The expert validates a case in their own chat (they are role=expert):
    uv run test-scripts/test-channel.py "approve NC-20261021-ab12cd" \\
        --phone 5511999998888 --channel whatsapp

    # Same person, different channel — history is shared (keyed by identity):
    uv run test-scripts/test-channel.py "status?" \\
        --phone 5521988887777 --channel sms

    # Offline: just print the SNS envelope, no AWS calls at all:
    uv run test-scripts/test-channel.py "hello" --print-event

Notes:
  - Replies are read from the channel-handler's CloudWatch log group, filtered
    to the invocation this script just triggered (the POC had the same need:
    a busy log group interleaves earlier invocations).
  - `--phone` also selects the role: numbers seeded as experts in the contacts
    table are experts; everyone else is a technician.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

# Reuse FAST's stack/output helpers so this script behaves like its siblings.
scripts_dir = Path(__file__).parent.parent / "scripts"
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import get_stack_config, print_msg, print_section  # noqa: E402

CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_SMS = "sms"

# The region the stack lives in (from config.yaml). Filled in by main() before
# any envelope is built; the ARNs in the WhatsApp fixture are only cosmetic, so
# the default is fine for --print-event which never reaches AWS.
REGION = "eu-west-1"


def normalize_phone(raw: str) -> str:
    """Canonical phone identity: digits only, no '+' (matches the Lambda)."""
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


# ---------------------------------------------------------------------------
# Envelope builders — these mirror what AWS End User Messaging publishes
# ---------------------------------------------------------------------------

def build_whatsapp_envelope(phone: str, text: str, phone_number_id: str,
                            origination_id: str) -> dict:
    """A WhatsApp event exactly as AWS End User Messaging Social publishes it.

    Shape per the End User Messaging Social docs ("Example WhatsApp JSON for
    receiving a message"): the Meta webhook body lives under
    `whatsAppWebhookEntry` **as a JSON string**, wrapped with `context`
    (MetaWabaIds / MetaPhoneNumberIds), `messageId` and `message_timestamp`.

    Two different ids appear, and conflating them is a real footgun:

      phone_number_id  — Meta's id, in the webhook `metadata`. Used to *match*
                         the inbound message to a context entry.
      origination_id   — the AWS-assigned id (``phone-number-id-...``), carried
                         in the context ARN. This is what an outbound reply is
                         addressed with.
    """
    sender = normalize_phone(phone)
    webhook_body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "demo-entry",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": sender,
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Demo"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": f"wamid.demo{int(time.time())}",
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return {
        "context": {
            "MetaWabaIds": [
                {
                    "wabaId": "demo-waba",
                    "arn": f"arn:aws:social-messaging:{REGION}:000000000000:waba/demo",
                }
            ],
            "MetaPhoneNumberIds": [
                {
                    "metaPhoneNumberId": phone_number_id,
                    "arn": (
                        f"arn:aws:social-messaging:{REGION}:000000000000:"
                        f"phone-number-id/{origination_id}"
                    ),
                }
            ],
        },
        # Real payloads carry this as a JSON *string*; the Lambda accepts both.
        "whatsAppWebhookEntry": json.dumps(webhook_body),
        "aws_account_id": "000000000000",
        "message_timestamp": datetime.now(timezone.utc).isoformat(),
        "messageId": f"demo-{int(time.time())}",
    }


def build_sms_envelope(phone: str, text: str, destination: str) -> dict:
    """An AWS End User Messaging SMS inbound payload.

    Keys per the End User Messaging docs: originationNumber (sender),
    destinationNumber (our number), messageBody, inboundMessageId.
    """
    return {
        "originationNumber": f"+{normalize_phone(phone)}",
        "destinationNumber": f"+{normalize_phone(destination)}",
        "messageBody": text,
        "inboundMessageId": f"demo-sms-{int(time.time())}",
        "messageKeyword": "",
        "previousPublishedMessageId": "",
    }


def build_envelope(channel: str, phone: str, text: str, phone_number_id: str,
                   origination_id: str, destination: str) -> dict:
    if channel == CHANNEL_WHATSAPP:
        return build_whatsapp_envelope(phone, text, phone_number_id, origination_id)
    return build_sms_envelope(phone, text, destination)


# ---------------------------------------------------------------------------
# Log tailing (the POC-popularised trick: read the reply from CloudWatch)
# ---------------------------------------------------------------------------

def tail_reply(logs, log_group: str, start_ms: int, timeout_s: int = 90) -> list:
    """Poll the channel-handler log group for this invocation's output.

    Returns the log lines we care about (agent resolution + the dispatched
    reply), ignoring anything older than `start_ms` so earlier invocations do
    not pollute the output.
    """
    seen = []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = logs.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                filterPattern='?MOCK_ ?"resolved" ?ERROR ?"reply"',
            )
        except logs.exceptions.ResourceNotFoundException:
            print_msg(f"Log group not found yet: {log_group}", "info")
            time.sleep(3)
            continue

        for event in resp.get("events", []):
            message = event.get("message", "").rstrip()
            if message and message not in seen:
                seen.append(message)
                print(message)

        # The handler logs the MOCK_<CHANNEL> line last; stop once we see it.
        if any("MOCK_" in line for line in seen):
            break
        time.sleep(3)
    return seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a fake inbound channel message through the deployed stack",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("text", nargs="?", default="", help="Message text to send")
    parser.add_argument("--text", dest="text_flag", default=None,
                        help="Message text (alternative to the positional argument)")
    parser.add_argument("--phone", default="5521988887777",
                        help="Sender phone in digits (5511999998888 = seeded expert)")
    parser.add_argument("--channel", default=CHANNEL_WHATSAPP,
                        choices=[CHANNEL_WHATSAPP, CHANNEL_SMS],
                        help="Channel to simulate (default: whatsapp)")
    parser.add_argument("--phone-number-id", default="46271669example",
                        help="Meta phone_number_id that delivered the message")
    parser.add_argument("--origination-id",
                        default="976c72a700aac43eaf573ae0500example",
                        help="AWS origination phone number id (used to address replies)")
    parser.add_argument("--destination", default="15550001111",
                        help="Our number the SMS was sent to (SMS only)")
    parser.add_argument("--print-event", action="store_true",
                        help="Print the SNS envelope and exit (no AWS calls)")
    parser.add_argument("--tail-only", action="store_true",
                        help="Only tail the log group; publish nothing")
    return parser.parse_args()


def main() -> None:
    global REGION
    args = parse_arguments()
    text = args.text_flag if args.text_flag is not None else args.text
    if not text and not args.tail_only:
        print_msg("A message is required (positional or --text)", "error")
        sys.exit(1)

    # Offline mode first: --print-event must not touch AWS at all (it is how you
    # inspect the envelope before the stack exists, and REST is not required to
    # build a fixture).
    if args.print_event:
        envelope = build_envelope(
            args.channel, args.phone, text, args.phone_number_id, args.origination_id,
            args.destination,
        )
        print_section("SNS envelope (offline)")
        print(json.dumps(envelope, indent=2))
        return

    # Resolve the stack: `region` feeds the ARNs inside the WhatsApp fixture.
    stack_cfg = get_stack_config()
    region = stack_cfg["region"]
    REGION = region
    outputs = stack_cfg["outputs"]

    envelope = build_envelope(
        args.channel, args.phone, text, args.phone_number_id, args.origination_id,
        args.destination,
    )

    print_section("Conformity channel test")

    topic_arn = outputs.get("ChannelTopicArn")
    if not topic_arn:
        print_msg("Stack output ChannelTopicArn is missing — is the stack deployed?", "error")
        sys.exit(1)

    log_group = f"/aws/lambda/{stack_cfg['stack_name']}-channel-handler"
    print(f"Stack:       {stack_cfg['stack_name']}")
    print(f"Channel:     {args.channel}")
    print(f"From:        {normalize_phone(args.phone)}")
    print(f"Topic:       {topic_arn}")
    print(f"Log group:   {log_group}")

    logs = boto3.client("logs", region_name=region)

    if not args.tail_only:
        start_ms = int(time.time() * 1000) - 1000
        sns = boto3.client("sns", region_name=region)
        print_section("Publishing inbound message")
        sns.publish(TopicArn=topic_arn, Message=json.dumps(envelope))
        print(f"Published at {datetime.now(timezone.utc).isoformat()}")
    else:
        start_ms = int(time.time() * 1000) - 60_000

    print_section("Reply (from CloudWatch)")
    lines = tail_reply(logs, log_group, start_ms)
    if not lines:
        print_msg("No reply observed within the timeout.", "error")
        print("Check the channel-handler log group directly; the agent may still be running,")
        print("or the stack's MOCK_CHANNELS may not include this channel.")
        sys.exit(1)
    print_section("Done")


if __name__ == "__main__":
    main()
