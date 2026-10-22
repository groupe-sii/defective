#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Provision the SMS channel (AWS End User Messaging, pinpoint-sms-voice-v2).

WHY THIS EXISTS
SMS is the one channel that can go live without any Meta/WABA involvement, so
it is the fork's end-to-end transport until WhatsApp is linked. Two account
facts drive this script:

  1. The account is in the EUM **sandbox**: it can only send to *verified
     destination numbers* (max 10) and is capped at $1/month of SMS. Adding a
     destination requires AWS to send a one-time code to that phone — and the
     code can only be sent by an origination identity from the same country.
  2. **France** offers no long codes through the API (no `FR_LONG_CODE_*`
     registration type exists), but alphanumeric sender IDs need no
     registration there. Since 2026-03-01 the only constraint is a-z/A-Z/0-9
     with no dashes. `CONFORMITE` satisfies it and costs $0/month.

So the flow is: a French sender ID is the origination, each tester's French
mobile is a verified destination, and replies are real SMS. Inbound stays
synthetic (test-scripts/test-channel.py publishes an EUM-shaped envelope to the
inbound SNS topic) because a sender ID cannot receive: France has no two-way
origination available to this account.

WHAT IT DOES
  --ensure-sender-id       create the FR sender ID if missing (free, idempotent)
  --add   +33 6 12 34 56 78   create the verified destination + send the code
  --verify +33... --code 123456
  --send  +33... --text "..." [--dry-run]
  --remove +33...
  --list                   show sender IDs, verified destinations, account tier

Usage:
    uv run scripts/provision-sms.py --list
    uv run scripts/provision-sms.py --ensure-sender-id
    uv run scripts/provision-sms.py --add +33612345678
    uv run scripts/provision-sms.py --verify +33612345678 --code 123456
    uv run scripts/provision-sms.py --send +33612345678 --text "hello"
"""

import argparse
import sys
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import print_msg, print_section  # noqa: E402

DEFAULT_SENDER_ID = "CONFORMITE"
DEFAULT_COUNTRY = "FR"
DEFAULT_LANGUAGE = "FR_FR"

# The EUM SMS simulator has a US-only origination that can only message US
# destination simulators. These two fixed numbers are the demo identities; the
# "failure" one is used to exercise delivery-failure handling.
SIMULATOR_DESTINATIONS = {
    "technician": ("+14254147755", "success"),
    "expert": ("+14254147167", "failure"),
}


def load_sms_config() -> dict:
    """`conformity.sms` + `conformity.region` from infra-cdk/config.yaml."""
    config_path = scripts_dir.parent / "infra-cdk" / "config.yaml"
    with open(config_path, "r") as handle:
        config = yaml.safe_load(handle) or {}
    conformity = config.get("conformity") or {}
    sms = conformity.get("sms") or {}
    identities = sms.get("demo_identities") or {}
    return {
        "region": conformity.get("region") or "eu-west-1",
        "origination_identity": sms.get("origination_identity") or DEFAULT_SENDER_ID,
        "message_type": sms.get("message_type") or "TRANSACTIONAL",
        "origination_by_country": sms.get("origination_by_country") or {},
        "technician": identities.get("technician") or SIMULATOR_DESTINATIONS["technician"][0],
        "expert": identities.get("expert") or SIMULATOR_DESTINATIONS["expert"][0],
    }


# Keep in sync with infra-cdk/lambdas/channel-handler/dispatcher.py.
COUNTRY_BY_DIAL_CODE = {"33": "FR", "1": "US"}


def country_for(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    for code in sorted(COUNTRY_BY_DIAL_CODE, key=len, reverse=True):
        if digits.startswith(code):
            return COUNTRY_BY_DIAL_CODE[code]
    return ""


def normalize_e164(raw: str) -> str:
    """`+33 6 12 34 56 78` / `0033...` -> `+33612345678`."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return f"+{digits}"


def client(region: str):
    return boto3.client("pinpoint-sms-voice-v2", region_name=region)


def find_sender_id(sms, sender_id: str, country: str) -> dict:
    try:
        resp = sms.describe_sender_ids(
            SenderIds=[{"SenderId": sender_id, "IsoCountryCode": country}]
        )
    except ClientError as err:
        # A filter that matches nothing raises ResourceNotFound; treat as absent.
        if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {}
        raise
    for entry in resp.get("SenderIds", []):
        if entry.get("SenderId") == sender_id:
            return entry
    return {}


