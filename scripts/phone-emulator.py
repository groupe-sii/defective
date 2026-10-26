#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Local phone emulator for the demo video.

WHY THIS EXISTS
The SMS transport is the EUM **simulator**: the stack really sends, and EUM
really records delivery events, but no handset receives anything. For a video
demo you still want to *see* the conversation. This serves a two-phone UI in the
browser and drives the real deployed stack:

    typing in a phone  -> publishes an inbound SMS envelope to the stack's SNS
                          topic (exactly what test-scripts/test-channel.py does)
    the assistant reply -> read back from the transcript table, so the bubble is
                          the agent's real answer, after the real tool calls
                          (Nova Pro + AgentCore Memory + Gateway MCP + Cedar)

Nothing here is part of the deployed system: it is a thin local viewer over
real infrastructure. It exists so the video can show a phone without faking the
agent.

Usage:
    uv run scripts/phone-emulator.py            # http://localhost:8787
    uv run scripts/phone-emulator.py --port 9000
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import get_stack_config, print_msg, print_section  # noqa: E402

HTML_PATH = scripts_dir.parent / "demo" / "phone-emulator.html"

# Filled in by main() from the stack outputs + config.yaml.
STATE = {
    "region": "eu-west-1",
    "topic_arn": "",
    "transcript_table": "",
    "phones": [],
}


def load_state(technician: str | None = None, expert: str | None = None,
               rcs_identity: str | None = None, show_rcs: bool = False,
               image_path: str | None = None) -> None:
    stack = get_stack_config()
    STATE["region"] = stack["region"]
    STATE["topic_arn"] = stack["outputs"]["ChannelTopicArn"]
    STATE["transcript_table"] = stack["outputs"].get("TranscriptTableName", "")
    STATE["media_bucket"] = stack["outputs"].get("InboundMediaBucketName", "")
    STATE["image_url"] = ""

    import yaml

    with open(scripts_dir.parent / "infra-cdk" / "config.yaml", "r") as handle:
        config = yaml.safe_load(handle) or {}
    conformity = config.get("conformity") or {}
    sms = conformity.get("sms") or {}
    identities = sms.get("demo_identities") or {}
    mocked = {
        part.strip()
        for part in str(conformity.get("mock_channels", "")).split(",")
        if part.strip()
    }

    def describe(role: str, identity: str, note: str) -> dict:
        real = identity.startswith("33")
        return {
            "id": identity,
            "label": role,
            "number": f"+{identity}",
            "note": "real handset · EUM" if real else note,
        }

    tech_id = technician or identities.get("technician", "14254147755")
    exp_id = expert or identities.get("expert", "5511999998888")

    phones = [
        describe("Technician", tech_id, "simulator · success"),
        describe("Expert", exp_id, "simulator · failure"),
    ]
    if show_rcs:
        rcs_id = rcs_identity or "14254147799"
        phones.append(describe("RCS (stub)", rcs_id, "stub · not provisioned"))
    STATE["phones"] = phones
    STATE["sms_mocked"] = "sms" in mocked
    STATE["origination_number"] = "12065552684"
    STATE["technician_id"] = tech_id

    # Optionally upload a demo image to S3 and use it as the technician's first
    # attachment (one-shot: cleared after the first send).
    if image_path:
        import boto3
        s3 = boto3.client("s3", region_name=STATE["region"])
        key = f"demo/{Path(image_path).name}"
        with open(image_path, "rb") as fh:
            s3.upload_fileobj(fh, STATE["media_bucket"], key)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": STATE["media_bucket"], "Key": key},
            ExpiresIn=7 * 24 * 3600,
        )
        STATE["image_url"] = url
        print_msg(f"uploaded {image_path} -> {url}", "success")


def sms_envelope(phone: str, text: str, destination: str, image_url: str = "") -> dict:
    """The inbound SMS shape AWS End User Messaging publishes to SNS."""
    env = {
        "originationNumber": f"+{phone}",
        "destinationNumber": f"+{destination}",
        "messageBody": text,
        "inboundMessageId": f"emu-sms-{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "messageKeyword": "",
        "previousPublishedMessageId": "",
    }
    if image_url:
        env["imageUrl"] = image_url
    return env


