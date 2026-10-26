"""Conformity assistant agent — Strands on Bedrock AgentCore Runtime.

Ported from the POC (``lambdas/strands_agent.py`` + ``lambdas/lambda_function.py``)
into FAST's pattern shape. What changed vs. the POC:

  * The POC's one-shot ``bedrock.agent_invoke`` per SNS record becomes an
    AgentCore Runtime entrypoint that streams a response.
  * Conversation history is no longer a hand-rolled DynamoDB row per identity+day;
    it is AgentCore Memory, keyed by actor (the authenticated Cognito ``sub``).
    Because the actor is the *identity* and not the *channel*, a technician's
    WhatsApp and SMS messages still share one conversation — the same property the
    POC achieved by keying history on a channel-agnostic phone number.
  * Tools are no longer in-process ``@tool`` functions. They are AgentCore Gateway
    MCP tools (separate Lambdas). The agent just gets a gateway MCP client.
  * The sender role is resolved from the JWT identity (see ``tools/session.py``)
    instead of from an ``EXPERT_CONTACTS`` environment variable.

Protocol note: the runtime is deployed with ``protocolConfiguration: HTTP``. The
channel-handler Lambda calls ``bedrock-agentcore:InvokeAgentRuntime`` with a JSON
payload ``{"prompt": ..., "runtimeSessionId": ...}`` and reads the streamed events.
"""

import json
import logging
import os
import re
import sys

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.hooks import BeforeToolCallEvent, HookProvider
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client
from tools.session import ROLE_EXPERT, ROLE_TECHNICIAN, session_note, set_sender
from utils.contacts import resolve_role
from utils.locales import get_prompt, get_technician_rule
from utils.auth import extract_user_id_from_context

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Logged at import: identifies the code revision and SDK actually running in the
# container. The AgentCore Runtime caches warm instances per session, so after a
# deploy a stale instance can keep serving; this line is how to tell whether the
# running container is the one just built.
def _sdk_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:  # pragma: no cover - metadata missing is not fatal
        return "unknown"


logger.info(
    "conformity-agent starting: strands-agents=%s bedrock-agentcore=%s python=%s",
    _sdk_version("strands-agents"),
    _sdk_version("bedrock-agentcore"),
    sys.version.split()[0],
)

LOCALE = os.environ.get("LOCALE", "en_US")

_THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)

_OPEN = "<thinking>"
_CLOSE = "</thinking>"


def _filter_thinking(buffer: str, in_thinking: bool):
    """Filter reasoning out of a partial stream of assistant text.

    Returns ``(emit, in_thinking, remainder)``:

      * ``emit`` — text safe to send now (outside any thinking block),
      * ``in_thinking`` — state to pass to the next call,
      * ``remainder`` — text held back because it might begin a tag.

    A trailing ``<`` or a partial tag is withheld rather than emitted, so a tag
    split across two deltas is never leaked as literal text.
    """
    out = []
    pos = 0
    while pos < len(buffer):
        if in_thinking:
            end = buffer.find(_CLOSE, pos)
            if end == -1:
                # No close tag yet. Keep any trailing characters that could be
                # the start of "</thinking>" — the tag can arrive split across
                # deltas, and dropping them would lose the close tag entirely.
                hold = _partial_tag_len(buffer[pos:], _CLOSE)
                return "".join(out), True, buffer[len(buffer) - hold:] if hold else ""
            pos = end + len(_CLOSE)
            in_thinking = False
            continue

        start = buffer.find(_OPEN, pos)
        if start == -1:
            out.append(buffer[pos:])
            pos = len(buffer)
            break
        out.append(buffer[pos:start])
        pos = start + len(_OPEN)
        in_thinking = True

    # Withhold a trailing partial tag: '<', '<t', ... or '</thinkin'. Without
    # this a tag split across deltas would leak as literal text.
    emit = "".join(out)
    keep = _partial_tag_len(emit, _OPEN, _CLOSE)
    if keep:
        return emit[:-keep], in_thinking, emit[-keep:]
    return emit, in_thinking, ""


def _partial_tag_len(text: str, *tags: str) -> int:
    """Length of ``text``'s tail that is a prefix of one of ``tags``.

    ``0`` when the tail cannot begin any of the tags. Used to hold back a tag
    that is still arriving, without withholding ordinary text or a literal '<'
    that will never become a tag.
    """
    longest = max(len(t) for t in tags)
    for n in range(min(len(text), longest - 1), 0, -1):
        tail = text[-n:]
        if tail.startswith("<") and any(t.startswith(tail) for t in tags):
            return n
    return 0


