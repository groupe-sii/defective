# Identity propagation to Gateway tools (open issue)

**Status:** open — narrow: the tool-Lambda re-check cannot see the caller, so
`record_validation` fails closed for experts too. Cedar itself is correct.
Does **not**
block the Phase A milestone (technician → `search_ncr_history` → reply), which
needs no caller identity.

## What we assumed

That the Gateway forwards the authenticated caller's identity to Lambda targets,
so `gateway/tools/*/*_lambda.py` could read `context.client_context.custom`
and find `user_id` / `role` — the pattern `_caller()` in
`gateway/tools/case_tool/case_tool_lambda.py` implements.

## What actually happens

Per the AWS docs
([Lambda function targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)),
`context.client_context.custom` contains only:

```
bedrockAgentCoreMessageVersion
bedrockAgentCoreAwsRequestId
bedrockAgentCoreMcpMessageId
bedrockAgentCoreGatewayId
bedrockAgentCoreTargetId
bedrockAgentCoreToolName      # "<target-name>___<tool-name>"
```

There is **no** `user_id` and **no** `role`. The Gateway does not forward JWT
claims into the target's Lambda context.

## What does work (verified in the code, not assumed)

`tools/gateway.py` authenticates with an **M2M** token, but it is *not* an
anonymous machine token: `get_gateway_access_token(user_id)` sends the caller's
verified identity as Cognito client metadata —

```python
"aws_client_metadata": json.dumps({"verified_user_id": user_id})
```

`infra-cdk/lambdas/pretoken-v3/index.py` reads exactly that in the
`TokenGeneration_ClientCredentials` flow and injects `user_id` + `role` into the
access token. So **Cedar evaluates the real user's role**, and the
`record_validation` gate is genuine: a technician's token carries
`role=technician`, which the expert-only permit does not match, and the tool is
therefore denied *and* hidden from `tools/list`.

One caveat worth keeping in mind: this relies on the *agent* passing its own
`user_id`. It comes from the runtime header / JWT `sub`, not the model, so it is
not model-settable — but it does mean the Cedar principal is only as trustworthy
as the runtime's identity extraction (`patterns/conformity-agent/utils/auth.py`).

## What is actually broken

Only the **second layer** — the tool Lambda's defence-in-depth re-check.
`_caller()` in `gateway/tools/case_tool/case_tool_lambda.py` reads
`context.client_context.custom`, which has no `user_id`, so:

- `identity=""`;
- its DynamoDB fallback is guarded by `if ... and identity`, so it never runs;
- `role` therefore defaults to `technician`.

`record_validation` refuses **everyone**, including a legitimate expert — the
opposite failure from a security hole, but it makes expert validation
untestable. Cedar would have permitted the expert; the Lambda then denies.

Decision: this is fail-closed and therefore safe to ship. Fix it in Phase B.

## The fix (documented mechanism: Gateway request interceptor)

AWS documents this exact problem and its solution in
[Apply fine-grained access control with Bedrock AgentCore Gateway interceptors](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/).

A **Gateway request interceptor** Lambda sits between the Gateway and the
target. It can:

1. read the inbound request's headers/context, and
2. inject parameters into the request the target receives.

So the plan is:

1. Add a request interceptor to the Gateway (CDK: the Gateway construct takes an
   interceptor configuration).
2. **Enable `inputConfiguration.passRequestHeaders: true` on the interceptor.**
   The AWS Security Blog is explicit that without it the interceptor cannot see
   the inbound request headers at all, and the whole pattern fails. This is the
   easiest step to miss.
3. Have the runtime send the caller's identity + role as request headers on the
   MCP call (it already knows both: `utils/auth.extract_user_id_from_context`
   and `utils.contacts.resolve_role`).
4. The interceptor moves them into `inputSchema` parameters (e.g.
   `_caller_identity`, `_caller_role`) so the tool Lambdas receive them in
   `event`, and strips the headers before the target sees them.
5. Tool Lambdas read those parameters instead of `context.client_context.custom`.
   Keep the contacts-table lookup as the authority: treat the injected role as a
   *claim*, then re-derive from DynamoDB keyed by the injected identity.
6. Cedar needs **no** change — it already sees the real user's role (see above).
   Do not weaken it into "the Lambda is the only check"; keep both layers.

A simpler alternative worth considering first, since the gap is only in the
Lambda's *defence-in-depth* check: have the agent pass the caller identity as a
**tool argument** that the interceptor overwrites. That is strictly weaker than
headers (the model can attempt to set it), so if it is used, the interceptor must
*replace* the value rather than trust it, and the Lambda must still re-derive the
role from DynamoDB. Headers are cleaner; prefer them.

The interceptor handler receives the request under `event["mcp"]["gatewayRequest"]`
(headers in `.headers`), and returns:

```python
{
  "interceptorOutputVersion": "1.0",
  "mcp": {
    "transformedGatewayRequest": {
      "statusCode": 200,
      "headers": {...},           # strip the identity headers here
      "body": { ... },            # with identity injected into the tool arguments
    }
  },
}
```

(Inbound is `transformedGatewayRequest` for a REQUEST interception point;
response interceptors use `transformedGatewayResponse`.)

## Interim safety

Until this lands, the tool Lambdas' role checks must be treated as **not
working** (they fail closed — they deny — which is safe but makes expert
validation untestable). Do not "fix" it by trusting a role from the model's tool
arguments: an LLM can be prompt-injected into passing `role="expert"`. The role
must arrive on a channel the model does not control.
