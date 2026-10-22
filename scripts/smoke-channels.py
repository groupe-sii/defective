#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
One-command answer to "which channels are actually live?".

The deployed system has four channels (WhatsApp, SMS, e-mail, RCS) and three
independent switches (per-channel mock flags, plus `mock_email`). After a few
deploys it is genuinely hard to remember which one is real, and the answer lives
in four different services. This script reads them all and prints one verdict
per channel, plus the reason when a channel cannot be live.

It also reports the two access-control settings that fail *silently*: whether
the Cedar policy engine is attached to the Gateway (an unattached engine is
never evaluated) and whether the Pre-Token Lambda can see the contacts table
(without it every token gets role=technician).

Read-only: it never publishes, provisions or deletes anything.

Usage:
    uv run scripts/smoke-channels.py
    uv run scripts/smoke-channels.py --json     # machine-readable
"""

import argparse
import json
import sys
from pathlib import Path

import boto3
import yaml

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import get_stack_config, print_msg, print_section  # noqa: E402


def load_config() -> dict:
    with open(scripts_dir.parent / "infra-cdk" / "config.yaml", "r") as handle:
        return yaml.safe_load(handle) or {}


def mocked_channels(conformity: dict) -> set:
    raw = str(conformity.get("mock_channels", ""))
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def safe(fn, default):
    """Call a describe/list operation, returning `default` on any error."""
    try:
        return fn()
    except Exception as err:  # noqa: BLE001 - a smoke report never crashes
        return {"__error__": f"{type(err).__name__}: {err}"} if default is None else default


def find_output(outputs: dict, suffix: str) -> str:
    """Find a stack output by exact name or CDK's auto-generated suffix form."""
    if outputs.get(suffix):
        return outputs[suffix]
    for key, value in outputs.items():
        if key.endswith(suffix) and value:
            return value
    return ""


