#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Ingest the synthetic text NCRs into the Bedrock Knowledge Base.

WHY THIS EXISTS
`search_ncr_history` is the tool that makes the assistant useful: it grounds the
drafted procedure in past Non-Conformity Reports. The deployed KB was seeded with
the *images* of the defect dataset, so retrieval only returned captions
(`x-amz-bedrock-kb-description`) and the agent had almost nothing to cite.

The repo already carries 92 fully written synthetic NCRs in
`data/ncr_cable_synthetiques_bundle/` (`ncr_text`, French, one per image). This
script publishes them next to the images and runs an ingestion job, so the same
tool now returns real prose to quote. No code change in the tool: it already
prefers `content.text` and only falls back to the caption.

Usage:
    uv run scripts/ingest-ncr-text.py --dry-run     # what would be uploaded
    uv run scripts/ingest-ncr-text.py               # upload + ingest + wait
    uv run scripts/ingest-ncr-text.py --no-ingest   # upload only
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import yaml

scripts_dir = Path(__file__).parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

from utils import print_msg, print_section  # noqa: E402

REPO_ROOT = scripts_dir.parent
BUNDLE = REPO_ROOT / "data" / "ncr_cable_synthetiques_bundle" / "ncr_cable_synthetiques.jsonl"
DEFAULT_BUCKET = "hackaton-nci-ingest"
DEFAULT_PREFIX = "ncr-text/"
POLL_SECONDS = 10


def load_config() -> dict:
    with open(REPO_ROOT / "infra-cdk" / "config.yaml", "r") as handle:
        return (yaml.safe_load(handle) or {}).get("conformity") or {}


def load_documents(limit: int = 0) -> list:
    """One document per synthetic NCR, named by its stable record id."""
    records = []
    with open(BUNDLE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not record.get("is_ncr"):
                continue
            text = (record.get("ncr_text") or "").strip()
            if not text:
                continue
            records.append(
                {
                    "key": f"{DEFAULT_PREFIX}{record['record_id']}.txt",
                    "text": text,
                    # A one-line header keeps the defect class searchable even
                    # before embedding (and readable in the S3 console).
                    "record_id": record["record_id"],
                    "defect_class": record.get("defect_class", ""),
                }
            )
            if limit and len(records) >= limit:
                break
    return records


def upload(bucket: str, documents: list, region: str) -> int:
    s3 = boto3.client("s3", region_name=region)

    def put(document: dict) -> bool:
        body = (
            f"record_id: {document['record_id']}\n"
            f"defect_class: {document['defect_class']}\n\n{document['text']}"
        )
        s3.put_object(
            Bucket=bucket,
            Key=document["key"],
            Body=body.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        done = sum(1 for _ in pool.map(put, documents))
    return done


def discover_data_source(agent, knowledge_base_id: str) -> str:
    resp = agent.list_data_sources(knowledgeBaseId=knowledge_base_id)
    summaries = resp.get("dataSourceSummaries", [])
    if not summaries:
        raise SystemExit(f"no data source on knowledge base {knowledge_base_id}")
    return summaries[0]["dataSourceId"]


def wait_for_job(agent, knowledge_base_id: str, data_source_id: str, job_id: str) -> dict:
    import time

    while True:
        resp = agent.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        job = resp["ingestionJob"]
        status = job["status"]
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            return job
        print_msg(f"  ingestion {job_id}: {status}", "info")
        time.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest text NCRs into the knowledge base")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--limit", type=int, default=0, help="only the first N NCRs")
    parser.add_argument("--dry-run", action="store_true", help="list documents, upload nothing")
    parser.add_argument("--no-ingest", action="store_true", help="upload but do not start a job")
    args = parser.parse_args()

    config = load_config()
    region = config.get("region") or "eu-west-1"
    knowledge_base_id = config.get("knowledge_base_id") or ""
    if not knowledge_base_id:
        raise SystemExit("conformity.knowledge_base_id is missing from config.yaml")

    documents = load_documents(args.limit)
    print_section("text NCRs")
    print_msg(f"  documents: {len(documents)}", "info")
    print_msg(f"  target:    s3://{args.bucket}/{DEFAULT_PREFIX}", "info")
    print_msg(f"  kb:        {knowledge_base_id} ({region})", "info")

    if args.dry_run:
        for document in documents[:5]:
            print_msg(f"  {document['key']}  ({len(document['text'])} chars, "
                      f"{document['defect_class']})", "info")
        if len(documents) > 5:
            print_msg(f"  … and {len(documents) - 5} more", "info")
        return

    uploaded = upload(args.bucket, documents, region)
    print_msg(f"uploaded {uploaded} documents", "success")

    if args.no_ingest:
        return

    agent = boto3.client("bedrock-agent", region_name=region)
    data_source_id = discover_data_source(agent, knowledge_base_id)
    started = agent.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
        description="conformity: ingest synthetic text NCRs",
    )
    job_id = started["ingestionJob"]["ingestionJobId"]
    print_msg(f"ingestion job {job_id} started on {data_source_id}", "info")

    job = wait_for_job(agent, knowledge_base_id, data_source_id, job_id)
    stats = job.get("statistics", {})
    print_section("result")
    print_msg(f"  status: {job['status']}", "success" if job["status"] == "COMPLETE" else "error")
    for key in (
        "numberOfDocumentsScanned",
        "numberOfNewDocumentsIndexed",
        "numberOfModifiedDocumentsIndexed",
        "numberOfDocumentsFailed",
    ):
        if key in stats:
            print_msg(f"  {key}: {stats[key]}", "info")
    if job.get("failureReasons"):
        for reason in job["failureReasons"]:
            print_msg(f"  failure: {reason}", "error")
    print_msg("next: uv run test-scripts/test-channel.py \"have we seen a bent wire before?\" "
              "--phone 14254147799", "info")


if __name__ == "__main__":
    main()