def remove_thinking_tags(text: str) -> str:
    """Strip <thinking>...</thinking> blocks the model may emit.

    Ported from the POC: Nova wraps its reasoning in these tags and they must
    never reach the technician's phone.

    ``text`` must be a *complete* piece of text. The POC's version was called on
    a whole response; applying it to a single streamed delta cannot work, because
    ``<thinking>`` and ``</thinking>`` arrive in different deltas and neither a
    partial tag nor a half-closed block matches the pattern. The runtime strips
    the assembled text instead — see the entrypoint — and the channel handler
    strips again on the final reply, since the model can still emit a block that
    a single delta-slice would miss.
    """
    try:
        return _THINKING_RE.sub("", text).strip()
    except Exception as err:  # pragma: no cover - defensive, mirrors the POC
        logger.error("Error removing thinking tags: %s", err)
        raise


def _create_session_manager(user_id: str, session_id: str) -> AgentCoreMemorySessionManager:
    """Bind the agent to AgentCore Memory for this actor + session.

    ``actor_id`` is the identity (Cognito sub), so history follows the *person*
    across channels and devices. ``session_id`` is supplied by the caller (our
    channel handler derives it from the identity + UTC day, reproducing the POC's
    "one conversation per identity per day" behaviour).
    """
    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")

    use_ltm = os.environ.get("USE_LONG_TERM_MEMORY", "false").lower() == "true"
    top_k = int(os.environ.get("LTM_TOP_K", "10"))
    relevance_score = float(os.environ.get("LTM_RELEVANCE_SCORE", "0.3"))

    retrieval_config = (
        {"/facts/{actorId}": RetrievalConfig(top_k=top_k, relevance_score=relevance_score)}
        if use_ltm
        else None
    )

    config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=user_id,
        retrieval_config=retrieval_config,
        # Restored history keeps user/assistant text (so the agent remembers the
        # conversation) but drops historical toolUse/toolResult blocks.
        #
        # Without this the model treats an *old* tool result as current truth: a
        # technician who created a case, then asked "status" after the expert had
        # approved it, was answered from the replayed create_case payload
        # ("PENDING_VALIDATION") without ever calling list_cases. Stripping the
        # tool context forces a fresh tool call whenever the answer depends on
        # state.
        filter_restored_tool_context=True,
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"),
    )


def _system_prompt(role: str) -> str:
    """Base locale prompt + the per-request session line (+ role lock).

    The base prompt documents both the technician and the expert workflow, so a
    technician who *claims* to be the expert in the message text can be talked
    into the wrong half of it. Cedar still blocks the call, but the assistant
    would then describe tools it does not have and tell the sender to run them.
    The technician rule pins the role to the authenticated identity; expert
    sessions need no extra rule because the expert section already applies.
    """
    parts = [get_prompt(LOCALE), session_note()]
    if role != ROLE_EXPERT:
        parts.append(get_technician_rule(LOCALE))
    return "\n\n".join(part.strip() for part in parts if part.strip())


def create_conformity_agent(user_id: str, session_id: str) -> Agent:
    """Build the Strands agent: Nova Pro, Gateway tools, AgentCore Memory."""
    model_id = os.environ.get("DEFAULT_MODEL", "eu.amazon.nova-pro-v1:0")
    # Streaming is disabled by default: with tools in play Nova Pro's
    # ConverseStream intermittently aborts the turn with
    #   modelStreamErrorException: Model produced invalid sequence as part of
    #   ToolUse
    # whereas the non-streaming Converse path handles the same prompt and tool
    # schema reliably. Strands still emits the same stream events either way, so
    # the runtime's SSE contract is unchanged. See config.yaml: streaming.
    streaming = os.environ.get("MODEL_STREAMING", "false").lower() == "true"
    bedrock_model = BedrockModel(
        model_id=model_id, temperature=0.1, streaming=streaming
    )

    session_manager = _create_session_manager(user_id, session_id)

    # All domain tools live behind the AgentCore Gateway as MCP tools.
    gateway_client = create_gateway_mcp_client(user_id)

    # The system prompt is set per-request (it embeds the session line), so the
    # value here is only a placeholder for the constructor.
    # Guard against runaway tool loops (e.g. Nova Pro re-issuing the same
    # search with slightly different queries, or re-calling
    # request_expert_validation on a replayed session). Six calls is plenty for
    # the workflow (search, create, request_validation, list, get, send_email).
    # Strands has no `max_iterations` knob, so enforce it with a hook that
    # cancels further tool calls once the budget is spent.
    class _ToolCallLimiter(HookProvider):
        def __init__(self, limit: int = 6):
            self.limit = limit
            self.count = 0

        def register_hooks(self, registry) -> None:
            registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

        def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
            self.count += 1
            if self.count > self.limit:
                event.cancel_tool = (
                    "Tool-call budget exhausted for this turn. Stop calling "
                    "tools and reply with what you have."
                )

    return Agent(
        name="conformity_agent",
        system_prompt=_system_prompt(ROLE_TECHNICIAN),
        tools=[gateway_client],
        model=bedrock_model,
        session_manager=session_manager,
        hooks=[_ToolCallLimiter()],
        trace_attributes={"user.id": user_id, "session.id": session_id},
    )


