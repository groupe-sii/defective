#!/usr/bin/env python3
"""Demo the deployed agent WITHOUT a WhatsApp phone number or SMS number.

Publishes a crafted inbound event (WhatsApp webhook, or an AWS End User
Messaging SMS payload) to the project's real SNS topic, which triggers the real
Lambda, Strands agent, Bedrock model and DynamoDB history. Because the stack
runs with MOCK_CHANNELS covering the demo channels, the reply is written to
CloudWatch logs instead of being sent -- and this script tails those logs.

Usage:
    .venv/Scripts/python mock_send.py "P-101 is leaking oil"
    .venv/Scripts/python mock_send.py --channel sms "same case, sent by SMS"   # shared history
    .venv/Scripts/python mock_send.py --tail-only                             # re-read last replies
    .venv/Scripts/python mock_send.py --print-event "hi"                      # just show the event JSON

Requires: boto3 + AWS credentials with permissions for CloudFormation (read),
SNS (publish) and CloudWatch Logs (read).
"""

import argparse
import json
import sys
import time
import uuid

import boto3

STACK_NAME = "aws-whatsapp-stack"
LAMBDA_FN = "WhatsApp-Aws-Integration-Main"
LOG_GROUP = f"/aws/lambda/{LAMBDA_FN}"
DEFAULT_MODEL_TEXT = "Hi! We found a leak on pump P-101. What is the procedure?"
DEFAULT_PHONE = "5511987654321"  # fake, fixed => one conversation history
MOCK_PHONE_NUMBER_ID = "123456789012345"  # fake Meta phone number id
MOCK_SMS_DESTINATION = "15550100"  # fake AWS End User Messaging SMS number
POLL_TIMEOUT_S = 120
POLL_INTERVAL_S = 4
MAX_LINE_CHARS = 4000


def build_whatsapp_event(text: str, phone: str, region: str, account: str) -> dict:
    """Build the SNS message body exactly as WhatsappService expects it."""
    meta_arn = (
        f"arn:aws:social-messaging:{region}:{account}:phone-number-id/{MOCK_PHONE_NUMBER_ID}"
    )
    webhook_entry = {
        "changes": [
            {
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": MOCK_PHONE_NUMBER_ID},
                    "messages": [
                        {
                            "from": phone,
                            "id": f"wamid.{uuid.uuid4().hex}",
                            "timestamp": str(int(time.time())),
                            "type": "text",
                            "text": {"body": text},
                        }
                    ],
                },
            }
        ]
    }
    return {
        "context": {
            "MetaPhoneNumberIds": [
                {"arn": meta_arn, "metaPhoneNumberId": MOCK_PHONE_NUMBER_ID}
            ],
            "MetaWabaIds": [],
        },
        "whatsAppWebhookEntry": json.dumps(webhook_entry),
        "message_timestamp": str(int(time.time())),
    }


def build_sms_event(text: str, phone: str) -> dict:
    """Build the SNS message body as AWS End User Messaging SMS delivers it."""
    return {
        "originationNumber": f"+{phone}",
        "destinationNumber": f"+{MOCK_SMS_DESTINATION}",
        "messageKeyword": "",
        "messageBody": text,
        "inboundMessageId": f"sms-{uuid.uuid4().hex}",
        "previousPublishedMessageId": "",
        "message_timestamp": str(int(time.time())),
    }


def build_sns_message(text: str, phone: str, region: str, account: str, channel: str) -> dict:
    if channel == "sms":
        return build_sms_event(text, phone)
    return build_whatsapp_event(text, phone, region, account)


def get_topic_arn(cfn) -> str:
    stack = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    for out in stack.get("Outputs", []):
        if out["OutputKey"] == "WhatsAppAWSTopicArn":
            return out["OutputValue"]
    raise SystemExit("Could not find WhatsAppAWSTopicArn in stack outputs")


MAX_STREAMS_PER_POLL = 10


