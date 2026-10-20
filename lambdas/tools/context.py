"""Per-invocation sender context (role/identity/channel).

The Lambda resolves the inbound sender's role from the configured expert
contacts BEFORE invoking the agent and stores it here. Tools that must be
role-gated (e.g. record_validation) read it - prompt instructions alone are
not an authorization mechanism.

Lambda execution environments are reused across invocations, so this state
MUST be set at the start of every message and cleared afterwards.
"""

_state = {"role": "technician", "identity": None, "channel": "whatsapp"}


def set_sender(role: str, identity: str, channel: str = "whatsapp") -> None:
    _state["role"] = role or "technician"
    _state["identity"] = identity
    _state["channel"] = channel or "whatsapp"


def clear_sender() -> None:
    _state["role"] = "technician"
    _state["identity"] = None
    _state["channel"] = "whatsapp"


def get_sender() -> dict:
    return dict(_state)


def session_note() -> str:
    """Line appended to the system prompt so the model knows who it is talking to."""
    return (
        "\n\n[Session] sender_role={role}; sender={identity}; channel={channel}".format(
            role=_state["role"], identity=_state["identity"], channel=_state["channel"]
        )
    )
