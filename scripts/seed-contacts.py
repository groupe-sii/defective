#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Seed the conformity contacts table.

WHY THIS EXISTS
The conformity workflow has two chat roles:

  technician — reports a non-conformity (everyone)
  expert     — validates the drafted procedure (a specific few)

Role is not a hardcoded environment variable (the POC's EXPERT_CONTACTS hack).
It lives in the `conformity-agent-contacts` DynamoDB table, keyed by the
authenticated Cognito user, and is enforced at the AgentCore Gateway by a Cedar
policy. Keeping the data in a table means promoting someone to expert is a
put_item, not a redeploy.

CloudFormation cannot manage *items* inside a CDK-created table, so the seed is
this script — run it once after `cdk deploy` (scripts/deploy-with-codebuild.py
does not do it for you).

WHAT IT DOES
For each entry in `conformity.expert_contacts` in infra-cdk/config.yaml:

  1. ensure a Cognito user exists for the number
     (username = `<phone>@conformity.local`; the pool signs in by e-mail, so a
      synthetic address is used while the real number stays on the user's
      phone_number attribute. No password is set — these users never log in,
      they interact over WhatsApp/SMS and are mapped by phone number), and
  2. write a contacts row:
        actor_id        = <Cognito sub>        (primary key)
        phone_identity  = <digits>             (GSI: phone -> contact)
        role            = "expert"
        channel         = "whatsapp"           (preferred reply channel)

Unknown senders are served as technicians at runtime (the channel handler
attributes them to a deterministic `phone:<number>` actor), so seeding is only
strictly required to make someone an *expert*.

Usage:
    uv run scripts/seed-contacts.py                 # seed experts from config.yaml
    uv run scripts/seed-contacts.py --list          # show the table contents
    uv run scripts/seed-contacts.py --promote +5521988887777
    uv run scripts/seed-contacts.py --channel sms   # preferred reply channel
"""

import argparse
import sys
from pathlib import Path

import boto3
import yaml

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import get_stack_config, print_msg, print_section  # noqa: E402


def normalize_phone(raw: str) -> str:
    """Digits-only phone identity (must match utils/messages.normalize_identity)."""
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def load_expert_contacts() -> list:
    """Expert numbers from infra-cdk/config.yaml -> conformity.expert_contacts."""
    config_path = scripts_dir.parent / "infra-cdk" / "config.yaml"
    with open(config_path, "r") as handle:
        config = yaml.safe_load(handle) or {}
    return list((config.get("conformity") or {}).get("expert_contacts") or [])


def username_for_phone(phone: str) -> str:
    """Cognito username for a phone identity.

    The user pool is created with ``UsernameAttributes: ['email']`` (FAST's
    default — e-mail is the sign-in identifier), so Cognito rejects a bare phone
    number with:

        InvalidParameterException: Username should be an email.

    Nothing downstream depends on the username being the number: the channel
    handler and the Pre-Token Lambda both key on the Cognito ``sub``, which
    becomes ``actor_id``. So the seed uses a deterministic, clearly-synthetic
    address derived from the phone number. The real phone stays on the user as
    the ``phone_number`` attribute and in the contacts row as ``phone_identity``.
    """
    return f"{phone}@conformity.local"


def ensure_cognito_user(cognito, user_pool_id: str, phone: str) -> str:
    """Return the Cognito sub for `phone`, creating the user if needed.

    The username is a synthetic e-mail derived from the phone (see
    `username_for_phone`) because the pool signs users in by e-mail. No password
    is set: these users never sign in to the web console, they are addressed by
    phone number through the channel handler, which maps number -> this user's
    sub.
    """
    username = username_for_phone(phone)
    try:
        resp = cognito.admin_get_user(UserPoolId=user_pool_id, Username=username)
        for attr in resp.get("UserAttributes", []):
            if attr.get("Name") == "sub":
                print_msg(f"  user {username} exists (sub={attr['Value']})", "info")
                return attr["Value"]
    except cognito.exceptions.UserNotFoundException:
        pass
    except Exception as err:  # pragma: no cover - surface any other boto error
        print_msg(f"  failed to look up {username}: {err}", "error")
        raise

    try:
        resp = cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=[
                # The pool signs users in by e-mail, so 'email' must be present.
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "phone_number", "Value": f"+{phone}"},
                {"Name": "phone_number_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",  # no invitation e-mail; these users never log in
        )
    except Exception as err:
        print_msg(f"  failed to create {username}: {err}", "error")
        raise

    for attr in resp.get("User", {}).get("Attributes", []):
        if attr.get("Name") == "sub":
            print_msg(f"  created user {username} (sub={attr['Value']})", "success")
            return attr["Value"]
    raise RuntimeError(f"Cognito did not return a sub for {username}")


def upsert_contact(table, actor_id: str, phone: str, role: str, channel: str) -> None:
    table.put_item(
        Item={
            "actor_id": actor_id,
            "phone_identity": phone,
            "role": role,
            "channel": channel,
        }
    )
    print_msg(f"  contact row: {phone} -> {role} (actor={actor_id})", "success")


def cmd_list(table) -> None:
    print_section("Contacts")
    resp = table.scan()
    items = resp.get("Items", [])
    if not items:
        print_msg("Table is empty — run this script without --list to seed it.", "info")
        return
    for item in items:
        print(
            f"  {item.get('phone_identity', '-'):>15}  "
            f"{item.get('role', '-'):<11}  "
            f"{item.get('channel', '-')!s:<9}  {item.get('actor_id', '-')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the conformity contacts table")
    parser.add_argument("--list", action="store_true", help="List current contacts and exit")
    parser.add_argument("--promote", metavar="+PHONE",
                        help="Add/promote a single number to expert")
    parser.add_argument("--channel", default="whatsapp",
                        choices=["whatsapp", "sms"],
                        help="Preferred reply channel stored on the contact row")
    args = parser.parse_args()

    print_section("Conformity contacts seeder")
    stack_cfg = get_stack_config()
    region = stack_cfg["region"]
    outputs = stack_cfg["outputs"]

    table_name = outputs.get("ContactsTableName")
    user_pool_id = outputs.get("CognitoUserPoolId")
    if not table_name:
        print_msg("Stack output ContactsTableName missing — is the stack deployed?", "error")
        sys.exit(1)

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    if args.list:
        cmd_list(table)
        return

    if not user_pool_id:
        print_msg("Stack output CognitoUserPoolId missing — cannot create users.", "error")
        sys.exit(1)

    cognito = boto3.client("cognito-idp", region_name=region)

    if args.promote:
        phones = [normalize_phone(args.promote)]
    else:
        phones = [normalize_phone(p) for p in load_expert_contacts()]
        if not phones:
            print_msg("No expert_contacts in config.yaml; nothing to seed.", "info")
            return

    print(f"Stack:      {stack_cfg['stack_name']}")
    print(f"Table:      {table_name}")
    print(f"User pool:  {user_pool_id}")
    print(f"Channel:    {args.channel}\n")

    for phone in phones:
        if not phone:
            continue
        print(f"{phone}:")
        actor_id = ensure_cognito_user(cognito, user_pool_id, phone)
        upsert_contact(table, actor_id, phone, "expert", args.channel)

    print_section("Done")
    cmd_list(table)


if __name__ == "__main__":
    main()