def collect(region: str, outputs: dict, conformity: dict, stack_name: str = "conformity-agent") -> dict:
    mocked = mocked_channels(conformity)
    identifiers = conformity.get("sms") or {}
    demo_ids = identifiers.get("demo_identities") or {}
    report = {"region": region, "mock_channels": sorted(mocked),
              "stack_name_base": stack_name}

    # --- End User Messaging (SMS) -------------------------------------------
    eum = boto3.client("pinpoint-sms-voice-v2", region_name=region)
    attrs = safe(lambda: eum.describe_account_attributes(), {"AccountAttributes": []})
    report["sms_tier"] = next(
        (a["Value"] for a in attrs.get("AccountAttributes", []) if a["Name"] == "ACCOUNT_TIER"),
        "unknown",
    )
    numbers = safe(lambda: eum.describe_phone_numbers(), {"PhoneNumbers": []}).get("PhoneNumbers", [])
    report["sms_origination"] = [
        {
            "number": n.get("PhoneNumber"),
            "id": n.get("PhoneNumberId"),
            "type": n.get("NumberType"),
            "status": n.get("Status"),
        }
        for n in numbers
        if n.get("Status") != "DELETED"
    ]
    report["sms_sender_ids"] = [
        {
            "sender_id": s.get("SenderId"),
            "country": s.get("IsoCountryCode"),
            "registered": s.get("Registered"),
            "arn": s.get("SenderIdArn"),
        }
        for s in safe(lambda: eum.describe_sender_ids(), {"SenderIds": []}).get("SenderIds", [])
    ]
    verified = safe(
        lambda: eum.describe_verified_destination_numbers(),
        {"VerifiedDestinationNumbers": []},
    ).get("VerifiedDestinationNumbers", [])
    report["sms_verified_destinations"] = [
        {"number": v.get("DestinationPhoneNumber"), "status": v.get("Status")} for v in verified
    ]
    report["sms_demo_identities"] = demo_ids
    report["sms_origination_by_country"] = identifiers.get("origination_by_country") or {}

    # --- WhatsApp (End User Messaging Social) -------------------------------
    social = boto3.client("socialmessaging", region_name=region)
    accounts = safe(
        lambda: social.list_linked_whatsapp_business_accounts(), {"linkedAccounts": []}
    )
    report["whatsapp_accounts"] = [
        {
            "id": a.get("id"),
            "arn": a.get("arn"),
            "waba_id": (a.get("wabaId") or a.get("metaWabaId")),
            "status": a.get("status"),
        }
        for a in accounts.get("linkedAccounts", [])
    ]

    # --- E-mail (SES v2) ----------------------------------------------------
    ses = boto3.client("sesv2", region_name=region)
    identities = safe(lambda: ses.list_email_identities(), {"EmailIdentities": []})
    report["email_identities"] = [
        {"identity": i.get("IdentityName"), "type": i.get("IdentityType"),
         "verified": i.get("SendingEnabled")}
        for i in identities.get("EmailIdentities", [])
    ]

    # --- access control (Cedar + role claims) ------------------------------
    # Two silent failures worth detecting: a gateway with no policy engine
    # attached (Cedar created but never evaluated) and a pre-token Lambda with
    # no contacts table (every token says role=technician).
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    # CDK auto-names the L2 output (<construct-path>GatewayId<HASH>), so match
    # on the suffix; fall back to the first gateway of this stack.
    gateway_id = find_output(outputs, "GatewayId")
    if not gateway_id:
        gateways = safe(lambda: control.list_gateways(), {}).get("items", [])
        for item in gateways:
            if report.get("stack_name_base", "") in (item.get("name") or ""):
                gateway_id = item.get("gatewayId", "")
                break
    gateway = (
        safe(lambda: control.get_gateway(gatewayIdentifier=gateway_id), {})
        if gateway_id else {}
    )
    engine = gateway.get("policyEngineConfiguration") or {}
    report["gateway_id"] = gateway_id
    report["policy_engine"] = {
        "attached": bool(engine.get("arn")),
        "mode": engine.get("mode"),
        "arn": engine.get("arn"),
    }
    report["interceptor_attached"] = bool(gateway.get("interceptorConfigurations"))

    function_name = f"{report.get('stack_name_base', 'conformity-agent')}-pretoken-v3"
    awslambda = boto3.client("lambda", region_name=region)
    pre_token = safe(
        lambda: awslambda.get_function_configuration(FunctionName=function_name), {}
    )
    report["pretoken_contacts_table"] = (
        (pre_token.get("Environment") or {}).get("Variables", {}).get("CONTACTS_TABLE", "")
    )

    # --- solution-level state ----------------------------------------------
    report["outputs"] = outputs
    report["mock_email"] = bool(conformity.get("mock_email", True))
    report["sms_origination_identity"] = identifiers.get("origination_identity", "")
    return report


def verdicts(report: dict) -> list:
    """One (channel, state, reason) tuple per channel."""
    mocked = set(report["mock_channels"])
    sms_orig = [
        n for n in report["sms_origination"]
        if n["id"] == report.get("sms_origination_identity")
    ]
    sim_active = [n for n in sms_orig if n.get("type") == "SIMULATOR" and n.get("status") == "ACTIVE"]
    out = []

    if "sms" in mocked:
        out.append(("sms", "MOCKED", "listed in conformity.mock_channels"))
    elif sim_active:
        out.append(("sms", "LIVE (simulator)",
                    f"{sim_active[0]['number']} — no carrier, EUM delivery events only"))
    elif sms_orig:
        out.append(("sms", "LIVE", sms_orig[0]["number"]))
    else:
        out.append(("sms", "BROKEN", "not mocked but no active origination identity"))

    if "whatsapp" in mocked:
        reason = "listed in conformity.mock_channels"
        if not report["whatsapp_accounts"]:
            reason += "; no WABA linked"
        out.append(("whatsapp", "MOCKED", reason))
    elif not report["whatsapp_accounts"]:
        out.append(("whatsapp", "BROKEN", "not mocked but no WABA is linked"))
    else:
        out.append(("whatsapp", "LIVE", report["whatsapp_accounts"][0]["id"]))

    if "rcs" in mocked:
        out.append(("rcs", "MOCKED", "stub channel"))
    else:
        out.append(("rcs", "BROKEN", "stub channel cannot send"))

    contractor_email = report.get("mock_email")
    identities = [i for i in report["email_identities"] if i.get("verified")]
    if contractor_email:
        out.append(("email (contractor)", "MOCKED",
                    "conformity.mock_email=true; logs instead of SES"))
    elif not identities:
        out.append(("email (contractor)", "BROKEN",
                    "not mocked but SES has no sending-enabled identity"))
    else:
        out.append(("email (contractor)", "LIVE", identities[0]["identity"]))
    return out


