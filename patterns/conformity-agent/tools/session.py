"""Per-request sender context for the conformity agent.

Replaces the POC's ``lambdas/tools/context.py``. In the POC, the Lambda resolved
the sender role from a phone number and stashed it in a module global that tools
read, then cleared in a ``finally`` block (because Lambda containers are reused).

On AgentCore the situation is different and better:

  * The runtime is long-lived and serves concurrent requests, so a module global
    is not just ugly — it is a correctness bug.
  * The caller is authenticated. AgentCore Runtime validates the JWT and hands the
    runtime a ``RequestContext``; we take the user id from the token's ``sub``
    claim, never from the payload (blocks impersonation via prompt injection).

We therefore keep the sender context in a ``contextvars.ContextVar``: it is
per-async-task, so concurrent requests never see each other's role, and there is
nothing to clear in a ``finally`` block.

The role itself ("technician" vs "expert") is resolved from the ``conformity-
contacts`` DynamoDB mapping table (see ``infra-cdk``), keyed by the authenticated
identity. The gateway tool Lambdas independently re-derive and enforce the role,
so this context is a convenience for prompting — not the security boundary.
"""

import contextvars
from dataclasses import dataclass, field

ROLE_TECHNICIAN = "technician"
ROLE_EXPERT = "expert"


@dataclass
class Sender:
    """Who the agent is currently talking to."""

    identity: str = ""
    role: str = ROLE_TECHNICIAN
    channel: str = ""
    claims: dict = field(default_factory=dict)

    @property
    def is_expert(self) -> bool:
        return self.role == ROLE_EXPERT


_sender: contextvars.ContextVar[Sender] = contextvars.ContextVar(
    "conformity_sender", default=Sender()
)


def set_sender(identity: str, role: str = ROLE_TECHNICIAN, channel: str = "",
               claims: dict | None = None) -> Sender:
    """Bind the sender for the current request. Task-local; no cleanup needed."""
    sender = Sender(identity=identity or "", role=role or ROLE_TECHNICIAN,
                    channel=channel or "", claims=claims or {})
    _sender.set(sender)
    return sender


def get_sender() -> Sender:
    return _sender.get()


def session_note() -> str:
    """The one-line session header appended to the system prompt.

    Kept byte-compatible with the POC's ``[Session] sender_role=...`` line so the
    ported prompts keep working unchanged.
    """
    sender = get_sender()
    if sender.is_expert:
        return f"[Session] sender_role=expert identity={sender.identity}"
    return f"[Session] sender_role=technician identity={sender.identity}"