def ensure_sender_id(sms, sender_id: str, country: str) -> dict:
    existing = find_sender_id(sms, sender_id, country)
    if existing:
        print_msg(f"sender ID {sender_id} ({country}) exists: {existing['SenderIdArn']}", "info")
        return existing
    created = sms.request_sender_id(
        SenderId=sender_id,
        IsoCountryCode=country,
        MessageTypes=["TRANSACTIONAL"],
    )
    print_msg(f"created sender ID {sender_id} ({country}): {created['SenderIdArn']}", "success")
    return created


def origination_for(sms, config: dict, phone: str) -> str:
    """Resolve the origination identity to send *to* `phone`.

    Per-country overrides win (France -> CONFORMITE), the simulator is the
    fallback. A bare sender-ID name is resolved to its ARN, which is
    unambiguous; phone-number ids and pool ids pass through.
    """
    country = country_for(phone)
    configured = config["origination_by_country"].get(country) \
        or config["origination_identity"]
    if configured.startswith("arn:") or configured.startswith("phone-") \
            or configured.startswith("pool-"):
        return configured
    found = find_sender_id(sms, configured, country or DEFAULT_COUNTRY)
    return found.get("SenderIdArn") or configured


def find_verified(sms, phone: str) -> dict:
    try:
        resp = sms.describe_verified_destination_numbers(DestinationPhoneNumbers=[phone])
    except ClientError as err:
        # A filter that matches nothing raises ResourceNotFound; treat as absent.
        if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {}
        raise
    for entry in resp.get("VerifiedDestinationNumbers", []):
        if entry.get("DestinationPhoneNumber") == phone:
            return entry
    return {}


def add_destination(sms, phone: str, origination: str,
                    language: str = DEFAULT_LANGUAGE) -> None:
    entry = find_verified(sms, phone)
    if entry:
        print_msg(f"{phone} already registered (id={entry['VerifiedDestinationNumberId']}, "
                  f"status={entry.get('Status')})", "info")
    else:
        entry = sms.create_verified_destination_number(DestinationPhoneNumber=phone)
        print_msg(f"registered {phone}: {entry['VerifiedDestinationNumberId']}", "success")

    sms.send_destination_number_verification_code(
        VerifiedDestinationNumberId=entry["VerifiedDestinationNumberId"],
        VerificationChannel="TEXT",
        LanguageCode=language,
        OriginationIdentity=origination,
    )
    print_msg(
        f"verification code sent to {phone} from {origination}. "
        f"Next: --verify {phone} --code <6 digits>",
        "info",
    )


def verify_destination(sms, phone: str, code: str) -> None:
    entry = find_verified(sms, phone)
    if not entry:
        raise SystemExit(f"{phone} is not a verified-destination record; run --add first")
    sms.verify_destination_number(
        VerifiedDestinationNumberId=entry["VerifiedDestinationNumberId"],
        VerificationCode=code,
    )
    print_msg(f"{phone} verified", "success")


def send_test(sms, phone: str, text: str, origination: str,
              message_type: str, dry_run: bool) -> None:
    kwargs = {
        "DestinationPhoneNumber": phone,
        "OriginationIdentity": origination,
        "MessageBody": text,
        "MessageType": message_type,
    }
    if dry_run:
        kwargs["DryRun"] = True
    resp = sms.send_text_message(**kwargs)
    label = "dry-run OK" if dry_run else "sent"
    print_msg(f"{label}: {phone} <- {origination}: {resp.get('MessageId')}", "success")


def ensure_simulator(sms, config: dict) -> None:
    """Request a US simulator origination if the account has none, and print the
    demo destination numbers. Messages stay inside EUM — no carrier, no phone."""
    numbers = sms.describe_phone_numbers().get("PhoneNumbers", [])
    sims = [n for n in numbers if n.get("NumberType") == "SIMULATOR"
            and n.get("Status") != "DELETED"]
    if not sims:
        created = sms.request_phone_number(
            IsoCountryCode="US",
            NumberType="SIMULATOR",
            NumberCapabilities=["SMS"],
            MessageType="TRANSACTIONAL",
        )
        print_msg(f"requested simulator origination {created['PhoneNumber']} "
                  f"({created['PhoneNumberId']})", "success")
        sims = [created]
    else:
        print_msg("simulator origination already provisioned", "info")

    for sim in sims:
        print_msg(f"  {sim['PhoneNumber']}  id={sim['PhoneNumberId']}  "
                  f"status={sim.get('Status')}", "info")

    print_msg("set in infra-cdk/config.yaml -> conformity.sms.origination_identity:", "info")
    print_msg(f"  {sims[0]['PhoneNumberId']}", "info")

    print_section("simulator destinations (US-only)")
    for role, (phone, kind) in SIMULATOR_DESTINATIONS.items():
        print_msg(f"  {role:10s} {phone}  ({kind} event)", "info")
    print_msg("a US simulator origination cannot message any other country "
              "(INVALID_IDENTITY_FOR_DESTINATION_COUNTRY)", "info")