def access_verdicts(report: dict) -> list:
    """One (check, state, reason) tuple for the two silent access-control traps."""
    engine = report.get("policy_engine") or {}
    if engine.get("attached"):
        out = [("cedar policy engine", engine.get("mode") or "attached",
                engine.get("arn") or "")]
    else:
        out = [("cedar policy engine", "DETACHED",
                "gateway has no policyEngineConfiguration: Cedar is never evaluated")]

    out.append((
        "identity interceptor",
        "LIVE" if report.get("interceptor_attached") else "MISSING",
        "injects user_id/role into tool arguments" if report.get("interceptor_attached")
        else "gateway has no interceptor: tools lose the caller identity",
    ))
    out.append((
        "pre-token role lookup",
        "LIVE" if report.get("pretoken_contacts_table") else "BROKEN",
        report["pretoken_contacts_table"] if report.get("pretoken_contacts_table")
        else "CONTACTS_TABLE unset: every token carries role=technician",
    ))
    return out


def print_report(report: dict) -> None:
    print_section("channels")
    for channel, state, reason in verdicts(report):
        print_msg(f"  {channel:<18} {state:<18} {reason}", "info")

    print_section("access control")
    for check, state, reason in access_verdicts(report):
        level = "info" if state in ("LIVE", "ENFORCE", "LOG_ONLY", "attached") else "error"
        print_msg(f"  {check:<20} {state:<18} {reason}", level)

    print_section("sms")
    print_msg(f"  account tier: {report['sms_tier']}", "info")
    print_msg(f"  configured origination: {report['sms_origination_identity']}", "info")
    for number in report["sms_origination"]:
        print_msg(f"  number {number['number']} ({number['type']}, {number['status']})", "info")
    for sender in report["sms_sender_ids"]:
        print_msg(f"  sender ID {sender['sender_id']}/{sender['country']} "
                  f"registered={sender['registered']}", "info")
    for dest in report["sms_verified_destinations"]:
        print_msg(f"  verified destination {dest['number']} status={dest['status']}", "info")
    demos = report["sms_demo_identities"]
    if demos:
        print_msg(f"  demo identities: technician={demos.get('technician')} "
                  f"expert={demos.get('expert')}", "info")
    for country, origination in (report.get("sms_origination_by_country") or {}).items():
        print_msg(f"  origination for {country}: {origination}", "info")

    print_section("whatsapp")
    if not report["whatsapp_accounts"]:
        print_msg("  no linked WhatsApp Business Account", "info")
    for account in report["whatsapp_accounts"]:
        print_msg(f"  {account['id']} status={account['status']}", "info")

    print_section("email")
    print_msg(f"  mock_email: {report['mock_email']}", "info")
    if not report["email_identities"]:
        print_msg("  no SES identities", "info")
    for identity in report["email_identities"]:
        print_msg(f"  {identity['identity']} ({identity['type']}) "
                  f"sending={identity['verified']}", "info")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report live status of every channel")
    parser.add_argument("--json", action="store_true", help="print JSON and exit")
    args = parser.parse_args()

    stack = get_stack_config()
    config = load_config()
    conformity = config.get("conformity") or {}
    report = collect(stack["region"], stack["outputs"], conformity, stack["stack_name"])

    if args.json:
        print(json.dumps({"report": report, "verdicts": verdicts(report),
                          "access": access_verdicts(report)}, indent=2,
                         default=str))
        return

    print_section("stack")
    print_msg(f"  {stack['stack_name']} ({stack['region']})", "info")
    print_msg(f"  mock_channels: {','.join(report['mock_channels']) or '(none)'}", "info")
    print_report(report)


if __name__ == "__main__":
    main()