def publish(phone: str, text: str, image_url: str = "") -> dict:
    # One-shot: attach the demo image to the technician's first send only.
    if not image_url and phone == STATE.get("technician_id") and STATE.get("image_url"):
        image_url = STATE["image_url"]
        STATE["image_url"] = ""  # consume
    sns = boto3.client("sns", region_name=STATE["region"])
    resp = sns.publish(
        TopicArn=STATE["topic_arn"],
        Message=json.dumps(sms_envelope(phone, text, STATE["origination_number"], image_url)),
    )
    return {"ok": True, "message_id": resp.get("MessageId"), "image_url": image_url}



def messages(phone: str, limit: int = 100) -> dict:
    table_name = STATE["transcript_table"]
    if not table_name:
        return {"messages": [], "error": "TranscriptTableName output missing"}
    table = boto3.resource("dynamodb", region_name=STATE["region"]).Table(table_name)
    resp = table.query(
        KeyConditionExpression="#identity = :identity",
        ExpressionAttributeNames={"#identity": "identity"},
        ExpressionAttributeValues={":identity": phone},
        ScanIndexForward=True,
        Limit=limit,
    )
    items = [
        {
            "role": item.get("role", "user"),
            "text": item.get("text", ""),
            "channel": item.get("channel", ""),
            "image_url": item.get("image_url", ""),
            "created_at": item.get("created_at", ""),
            "dispatch": item.get("dispatch", ""),
        }
        for item in resp.get("Items", [])
    ]
    return {"messages": items}


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path.startswith("/api/status"):
            self._json({k: v for k, v in STATE.items()})
            return
        if self.path.startswith("/api/messages"):
            phone = ""
            if "phone=" in self.path:
                phone = self.path.split("phone=", 1)[1].split("&", 1)[0]
            if not phone:
                self._json({"error": "phone query parameter required"}, 400)
                return
            try:
                self._json(messages(phone))
            except Exception as err:  # surfaced in the UI, not a crash
                self._json({"messages": [], "error": str(err)}, 500)
            return
        if self.path in ("/", "/index.html"):
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/api/send"):
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, 400)
            return
        phone = str(payload.get("phone", "")).strip()
        text = str(payload.get("text", "")).strip()
        image_url = str(payload.get("imageUrl") or payload.get("image_url") or "").strip()
        if not phone or not text:
            self._json({"error": "phone and text are required"}, 400)
            return
        try:
            self._json(publish(phone, text, image_url))
        except Exception as err:
            self._json({"error": str(err)}, 500)

    def log_message(self, fmt, *args):  # quieter console
        print_msg(f"{self.address_string()} {fmt % args}", "info")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local phone emulator for the demo")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--technician", metavar="IDENTITY",
                        help="technician identity (digits only), overrides config.yaml")
    parser.add_argument("--expert", metavar="IDENTITY",
                        help="expert identity (digits only), overrides config.yaml")
    parser.add_argument("--image", metavar="PATH",
                        help="optional image file uploaded to S3 and attached to the technician's first message")
    parser.add_argument("--rcs", action="store_true",
                        help="add a third phone labelled 'RCS (stub)'")
    parser.add_argument("--rcs-identity", metavar="IDENTITY", default="14254147799",
                        help="identity used by the stubbed RCS phone")
    args = parser.parse_args()

    load_state(technician=args.technician, expert=args.expert,
               rcs_identity=args.rcs_identity, show_rcs=args.rcs,
               image_path=args.image)
    print_section("phone emulator")
    for phone in STATE["phones"]:
        print_msg(f"  {phone['label']:<11} {phone['number']}  (identity {phone['id']})", "info")
    if STATE.get("sms_mocked"):
        print_msg("WARNING: 'sms' is still in mock_channels — replies will not be sent", "error")
    print_msg(f"open http://localhost:{args.port}", "success")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