@app.entrypoint
async def invocations(payload, context: RequestContext):
    """AgentCore Runtime entrypoint — called per inbound channel message.

    Payload:
        prompt (str): the technician's/expert's message text.
        runtimeSessionId (str): conversation session id (identity + day).
        channel (str, optional): whatsapp | sms | rcs — context only.
        senderRole (str, optional): override resolved role (defaults to the
            contacts table lookup below).

    The user id comes from the validated JWT (``sub``), NOT the body.
    """
    user_query = payload.get("prompt")
    session_id = payload.get("runtimeSessionId")
    channel = payload.get("channel", "")

    if not all([user_query, session_id]):
        yield {"status": "error",
               "error": "Missing required fields: prompt or runtimeSessionId"}
        return

    try:
        user_id = extract_user_id_from_context(context)

        # Resolve who is talking before building the agent: the prompt's session
        # line and the (gateway-enforced) role both hang off this. Phone senders
        # are mapped to Cognito identities by the channel handler; the contacts
        # table still carries the authoritative technician/expert role.
        resolved_role = resolve_role(user_id)
        role = payload.get("senderRole") or resolved_role
        if role not in (ROLE_TECHNICIAN, ROLE_EXPERT):
            role = resolved_role
        set_sender(user_id, role=role, channel=channel)

        # If an image accompanied this turn (or was carried over from a recent
        # image-only turn), build a multimodal prompt so the VLM can see the
        # defect. Nova Pro handles images; no model swap needed.
        # The handler downloads the image and passes base64 (no network needed
        # in the runtime); fall back to fetching image_url if b64 is absent.
        prompt = user_query
        image_b64 = payload.get("image_b64") or ""
        image_fmt = payload.get("image_format") or "jpeg"
        image_url = payload.get("image_url") or ""
        if image_b64:
            try:
                import base64
                img_bytes = base64.b64decode(image_b64)
                prompt = [
                    {"text": user_query or "Describe the defect shown in this image and propose a handling procedure."},
                    {"image": {"format": image_fmt, "source": {"bytes": img_bytes}}},
                ]
                logger.info("Loaded image from payload (%d bytes, %s)", len(img_bytes), image_fmt)
            except Exception as err:
                logger.warning("Could not decode image payload: %s", err)
                prompt = (user_query or "") + "\n\n[An image was attached but could not be loaded.]"
        elif image_url:
            try:
                import urllib.request
                with urllib.request.urlopen(image_url, timeout=15) as resp:
                    img_bytes = resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                fmt = "jpeg"
                if "png" in content_type:
                    fmt = "png"
                elif "webp" in content_type:
                    fmt = "webp"
                elif "gif" in content_type:
                    fmt = "gif"
                prompt = [
                    {"text": user_query or "Describe the defect shown in this image and propose a handling procedure."},
                    {"image": {"format": fmt, "source": {"bytes": img_bytes}}},
                ]
                logger.info("Loaded image from %s (%d bytes, %s)", image_url, len(img_bytes), fmt)
            except Exception as err:
                logger.warning("Could not load image %s: %s", image_url, err)
                prompt = (user_query or "") + "\n\n[An image was attached but could not be loaded: %s]" % image_url

        agent = create_conformity_agent(user_id, session_id)
        agent.system_prompt = _system_prompt(role)

        # <thinking> spans several deltas, so it cannot be stripped delta by
        # delta: neither a partial tag nor a half-closed block matches the
        # pattern. Reasoning is instead filtered with a small state machine that
        # tracks whether an opening tag is being buffered or a block is open, so
        # no reasoning text leaves the runtime even mid-stream. The channel
        # handler strips the final reply as well (belt and braces).
        in_thinking = False
        pending = ""

        async for event in agent.stream_async(prompt):
            event = dict(event)
            if isinstance(event.get("data"), str) and event["data"]:
                pending += event["data"]
                clean, in_thinking, pending = _filter_thinking(pending, in_thinking)
                # Emit the event even when the delta was entirely reasoning: the
                # event may carry other fields (tool state, metadata) and
                # dropping it would reorder the stream. An empty '' is harmless.
                event["data"] = clean
            yield json.loads(json.dumps(event, default=str))

    except Exception as err:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(err)}


if __name__ == "__main__":
    app.run()
