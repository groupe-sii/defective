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

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from strands import Agent
from strands.models import BedrockModel
from tools.gateway import create_gateway_mcp_client
from tools.session import ROLE_EXPERT, ROLE_TECHNICIAN, session_note, set_sender
from utils.contacts import resolve_role
from utils.locales import get_prompt
from utils.auth import extract_user_id_from_context

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

LOCALE = os.environ.get("LOCALE", "en_US")

_THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)


def remove_thinking_tags(text: str) -> str:
    """Strip <thinking>...</thinking> blocks the model may emit.

    Ported verbatim from the POC: Nova wraps its reasoning in these tags and
    they must never reach the technician's phone.
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
    )
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=config,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"),
    )


def _system_prompt(role: str) -> str:
    """Base locale prompt + the per-request session line."""
    return f"{get_prompt(LOCALE)}\n\n{session_note()}"


def create_conformity_agent(user_id: str, session_id: str) -> Agent:
    """Build the Strands agent: Nova Pro, Gateway tools, AgentCore Memory."""
    model_id = os.environ.get("DEFAULT_MODEL", "eu.amazon.nova-pro-v1:0")
    bedrock_model = BedrockModel(model_id=model_id, temperature=0.1)

    session_manager = _create_session_manager(user_id, session_id)

    # All domain tools live behind the AgentCore Gateway as MCP tools.
    gateway_client = create_gateway_mcp_client(user_id)

    # The system prompt is set per-request (it embeds the session line), so the
    # value here is only a placeholder for the constructor.
    return Agent(
        name="conformity_agent",
        system_prompt=_system_prompt(ROLE_TECHNICIAN),
        tools=[gateway_client],
        model=bedrock_model,
        session_manager=session_manager,
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

        agent = create_conformity_agent(user_id, session_id)
        agent.system_prompt = _system_prompt(role)

        async for event in agent.stream_async(user_query):
            event = dict(event)
            # Strip <thinking> from streamed deltas before the caller sees them.
            if isinstance(event.get("data"), str):
                event["data"] = remove_thinking_tags(event["data"])
            yield json.loads(json.dumps(event, default=str))

    except Exception as err:
        logger.exception("Agent run failed")
        yield {"status": "error", "error": str(err)}


if __name__ == "__main__":
    app.run()
