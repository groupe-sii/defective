#!/usr/bin/env python3
"""Reset the demo's DynamoDB state (cases, transcript, dedupe).

Read-only-ish: only deletes items from the three demo tables. Never touches
contacts (role resolution) or any infrastructure.

IMPORTANT: this does NOT clear AgentCore Memory sessions. The agent keeps one
warm container per (actor, UTC day) and replays that session's history, so a
phone that has already chatted today will still imitate its own earlier wording
(and may still run an older image). Clearing DynamoDB makes the *data* empty
but the *agent behaviour* can still be stale. For a truly fresh demo, also use
identities that have not talked today (see docs/DEMO_SCRIPT.md).

Usage:
    uv run scripts/reset-demo.py --dry-run        # show counts, delete nothing
    uv run scripts/reset-demo.py --confirm        # actually delete
    uv run scripts/reset-demo.py --confirm --keep-cases  # keep cases, clear transcript+dedupe
"""

import argparse
import sys
from pathlib import Path

import boto3

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import get_stack_config, print_msg, print_section  # noqa: E402


def scan_keys(table, pk, sk=None):
    keys = []
    expr_names = {"#pk": pk}
    proj = "#pk"
    if sk:
        expr_names["#sk"] = sk
        proj = "#pk, #sk"
    kwargs = {"ProjectionExpression": proj, "ExpressionAttributeNames": expr_names}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            key = {pk: item[pk]}
            if sk:
                key[sk] = item[sk]
            keys.append(key)
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return keys


def clear_table(table, pk, sk, dry_run):
    keys = scan_keys(table, pk, sk)
    if dry_run:
        return len(keys)
    with table.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    return len(keys)


def main():
    parser = argparse.ArgumentParser(description="Reset demo DynamoDB state")
    parser.add_argument("--confirm", action="store_true",
                        help="actually delete (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show counts only (default)")
    parser.add_argument("--keep-cases", action="store_true",
                        help="keep NonConformityCases, clear transcript + dedupe only")
    args = parser.parse_args()

    dry_run = not args.confirm

    stack = get_stack_config()
    region = stack["region"]
    outputs = stack["outputs"]
    ddb = boto3.resource("dynamodb", region_name=region)

    print_section("reset demo state")
    if dry_run:
        print_msg("DRY RUN - nothing will be deleted. Pass --confirm to delete.", "info")

    tables = []
    cases_name = outputs.get("CasesTableName")
    transcript_name = outputs.get("TranscriptTableName")
    dedupe_name = outputs.get("DedupeTableName")
    if not dedupe_name:
        # The dedupe table is created by the stack but not (yet) exported as a
        # clean output; discover it by name.
        for t in boto3.client("dynamodb", region_name=region).list_tables().get("TableNames", []):
            if "dedupe" in t.lower() and "conformity" in t.lower():
                dedupe_name = t
                break

    if cases_name and not args.keep_cases:
        tables.append(("cases", ddb.Table(cases_name), "case_id", None))
    if transcript_name:
        tables.append(("transcript", ddb.Table(transcript_name), "identity", "ts"))
    if dedupe_name:
        tables.append(("dedupe", ddb.Table(dedupe_name), "pk", "sk"))

    # Discover dedupe key schema if present.
    for name, table, pk, sk in tables:
        if name == "dedupe":
            desc = table.meta.client.describe_table(TableName=table.name)
            ks = desc["Table"]["KeySchema"]
            pk = next(k["AttributeName"] for k in ks if k["KeyType"] == "HASH")
            sk_attr = next((k["AttributeName"] for k in ks if k["KeyType"] == "RANGE"), None)
            tables = [(n, t, (pk if n == "dedupe" else p), (sk_attr if n == "dedupe" else s))
                      for (n, t, p, s) in tables]

    total = 0
    for name, table, pk, sk in tables:
        try:
            n = clear_table(table, pk, sk, dry_run)
        except Exception as err:  # noqa: BLE001
            print_msg("%s: ERROR %s" % (name, err), "error")
            continue
        total += n
        verb = "would delete" if dry_run else "deleted"
        print_msg("%-12s %s %d item(s)" % (name, verb, n), "info" if dry_run else "success")

    print()
    if dry_run:
        print_msg("Total: %d item(s). Re-run with --confirm to delete." % total, "info")
    else:
        print_msg("Total deleted: %d item(s)." % total, "success")
        print_msg("Contacts table untouched. AgentCore Memory sessions are NOT cleared - "
                  "use fresh identities for a truly clean demo.", "warning")


if __name__ == "__main__":
    main()