def find_log_streams(logs) -> list:
    """All streams, most recently active first.

    NOTE: `lastEventTime` is sometimes absent (None) on Lambda streams even
    when they contain events, so it can only be used for ordering - never to
    filter streams out, or freshly written logs get missed.
    """
    streams, token = [], None
    while True:
        kwargs = {"logGroupName": LOG_GROUP, "limit": 50}
        if token:
            kwargs["nextToken"] = token
        resp = logs.describe_log_streams(**kwargs)
        streams.extend(resp.get("logStreams", []))
        token = resp.get("nextToken")
        if not token or len(streams) >= 200:
            break
    streams.sort(key=lambda s: (s.get("lastEventTime") or 0, s["logStreamName"]), reverse=True)
    return streams


def fetch_new_events(logs, since_ms: int, seen: set) -> list:
    fresh = []
    for stream in find_log_streams(logs)[:MAX_STREAMS_PER_POLL]:
        try:
            resp = logs.get_log_events(
                logGroupName=LOG_GROUP,
                logStreamName=stream["logStreamName"],
                startTime=since_ms,
                startFromHead=True,
            )
        except logs.exceptions.ResourceNotFoundException:
            continue
        for ev in resp.get("events", []):
            # eventId is not guaranteed on every GetLogEvents response.
            key = ev.get("eventId") or (ev.get("timestamp"), ev.get("message"))
            if key not in seen:
                seen.add(key)
                fresh.append(ev)
    fresh.sort(key=lambda ev: ev.get("timestamp", 0))
    return fresh


def print_events(events) -> list:
    """Print events, return any terminal markers found."""
    markers = []
    for ev in events:
        msg = ev["message"].strip()
        print(f"  | {msg[:MAX_LINE_CHARS]}")
        if "MOCK_" in msg and ": reply to" in msg:
            markers.append("reply")
        if "Error processing event" in msg or "ERROR" in msg:
            markers.append("error")
    return markers


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", default=DEFAULT_MODEL_TEXT, help="message to send to the agent")
    ap.add_argument("--phone", default=DEFAULT_PHONE, help="fake sender phone number (fixed => shared history)")
    ap.add_argument("--channel", default="whatsapp", choices=["whatsapp", "sms"],
                    help="inbound channel to simulate (same --phone on both shares one conversation)")
    ap.add_argument("--topic-arn", help="override SNS topic ARN (default: from stack outputs)")
    ap.add_argument("--region", help="AWS region (default: from boto3 session)")
    ap.add_argument("--print-event", action="store_true", help="only print the event JSON, don't publish")
    ap.add_argument("--tail-only", action="store_true", help="don't publish, just tail logs from the last hour")
    args = ap.parse_args()

    session = boto3.session.Session()
    region = args.region or session.region_name or "eu-west-1"

    # --print-event works offline: no credentials or AWS calls needed.
    if args.print_event:
        print(json.dumps(build_sns_message(args.text, args.phone, region, "123456789012", args.channel), indent=2))
        return

    sns = session.client("sns", region_name=region)
    cfn = session.client("cloudformation", region_name=region)
    logs = session.client("logs", region_name=region)

    account = session.client("sts", region_name=region).get_caller_identity()["Account"]
    message = build_sns_message(args.text, args.phone, region, account, args.channel)

    since_ms = int((time.time() - 3600) * 1000) if args.tail_only else int(time.time() * 1000) - 30_000

    if not args.tail_only:
        topic_arn = args.topic_arn or get_topic_arn(cfn)
        print(f"Publishing to {topic_arn}")
        print(f'  channel={args.channel} from={args.phone} text="{args.text}"')
        sns.publish(TopicArn=topic_arn, Message=json.dumps(message))
        print("Published. Waiting for the agent (Bedrock + tools can take 10-40s)...")

    seen: set = set()
    deadline = time.time() + POLL_TIMEOUT_S
    got_reply = False
    while time.time() < deadline:
        events = fetch_new_events(logs, since_ms, seen)
        if events:
            markers = print_events(events)
            if "reply" in markers:
                got_reply = True
                break
            if "error" in markers:
                print("An error occurred -- see log lines above.")
                sys.exit(1)
        time.sleep(POLL_INTERVAL_S)

    if got_reply:
        print("\nDone. (Run again with the same --phone to demo conversation history.)")
    elif args.tail_only:
        print("No Lambda log events found in the last hour.")
    else:
        print(f"\nNo reply within {POLL_TIMEOUT_S}s. Check logs manually:")
        print(f"  aws logs tail {LOG_GROUP} --since 10m")
        sys.exit(1)


if __name__ == "__main__":
    main()