def release_simulator(sms, phone_number_id: str) -> None:
    sms.release_phone_number(PhoneNumberId=phone_number_id)
    print_msg(f"released {phone_number_id}", "success")


def list_state(sms, config: dict) -> None:
    print_section("account")
    attrs = sms.describe_account_attributes().get("AccountAttributes", [])
    for attr in attrs:
        print_msg(f"  {attr['Name']} = {attr['Value']}", "info")
    for limit in sms.describe_spend_limits().get("SpendLimits", []):
        print_msg(f"  {limit['Name']}: {limit['EnforcedLimit']} (max {limit['MaxLimit']})", "info")

    print_section("sender IDs")
    for entry in sms.describe_sender_ids().get("SenderIds", []):
        print_msg(
            f"  {entry['SenderId']} ({entry['IsoCountryCode']}) "
            f"registered={entry.get('Registered')} "
            f"lease={entry.get('MonthlyLeasingPrice')} {entry['SenderIdArn']}",
            "info",
        )

    print_section("verified destinations")
    for entry in sms.describe_verified_destination_numbers().get("VerifiedDestinationNumbers", []):
        print_msg(
            f"  {entry['DestinationPhoneNumber']} status={entry.get('Status')} "
            f"id={entry['VerifiedDestinationNumberId']}",
            "info",
        )

    print_section("simulator origination")
    sims = [n for n in sms.describe_phone_numbers().get("PhoneNumbers", [])
            if n.get("NumberType") == "SIMULATOR" and n.get("Status") != "DELETED"]
    if not sims:
        print_msg("  none (run --simulator)", "info")
    for sim in sims:
        print_msg(f"  {sim['PhoneNumber']}  id={sim['PhoneNumberId']}  "
                  f"status={sim.get('Status')}", "info")

    print_section("sms config")
    print_msg(f"  origination_identity = {config['origination_identity']}", "info")
    print_msg(f"  message_type         = {config['message_type']}", "info")
    for country, identity in (config["origination_by_country"] or {}).items():
        print_msg(f"  origination {country}       = {identity}", "info")
    print_msg(f"  demo technician      = {config['technician']}", "info")
    print_msg(f"  demo expert          = {config['expert']}", "info")
    print_msg("  mock switch          = conformity.mock_channels in infra-cdk/config.yaml", "info")


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision the SMS channel (EUM).")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="show account/channel state")
    action.add_argument("--ensure-sender-id", action="store_true", help="create the FR sender ID")
    action.add_argument("--add", metavar="+33...", help="register destination and send the code")
    action.add_argument("--remove", metavar="+33...", help="delete a verified destination")
    action.add_argument("--verify", metavar="+33...", help="confirm a destination with --code")
    action.add_argument("--send", metavar="+33...", help="send a test message")
    action.add_argument("--simulator", action="store_true",
                        help="request a US SMS-simulator origination and show the demo numbers")
    action.add_argument("--release-simulator", metavar="PHONE_NUMBER_ID",
                        help="release a simulator origination")
    parser.add_argument("--code", help="verification code for --verify")
    parser.add_argument("--text", default="Conformity assistant test message",
                        help="body for --send")
    parser.add_argument("--dry-run", action="store_true", help="validate --send without sending")
    args = parser.parse_args()

    config = load_sms_config()
    sms = client(config["region"])

    if args.verify:
        if not args.code:
            parser.error("--verify requires --code")
        verify_destination(sms, normalize_e164(args.verify), args.code)
        return

    if args.add or args.send:
        phone = normalize_e164(args.add or args.send)
        origination = origination_for(sms, config, phone)
        if args.add:
            add_destination(sms, phone, origination)
        else:
            send_test(sms, phone, args.text, origination,
                      config["message_type"], args.dry_run)
        return

    if args.remove:
        entry = find_verified(sms, normalize_e164(args.remove))
        if not entry:
            raise SystemExit(f"{args.remove} is not registered")
        sms.delete_verified_destination_number(
            VerifiedDestinationNumberId=entry["VerifiedDestinationNumberId"]
        )
        print_msg(f"deleted {args.remove}", "success")
        return

    if args.ensure_sender_id:
        ensure_sender_id(sms, config["origination_identity"], DEFAULT_COUNTRY)
        return

    if args.simulator:
        ensure_simulator(sms, config)
        return

    if args.release_simulator:
        release_simulator(sms, args.release_simulator)
        return

    list_state(sms, config)


if __name__ == "__main__":
    main()
