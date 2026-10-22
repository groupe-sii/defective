# Identity propagation to Gateway tools

**Status:** resolved — the tool Lambdas now see the authenticated caller.
Cedar was already correct; only the second enforcement layer was blind.

## The problem

A Lambda target never learns who called it. Per the AWS docs
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

No `user_id`, no `role`. So `_caller()` in
`gateway/tools/case_tool/case_tool_lambda.py` resolved to
`{"identity": "", "role": "technician"}` and `record_validation` denied
*everyone* — including a legitimate expert. Fail-closed, but it made expert
validation untestable: case rows had no `created_by`, and only experts could list
the validation queue.

Cedar itself was fine throughout: the runtime mints its M2M token with
`aws_client_metadata={"verified_user_id": user_id}`
(`patterns/conformity-agent/utils/auth.py`), and
`infra-cdk/lambdas/pretoken-v3/index.py` turns that into `user_id` + `role`
claims on the access token. The Gateway's JWT authorizer maps those to Cedar
principal tags, so policy decisions were always based on the real role.

## The fix: a Gateway REQUEST interceptor

`gateway/interceptor/interceptor_lambda.py`, attached in
`infra-cdk/lib/backend-construct.ts` via

```ts
gateway.addInterceptor(
  agentcore.LambdaInterceptor.forRequest(gatewayInterceptorLambda, {
    passRequestHeaders: true,
  })
)
```

Flow:

1. The Gateway authenticates the inbound M2M JWT.
2. It invokes the interceptor with the request under
   `event["mcp"]["gatewayRequest"]` — including the `Authorization` header,
   because `passRequestHeaders: true` (**without this the interceptor sees no
   headers and the whole mechanism silently no-ops**).
3. The interceptor decodes the JWT payload and copies two claims into the
   `tools/call` arguments:

   | Argument | Source claim | Meaning |
   |---|---|---|
   | `_caller_identity` | `user_id` | contacts-table `actor_id` (`phone:…` or a Cognito sub) |
   | `_caller_role` | `role` | `technician` \| `expert` |

4. The target Lambda pops both keys in `handler` before `fn(**event)`, and puts
   them in a `contextvars.ContextVar` for the tools to read.

`ncr_tool` and `mail_tool` also pop the keys even though they do not use
identity, so the extra arguments can never break `fn(**event)`.

### Why the arguments and not headers

A Lambda target only receives the tool arguments — headers stop at the Gateway.
The AWS reference implementation for identity propagation does the same thing
(`extended_body["params"]["arguments"]["authorization"] = auth_header`) against
an unmodified tool schema, so the Gateway does not reject arguments that are not
in `toolSpec.inputSchema`. No `tool_spec.json` changed.

### Why this is not "trusting model input"

The interceptor reads the claims from the **authenticated** token, and it
*deletes* both keys from the incoming arguments before writing its own values.
A prompt-injected `{"_caller_role": "expert"}` is therefore discarded whether or
not a valid token is present (verified: a garbled token leaves the arguments
empty rather than keeping the spoofed value).

Two further constraints:

* `sub` is deliberately ignored. On a client-credentials token `sub` is the
  *machine* client id, not an actor id.
* The role is only a claim. `_caller()` re-derives it from the contacts table
  keyed on the injected identity, and uses the claim solely as a fallback when
  the row is missing or the lookup fails. Promote someone by editing the table;
  the JWT claim and the table cannot diverge into extra privilege.

The signature is not re-verified inside the interceptor: the Gateway verified it
before invoking the interceptor, and only the Gateway's execution role has
`lambda:InvokeFunction` on it.

## What this fixes

* `create_case` sets `created_by` — so `created_by-index` queries and the
  technician's own-case visibility (`get_case`, `list_cases`) work.
* `record_validation` passes its Layer-2 check for a real expert and still
  refuses a technician, independently of Cedar.
* Case rows now record `validation.approver`.

## Verification

Deploy, then run the technician path and the expert path:

```bash
uv run test-scripts/test-channel.py "Pump P-101 is leaking at the mechanical seal..." \
    --channel whatsapp --phone 5511944443333
# expert phone, replying in its own 1:1 chat:
uv run test-scripts/test-channel.py "approve NC-20261022-059c16" \
    --channel whatsapp --phone 5511999998888
```

Expected: the case row has `created_by`, and `record_validation` returns
`status: APPROVED` with `approver` set for the expert and a `forbidden` error for
a technician.
