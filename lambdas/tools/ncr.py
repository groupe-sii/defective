import json
import logging
import os

import boto3
from strands import tool

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")
_runtime = boto3.client("bedrock-agent-runtime")

_MAX_TOP_K = 10
_SUMMARY_CHARS = 1500


@tool
def search_ncr_history(query: str, top_k: int = 5) -> str:
    """
        Search the Non-Conformity Report knowledge base (past NCR history).

        Args:
            query: Short description of the situation, e.g. "P-101 pump seal leak".
            top_k: How many similar reports to retrieve (1-10, default 5).

        Returns a JSON list of similar reports: ref, source, modality (text or image),
        score and summary. Call this BEFORE proposing a procedure so the proposal can
        cite similar past cases.
    """
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
            # Image/binary chunk: never surface raw bytes - use the KB's own
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
