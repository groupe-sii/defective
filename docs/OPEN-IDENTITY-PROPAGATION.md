# Identity propagation to Gateway tools (open issue)

**Status:** open — blocks expert-gated tools (`record_validation`). Does **not**
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

Compounding it: `tools/gateway.py` authenticates to the Gateway with an **M2M**
token (`get_gateway_access_token(user_id)` → client-credentials grant). That is
the *machine* client's token, so:

- the Cedar principal is the machine client, not the technician/expert, and
  `principal.getTag("role")` reads the machine token's claims;
- every user shares one token, so identity cannot ride on it.

Consequences today:

- `_caller()` resolves `identity=""`, so its DynamoDB fallback (which is guarded
  by `if ... and identity`) never runs; `role` defaults to `technician`.
- `record_validation` therefore refuses **everyone**, including the expert.
- Cedar's "technician can't even see `record_validation`" only holds if the
  machine token happens to lack an expert `role` tag.

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
6. For Cedar, decide between (a) a per-user token so the principal really is the
   user, or (b) keeping the M2M principal and doing all role enforcement in the
   tool Lambdas. Note (b) means Cedar's `record_validation` statement is
   effectively no-op — the enforcement that matters is the Lambda re-check.

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
