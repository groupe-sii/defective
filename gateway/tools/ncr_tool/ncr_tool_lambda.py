"""search_ncr_history — AgentCore Gateway Lambda tool.

Ported from the POC's ``lambdas/tools/ncr.py``. Queries the Bedrock Knowledge
Base of past Non-Conformity Reports (NCRs) and returns similar cases the agent
can cite when drafting a handling procedure.

The KB (``hackaton-nci``) is currently image-only: ``Retrieve`` returns
``byteContent`` PNGs whose captions live in the ``x-amz-bedrock-kb-description``
metadata key. We never surface base64 — we return the caption as ``summary`` and
mark ``modality: image``. When real text NCRs are ingested the same code returns
``modality: text`` with no change.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
_runtime = boto3.client("bedrock-agent-runtime", region_name=_REGION)

_MAX_TOP_K = 10
_SUMMARY_CHARS = 1500


def search_ncr_history(query: str, top_k: int = 5) -> str:
    """Search the Non-Conformity Report knowledge base (past NCR history)."""
    if not KNOWLEDGE_BASE_ID:
        return json.dumps({"error": "KNOWLEDGE_BASE_ID is not configured"})
    try:
        try:
            top_k = max(1, min(int(top_k), _MAX_TOP_K))
        except (TypeError, ValueError):
            top_k = 5
        resp = _runtime.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": top_k}
            },
        )
    except Exception as err:
        logger.error("KB retrieve failed: %s", err)
        return json.dumps({"error": f"knowledge base retrieve failed: {err}"})

    results = []
    for hit in resp.get("retrievalResults", []):
        content = hit.get("content") or {}
        meta = hit.get("metadata") or {}
        text = (content.get("text") or "").strip()
        modality = "text"
        if not text:
            # Image/binary chunk: never surface raw bytes — use the KB's own
            # auto-generated description so the agent still gets a usable summary.
            modality = "image"
            text = str(meta.get("x-amz-bedrock-kb-description", "")).strip()
        if not text:
            continue
        results.append(
            {
                "ref": str(meta.get("x-amz-bedrock-kb-chunk-id", ""))[:8],
                "source": str(
                    meta.get("x-amz-bedrock-kb-source-uri")
                    or meta.get("x-amz-bedrock-kb-byte-content-source")
                    or ""
                ),
                "modality": modality,
                "score": round(float(hit.get("score") or 0), 3),
                "summary": text[:_SUMMARY_CHARS],
            }
        )
    if not results:
        return json.dumps({"results": [], "note": "no results found for this query"})
    return json.dumps({"results": results}, default=str)


TOOLS = {"search_ncr_history": search_ncr_history}


def handler(event, context):
    """Gateway Lambda target handler (one Lambda, one or more tools)."""
    logger.info("Received event: %s", json.dumps(event))
    try:
        delimiter = "___"
        original = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = original[original.index(delimiter) + len(delimiter):]
        logger.info("Processing tool: %s", tool_name)

        fn = TOOLS.get(tool_name)
        if fn is None:
            logger.error("Unexpected tool name: %s", tool_name)
            return {"error": f"This Lambda does not implement '{tool_name}'"}

        # Strip the caller-identity arguments added by the Gateway request
        # interceptor (gateway/interceptor/interceptor_lambda.py). This tool
        # needs no identity, and they would otherwise break fn(**event).
        event.pop("_caller_identity", None)
        event.pop("_caller_role", None)

        result = fn(**event)
        return {"content": [{"type": "text", "text": result}]}
    except Exception as err:
        logger.error("Error processing request: %s", err)
        return {"error": f"Internal server error: {err}"}
