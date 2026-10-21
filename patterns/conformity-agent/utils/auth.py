"""Authentication utilities for the conformity agent.

Adapted from ``patterns/utils/auth.py``. Two deliberate differences:

1. **User identity comes from the runtime request header, not the JWT.**

   FAST's runtime is fronted by a Cognito *JWT* authorizer, so the agent reads
   the caller's ``sub`` from the Authorization header. Our primary caller is the
   **channel-handler Lambda** — a service-to-service backend, not a browser. The
   AWS guidance is explicit that service-to-service calls should use **IAM
   SigV4** and propagate the user through the
   ``X-Amzn-Bedrock-AgentCore-Runtime-User-Id`` header (exposed to the runtime as
   ``runtimeUserId``). So the runtime is deployed with the default IAM
   authorizer, and this module reads the user id from the runtime context.

   We still *accept* a JWT ``sub`` as a fallback, so the web/ops-console path
   (which authenticates users with Cognito directly) keeps working unchanged.

2. **The Gateway token flow is unchanged** — the runtime mints an M2M Cognito
   token enriched with the user's claims (``aws_client_metadata`` →
   V3 Pre-Token Lambda → Cedar principal tags). That is how the Gateway knows
   whether the caller is a technician or an expert.

Security note: on the SigV4 path the header is set by the *caller's* IAM principal.
The channel handler is the only principal granted ``bedrock-agentcore:
InvokeAgentRuntimeForUser``, and it derives the user id from the `conformity-
contacts` mapping table — never from the inbound message body. That keeps the
identity chain trustworthy end to end.
"""

import base64
import json
import logging
import os

import boto3
import jwt
import requests

from utils.ssm import get_ssm_parameter

logger = logging.getLogger(__name__)

# AgentCore exposes the propagated user id in this request header.
_RUNTIME_USER_ID_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"


def extract_user_id_from_context(context) -> str:
    """Return the authenticated user id for the current runtime invocation.

    Resolution order:
      1. ``X-Amzn-Bedrock-AgentCore-Runtime-User-Id`` request header
         (SigV4 service-to-service path; set by the channel handler).
      2. The ``sub`` claim of the Authorization JWT (web/ops-console path).

    Raises ``ValueError`` when neither is present — we never fall back to a
    payload-supplied identity, which would allow impersonation via prompt
    injection.
    """
    request_headers = getattr(context, "request_headers", None) or {}

    # Case-insensitive header lookup (header casing is not guaranteed).
    lowered = {k.lower(): v for k, v in request_headers.items()}
    user_id = lowered.get(_RUNTIME_USER_ID_HEADER)
    if user_id:
        logger.info("Extracted user_id from runtime header")
        return user_id

    auth_header = lowered.get("authorization")
    if not auth_header:
        raise ValueError(
            "No user identity found in the request context. Expected either the "
            f"'{_RUNTIME_USER_ID_HEADER}' header (SigV4 callers) or an "
            "Authorization JWT. Ensure the runtime's request header allowlist "
            "includes both headers."
        )

    token = auth_header[7:] if auth_header.startswith("Bearer ") else auth_header
    # Signature verification is skipped on purpose: AgentCore Runtime already
    # validated this token before handing it to the agent.
    claims = jwt.decode(  # nosec B105
        jwt=token,
        options={"verify_signature": False},
        algorithms=["RS256"],
    )
    user_id = claims.get("sub")
    if not user_id:
        raise ValueError("JWT token does not contain a 'sub' claim.")
    logger.info("Extracted user_id from JWT sub")
    return user_id


def get_secret(secret_name: str) -> str:
    """Fetch a secret value from AWS Secrets Manager."""
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def get_gateway_access_token(user_id: str) -> str:
    """Get an M2M OAuth2 token for the Gateway, enriched with the user's claims.

    Identical in spirit to FAST's implementation: call the Cognito
    ``/oauth2/token`` client-credentials endpoint directly so that
    ``aws_client_metadata`` can carry ``verified_user_id``. The V3 Pre-Token
    Lambda reads it and injects the ``role`` claim the Cedar policy evaluates.
    """
    stack_name = os.environ["STACK_NAME"]
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-west-1"))
    logger.info("Getting gateway access token for stack: %s, region: %s", stack_name, region)

    cognito_domain = get_ssm_parameter(f"/{stack_name}/cognito_provider")
    client_id = get_ssm_parameter(f"/{stack_name}/machine_client_id")
    client_secret = get_secret(f"/{stack_name}/machine_client_secret")

    token_url = f"https://{cognito_domain}/oauth2/token"
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    response = requests.post(
        url=token_url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": f"{stack_name}-gateway/read {stack_name}-gateway/write",
            "aws_client_metadata": json.dumps({"verified_user_id": user_id}),
        },
        timeout=30,
    )
    if response.status_code != 200:
        logger.error("Token request failed: %s", response.status_code)
        raise Exception(f"Failed to get access token: {response.status_code} - {response.text}")

    access_token = response.json().get("access_token")
    if not access_token:
        raise Exception("No access_token in Cognito response")
    return access_token
