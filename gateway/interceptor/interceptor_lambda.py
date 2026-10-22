"""Gateway REQUEST interceptor: propagate the caller's identity to tool Lambdas.

WHY THIS EXISTS
---------------
A Lambda target never sees who called it. ``context.client_context.custom``
carries only AgentCore bookkeeping (message id, gateway id, target id, tool
name) — no ``user_id``, no ``role``. See docs/OPEN-IDENTITY-PROPAGATION.md.

So the tool Lambda's defence-in-depth re-check had nothing to check, and
``record_validation`` denied everybody, including a legitimate expert.

HOW IT WORKS
------------
The Gateway authenticates the inbound JWT, then invokes this interceptor before
the target. With ``inputConfiguration.passRequestHeaders: true`` the event
carries the request headers, including the authenticated ``Authorization``
bearer token. That token is the M2M token minted in
``patterns/conformity-agent/utils/auth.py``, and the V3 Pre-Token Lambda stamps
it with the real caller's ``user_id`` and ``role`` claims — the same claims
Cedar already evaluates.

This function copies those two claims into the tool-call arguments, where the
target Lambda receives them in ``event``:

    "_caller_identity" -> JWT "user_id"   (the contacts-table actor id)
    "_caller_role"     -> JWT "role"      ("technician" | "expert")

SECURITY NOTES
--------------
* The values are read from the *authenticated* token, never from the model.
  They are written **after** any model-supplied arguments and overwrite both
  keys unconditionally, so a prompt-injected ``_caller_role: "expert"`` is
  discarded. That is why the interceptor must set them rather than merely
  default them.
* The signature is not re-verified here. AgentCore Gateway verified it before
  invoking this function, and this function is not publicly invokable (only the
  Gateway role has lambda:InvokeFunction). It is also why ``sub`` is ignored:
  on a client-credentials token ``sub`` is the *machine* client id, which is
  not an actor id. Only the custom ``user_id`` claim is meaningful.
* The role is still only a claim. ``case_tool_lambda._caller()`` re-derives the
  role from the contacts table and treats this value as a fallback, so a wrong
  or absent contacts row never yields more privilege than the table grants.
* Cedar needs no change: it already gates on these claims, independently.
"""

import base64
import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

IDENTITY_ARG = "_caller_identity"
ROLE_ARG = "_caller_role"


def _decode_claims(authorization: str) -> dict:
    """Return the JWT payload claims, or {} if the header is unusable.

    No signature check — see the module docstring for why that is safe here.
    """
    if not authorization:
        return {}
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    # base64url without padding — restore it before decoding.
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception as err:  # noqa: BLE001 - malformed token must not break the call
        logger.warning("interceptor: could not decode JWT payload: %s", err)
        return {}


def _find_header(headers: dict, name: str) -> str:
    """Header lookup that tolerates the Gateway's casing."""
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == lowered:
            return value or ""
    return ""


def lambda_handler(event, context):
    """Inject the caller's identity into tools/call arguments, then pass through."""
    mcp = (event or {}).get("mcp") or {}
    request = mcp.get("gatewayRequest") or {}
    body = request.get("body") or {}
    headers = request.get("headers") or {}

    if body.get("method") == "tools/call":
        params = body.get("params") or {}
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
            params["arguments"] = arguments
        body["params"] = params

        # Drop anything the model may have supplied, whatever the token says.
        arguments.pop(IDENTITY_ARG, None)
        arguments.pop(ROLE_ARG, None)

        claims = _decode_claims(_find_header(headers, "authorization"))
        identity = str(claims.get("user_id") or "")
        role = str(claims.get("role") or "").lower()
        if identity:
            arguments[IDENTITY_ARG] = identity
        if role:
            arguments[ROLE_ARG] = role

        logger.info(
            "interceptor: tool=%s identity=%s role=%s",
            params.get("name", ""),
            identity or "<none>",
            role or "<none>",
        )

    # Headers are passed through untouched: a Lambda target cannot read them, so
    # there is nothing to leak downstream, and echoing the original set avoids
    # dropping a header the Gateway still needs.
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": headers,
                "body": body,
            }
        },
    }
