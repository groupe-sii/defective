# Conformity Assistant — fork notes

This repo builds a **multichannel non-conformity assistant** on Amazon Bedrock AgentCore,
on top of AWS's Fullstack AgentCore Solution Template.

Everything generic (deployment, memory, gateway, Cedar, streaming, frontend) is covered by the
upstream template's docs. **This file documents only what this project
changes**, because those are the parts the upstream template's docs do not cover.

---

## The use case in one paragraph

A field technician finds a problem on equipment (a leak, a crack, corrosion, a
bad reading) and reports it over **WhatsApp or SMS**. A Strands agent on
AgentCore Runtime searches a Bedrock Knowledge Base of past Non-Conformity
Reports, drafts a handling procedure, and registers a case. A **conformity
expert** — a different person, in their own 1:1 chat — reviews the draft and
approves or rejects it. Once approved, the validated procedure is sent to the
equipment contractor by e-mail. Reply goes back on the channel the message
arrived on.

---

## Where it differs from the upstream template

### 1. An inbound messaging path (the upstream template has none)

The upstream template is a *request-driven web chat*: the browser calls the Runtime. It
has no notion of an unsolicited inbound message. This fork adds that brick:

```
AWS End User Messaging (WhatsApp / SMS)
  └─> SNS topic (customer-managed KMS key — REQUIRED, see below)
        └─> channel-handler Lambda          infra-cdk/lambdas/channel-handler/
              ├─ normalize the envelope      (messages.py)
              ├─ map phone -> Cognito user   (contacts table)
              ├─ InvokeAgentRuntime          (SigV4 + runtime-user-id header)
              └─ dispatch the reply          (dispatcher.py, MOCK_CHANNELS)
```

Nothing about the upstream template's Runtime, Gateway or frontend was removed — the channel
handler is purely additive.

### 2. The Runtime uses IAM SigV4, not a JWT authorizer

The upstream template fronts the Runtime with a Cognito **JWT** authorizer because its caller is
a browser. Our primary caller is the **channel-handler Lambda** — a
service-to-service backend. AWS guidance for that case is IAM SigV4 with the
user propagated via the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header, so
the Runtime is deployed **without** `authorizerConfiguration`.

The trade-off is deliberate and is why `patterns/conformity-agent/utils/auth.py`
reads the user id from the request header first and falls back to a JWT `sub`
(so a future web/ops-console path still works). Both headers are allowlisted.

The **Gateway keeps its JWT authorizer** — that is a separate authorizer and is
what makes the Cedar policy work. The Runtime mints an M2M token for the Gateway
exactly as the upstream template does.

### 3. Roles are data + Cedar, not an environment variable

The POC resolved "is this caller an expert?" from an `EXPERT_CONTACTS`
environment variable and stashed the answer in a module global cleared in a
`finally` block — necessary because Lambda reuses containers, but fragile and
not a security boundary on a concurrent Runtime.

Here:

| Layer | Mechanism | File |
|---|---|---|
| Identity | Inbound phone → Cognito user, via the contacts table | `infra-cdk/lambdas/channel-handler/handler.py` |
| Claim injection | V3 Pre-Token Lambda injects `role` from the contacts table | `infra-cdk/lambdas/pretoken-v3/index.py` |
| Enforcement | Cedar: `record_validation` permitted only for `role == "expert"` | `gateway/policies/policy.cedar` |
| Defence in depth | The tool Lambda re-checks the role | `gateway/tools/case_tool/case_tool_lambda.py` |

Promoting someone to expert is a DynamoDB `put_item` (see
`scripts/seed-contacts.py`), not a redeploy. Cedar is deny-by-default, so a
technician is not merely refused `record_validation` — the tool is **hidden from
their agent session entirely**.

Two wiring details are easy to get wrong, and both fail *silently*:

1. **The Pre-Token Lambda needs the contacts table.** It is created by
   `cognito-construct.ts`, before the backend that owns the table, so
   `fast-main-stack.ts` injects `CONTACTS_TABLE` and grants read access after
   the backend exists. Missing that env var does not error: `_resolve_role`
   falls back to `DEFAULT_ROLE = "technician"`, so every token — the expert's
   included — carries `role=technician`.
2. **The policy engine must be attached to the gateway.** Creating the engine
   and the Cedar policy is only steps 1 and 2 of the documented sequence. Here
   the `cedar-policy` custom resource performs step 3, and the CDK resource
   carries a `DeploymentTimestamp` property so it re-runs on every deploy and
   re-attaches the engine. That is not paranoia: CloudFormation only re-runs a
   custom resource when its properties change, and any gateway update made from
   the template (a new interceptor, say) rewrites the gateway *without*
   `policyEngineConfiguration`, silently detaching Cedar. The mode comes from
   `conformity.policy_engine_mode`. Referencing the engine ARN from the
   `CfnGateway` instead is circular (gateway → policy → targets → gateway).

Verify with `aws bedrock-agentcore-control get-gateway`: it must show the
policy engine ARN and `ENFORCE`.

### 4. History is AgentCore Memory, keyed by person

The POC stored one DynamoDB row per `(identity, day)` and hand-rolled replay
(including `sanitize_decimals`, because DynamoDB returns `Decimal` that Strands'
schema validator rejects).

This fork uses AgentCore Memory with `actor_id` = the Cognito identity. Two
properties fall out for free, one of which the POC had to engineer on purpose:

- a technician's **WhatsApp and SMS messages share one conversation** (history
  is keyed by *person*, not channel) — the same property the POC achieved by
  keying on a channel-agnostic phone number;
- the session survives container restarts.

`session_id` is derived from identity + UTC day by the channel handler,
reproducing the POC's "one conversation per person per day".

**Restored tool context is filtered** (`filter_restored_tool_context=True` in
`basic_agent.py`). User and assistant messages are replayed, but historical
`toolUse`/`toolResult` blocks are dropped. This was a live bug: a technician who
created a case and then asked "status" *after* the expert had approved it was
answered "PENDING_VALIDATION" — the model read the stale `create_case` payload
from memory and never called `list_cases`. Stripping tool context forces a fresh
tool call whenever the answer depends on state.

### 5. Tools are Gateway targets, not in-process `@tool`s

Each POC tool file became an AgentCore Gateway Lambda target with a
`tool_spec.json`:

| Target | Tools | Backing store |
|---|---|---|
| `ncr` | `search_ncr_history` | Bedrock KB `GXY5L19KJX` |
| `case` | `create_case`, `get_case`, `list_cases`, `record_validation` | `NonConformityCases` |
| `mail` | `send_contractor_email`, `request_expert_validation` | `NonConformityCases` + SES |

`lambdas/tools/context.py` is **gone** — its job is done by JWT claims + Cedar.

**The target names matter.** Cedar action names are
`<gatewayTargetName>___<toolName>` (triple underscore). Renaming a target in
`backend-construct.ts` without updating `gateway/policies/policy.cedar` silently
denies every call on that target.

### 5b. Caller identity reaches the tools through a Gateway interceptor

A Lambda target gets no caller identity in `context.client_context.custom` — only
bookkeeping ids and the tool name. So the tool-side role re-check had nothing to
check and denied everyone. A **REQUEST interceptor**
(`gateway/interceptor/interceptor_lambda.py`) fixes that: with
`passRequestHeaders: true` it reads the authenticated M2M JWT and copies its
`user_id`/`role` claims into the `tools/call` arguments as `_caller_identity` /
`_caller_role`. The tool Lambdas pop them in `handler`. Values are written *after*
the model's arguments and overwrite both keys, so a prompt-injected role is
discarded; the contacts table remains the authority for the role.
See `docs/OPEN-IDENTITY-PROPAGATION.md`.

### 6. Region and model

- Region: **eu-west-1** (the upstream template defaults to us-east-1).
- Model: **`eu.amazon.nova-pro-v1:0`** — an inference profile valid in
  eu-west-1. `us.*` profiles are rejected there with `ValidationException`.
  Model access must be enabled in eu-west-1.
- Knowledge Base: `GXY5L19KJX` (`hackaton-nci`, eu-west-1).

All three are in `infra-cdk/config.yaml` under `conformity:`.

### 7. Locales

Prompts exist for `en_US`, `pt_BR` and `fr_FR` in
`patterns/conformity-agent/utils/locales.py`. `en_US` is deployed;
`conformity.locale` in `config.yaml` switches.

---

## Mock mode (why you can demo this without a phone number)

Two independent flags, both in `config.yaml`:

- **`conformity.mock_channels`** (CSV, default `whatsapp,sms,email,rcs`) — a
  channel in the list **logs** its reply (`MOCK_<CHANNEL>: reply to ...`)
  instead of sending. Remove a channel to send for real; each flips separately,
  so WhatsApp can stay mocked while SMS goes live.
- **`conformity.mock_email`** (default `true`) — `send_contractor_email` logs
  (`MOCK_EMAIL ...`) instead of calling SES. The case still advances to
  `CONTRACTOR_NOTIFIED`.

Real sends need provisioning that is deliberately out of scope here: an
origination number for SMS, a WhatsApp Business account, an SES verified
identity.

---

## Knowledge Base: images *and* text NCRs

The KB (`hackaton-nci`, `GXY5L19KJX`) was seeded with the defect dataset images,
so `Retrieve` returned `byteContent` PNGs whose captions live in the
`x-amz-bedrock-kb-description` metadata key.

`scripts/ingest-ncr-text.py` fixes that: it publishes the 92 synthetic NCRs that
the repo already carries (`data/ncr_cable_synthetiques_bundle/`, French, one per
image) to `s3://hackaton-nci-ingest/ncr-text/` and runs an ingestion job.
Retrieval now returns `modality: text` with real prose, and the tool surfaces the
report id (parsed from the `record_id:` header) as `ref`, so the agent can cite
`NCR-SYN-CABLE-BENT-WIRE-000` instead of an opaque chunk id.

`gateway/tools/ncr_tool/ncr_tool_lambda.py` still never surfaces base64: image
chunks fall back to the caption with `modality: image`.

```bash
uv run scripts/ingest-ncr-text.py --dry-run   # what would be uploaded
uv run scripts/ingest-ncr-text.py             # upload + ingest + wait
```

---

## The customer-managed KMS key is not optional

The inbound SNS topic must use a **customer-managed** KMS key. AWS End User
Messaging cannot publish through the AWS-managed `alias/aws/sns` key. This bit
the POC too; do not "simplify" it away.

---

## WhatsApp payload shape (a fixed POC bug)

The real End User Messaging Social event nests the Meta webhook body one level
down, under `entry[]`, and carries it as a **JSON string**:

```json
{
  "context": { "MetaPhoneNumberIds": [{ "metaPhoneNumberId": "...", "arn": ".../phone-number-id/..." }] },
  "whatsAppWebhookEntry": "{\"object\": \"whatsapp_business_account\", \"entry\": [{\"changes\": [...]}]}",
  "messageId": "...",
  "message_timestamp": "..."
}
```

The POC's parser read `webhook_entry.get("changes")` at the top level, and its
`mock_send.py` fixture used a flattened `{"changes": [...]}` body — so the
fixture passed while **real WhatsApp traffic would have parsed to nothing**.
`infra-cdk/lambdas/channel-handler/whatsapp.py` now:

- accepts `whatsAppWebhookEntry` as either a JSON string (real) or a mapping (fixtures), and
- reads `changes` from `entry[].changes` (real) *or* top level (POC fixture).

Two ids appear in a WhatsApp message and are **not** interchangeable:

| id | where | used for |
|---|---|---|
| `phone_number_id` | webhook `metadata` (Meta's id) | matching the message to a context entry |
| origination id (`phone-number-id-...`) | derived from the context ARN | addressing an outbound reply |

`messages.py` deliberately does **not** `normalize_identity()` the WhatsApp
origination id — it is an opaque AWS identifier handed straight to
`send_whatsapp_message(originationPhoneNumberId=...)`, and stripping its
non-digits corrupts it. The SMS destination number *is* normalized.

---

## AgentCore API shapes that only fail at deploy/invoke time

The first successful deploy of this fork surfaced four provider-side constraints
that `cdk synth` and `tsc` cannot catch, because `aws-cdk-lib` shapes do not
validate them and the upstream template never exercises the paths. They are recorded here so the
next change does not rediscover them.

### 1. The Runtime header allowlist has two rules

`requestHeaderConfiguration.allowlistedHeaders` accepts only:

- `Authorization` — **and only** when the runtime has a `customJWTAuthorizer`.
  This runtime is IAM SigV4, so listing it fails `CreateRuntime` with
  *"Authorization header can be specified in requestHeaderAllowlist only when
  runtime is set up with customJWTAuthorizer"*.
- `X-Amzn-Bedrock-AgentCore-Runtime-Custom-*` names.

`X-Amzn-Bedrock-AgentCore-Runtime-User-Id` is **not** an allowlist entry: the
service sets it from the `runtimeUserId` API parameter. The caller identity is
therefore carried in `X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id`, sent by
`infra-cdk/lambdas/channel-handler/handler.py` via a botocore `before-sign`
handler (`invoke_agent_runtime` takes no arbitrary-header argument) and read in
`patterns/conformity-agent/utils/auth.py`. All three must agree on the name.

`runtimeUserId` is still passed — it is what scopes AgentCore Memory per person.

### 2. `create_policy` takes exactly **one** Cedar statement

Passing a document with several `permit` statements fails with:

```
ValidationException: When parsing the policy statement, the following errors
occurred: unexpected token `permit`
```

The parser stops at the end of the first statement and reports the next. The upstream template
ships one statement, so it never hits this. `gateway/policies/policy.cedar`
needs two — most tools are permitted for either role, `record_validation` is
expert-only — and collapsing them would change what is authorised.

`infra-cdk/lambdas/cedar-policy/index.py` splits the document on the `;` that
ends a top-level statement (tracking bracket depth so a nested `;` cannot split
early) and creates one policy per statement, named
`{engine_name}_cp_{timestamp}_{index}`. The `_cp` segment is what the delete
helper keys on, so multi-statement engines stay deletable.

### 3. A failed Create leaks its policy engine

CloudFormation calls `Delete` only on resources whose `Create` returned a
`PhysicalResourceId`. If the Lambda creates the engine and then fails at policy
creation, the engine survives — with an immutable, globally-unique name — and
every later deploy dies immediately with:

```
ConflictException: Policy engine with the same name already exists
```

`_get_or_create_policy_engine` therefore looks the engine up by name and adopts
it, deleting and recreating one stuck in a `*_FAILED` state. The `clientToken`
is derived from the engine name (uuid5) rather than random, so retries of the
same create resolve to the same engine.

### 4. Memory's `sessionId` and `actorId` have different character sets

| field | pattern | colon allowed |
|---|---|---|
| `sessionId` | `[a-zA-Z0-9][a-zA-Z0-9-_]*` | **no** |
| `actorId` | `[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*...` | yes |

The synthetic technician actor is `phone:<digits>`, so it is a valid *actor* id
but an invalid *session* id. `handler.py::_session_id` replaces every character
outside `[a-zA-Z0-9_-]` with `-` (and guards the first character) while staying
deterministic, so the same actor always maps to the same conversation. Passing
the actor id through unchanged is correct — only the session id needs
sanitising.

### 5. The runtime's event stream has two easy-to-miss shapes

`_extract_reply` reads the SSE stream the Runtime returns. Two properties cost a
deploy each to find, and neither is in the upstream template's frontend parser
(`frontend/src/lib/agentcore-client/parsers/strands.ts`), which reads `message`
events and expects a `{"result": {"stop_reason": ...}}` dict:

| event | gotcha |
|---|---|
| `{"data": "...", "delta": {"text": "..."}, ...}` | `data` is the token with **leading whitespace stripped**; only `delta.text` keeps it. Concatenating `data` gives `Hello!HowcanI...` |
| `{"result": "<answer>"}` | a plain **string** holding the complete answer — *not* the dict the frontend parser looks for |

So the reply is read as `result` first, then `delta.text` deltas, then an
assistant `message` (never a user message — those carry tool results).

`<thinking>` blocks: Nova Pro emits its reasoning inline in the assistant text, so
both the deltas and `result` carry `<thinking>...</thinking>`. The runtime
filters them with a delta-spanning state machine (`_filter_thinking` in
`basic_agent.py`) — the original code applied a whole-text regex to each delta,
where it can never match, because the opening and closing tags arrive in
different deltas. The channel handler strips them again on the final reply.

### 6. Model streaming is off, and why

`config.yaml: conformity.streaming` defaults to **false**
(`MODEL_STREAMING` → `BedrockModel(streaming=...)`). With streaming on, Nova Pro
intermittently aborts a tool-using turn:

```
modelStreamErrorException: Model produced invalid sequence as part of ToolUse
```

This is not our schema. The same seven-tool schema and prompt succeed repeatedly
via `Converse`, and 4/4 via Strands itself with `streaming=False`. Strands
synthesises the same stream events from a non-streaming response (verified: the
same event kinds on both paths), so the runtime's SSE contract and
`_extract_reply` are unchanged. `streaming` is a declared `BedrockConfig` field.

Related: `patterns/conformity-agent/requirements.txt` pins a **compatible pair**
(`strands-agents==1.61.1` + `bedrock-agentcore==1.4.7`). The upstream template's baseline pins
`strands-agents==1.32.0`, but `bedrock-agentcore==1.4.7` requires
`strands-agents>=1.56.0` for its strands integration — an inconsistent resolve.
The `streaming` key is present from 1.32.0 through 1.61.1, so the setting itself
is version-robust; the pin is aligned so the container resolves coherently.

Mid-stream failures: the stream can end with
`{"force_stop": true, "force_stop_reason": ...}` plus
`{"status": "error", "error": ...}` and **no** `result` — for example

```
modelStreamErrorException: Model produced invalid sequence as part of ToolUse
```

This is intermittent Nova Pro streaming flakiness, not a schema defect: the same
seven-tool schema and prompt via non-streaming `Converse` succeeds repeatedly.
`_extract_reply` raises `RuntimeReplyError` on it so the caller answers "please
resend" instead of dispatching an empty message.

---

### Hyphens in a tool name break Nova Pro's `Converse`

**A hyphen anywhere in a tool name breaks the whole turn.** The Gateway names
each tool `<targetName>___<toolName>`, and that string is what the model sees.
With a hyphen in it, Nova Pro fails the *first* model call of the turn with

```
ModelErrorException: Model produced invalid sequence as part of ToolUse
```

(even though no tool has been called yet), and with streaming on, the same error
surfaces as `modelStreamErrorException ... ConverseStream ...`.

Bisection proved it, and only this one variable mattered:

| Tool name sent to Nova | Result |
|---|---|
| `search_ncr_history` | OK |
| `gateway_ncr_tool_target___search_ncr_history` | OK |
| `ncr_tool_target___search_ncr_history` | OK (but not deployable — CDK forbids `_`) |
| `ncr-tool-target___search_ncr_history` | **FAIL** |
| `gateway_ncr-tool-target___search_ncr_history` | **FAIL** |

The schema, the prompt and the system prompt were identical across those rows;
the same tool with a hyphen-free name passes 3/3 on the exact prompt that fails
3/3 with hyphens. A tool whose name contains hyphens also breaks when it is the
*only* tool, and hyphens in the *tool* name (not just the target) break it too.

Consequences for this fork:

* **Gateway target names are a single alphanumeric word** (`ncr`, `case`,
  `mail`). They were `ncr-tool-target` etc. (hyphens — breaks Nova) before that.
  Neither form of separator works: hyphens break Nova, and CDK refuses
  underscores with

  ```
  «GatewayTargetNameInvalid» Gateway target name must contain only alphanumeric
  characters and hyphens, with hyphens only between characters
  ```

  so the name has to contain no separator at all. Resulting tool names are
  `ncr___search_ncr_history`, `case___create_case`, and so on — and the runtime's
  `MCPClient(prefix="gateway")` makes them `gateway_ncr___search_ncr_history`,
  which is alphanumeric-plus-underscore and passes.
* Keep tool names in `tool_spec.json` underscore-only as well.
* If a target is ever renamed, `gateway/policies/policy.cedar` must be edited in
  lockstep — Cedar actions are `<targetName>___<toolName>` and Cedar is
  deny-by-default, so a mismatch silently denies the whole target.

A cheap way to check any future tool naming: add the tool and send **one** short
prompt that should trigger it. If it fails on the first model call with
"invalid sequence as part of ToolUse", suspect the name before the schema.

---

## A deploy does not guarantee the container that runs

The AgentCore Runtime keeps **warm instances per session** and updates the
`DEFAULT` endpoint to the new version only when the runtime resource is updated.
A session that is already warm can therefore keep being served by the **previous
image**, even though `GetAgentRuntime` reports the new version as `READY` and
`ListAgentRuntimeEndpoints` reports it as `liveVersion`.

This is how a change that was demonstrably in the image — the pinned
`strands-agents` and the new `basic_agent.py` — appeared not to take effect: the
traceback line numbers in CloudWatch matched the *old* `strands/models/bedrock.py`
and the *old* `basic_agent.py`, while the image in ECR contained the new ones.

Two consequences:

* **Both components log their SDK versions at import** now
  (`basic_agent.py` and `channel-handler/handler.py`). A CloudWatch line is the
  cheap way to tell what is actually running.
* To force a new container, **use a fresh `runtimeSessionId`** — our session id is
  derived from the actor and the UTC day, so reusing the same phone number keeps
  landing on the same warm instance.

### The channel handler's `before-sign` handler is registered once per container

`InvokeAgentRuntime` takes no arbitrary-header argument, so
`infra-cdk/lambdas/channel-handler/handler.py` injects
`X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Id` from inside botocore's signing
pipeline — it has to be present *before* SigV4 runs, which is what
`register_first` buys.

It is registered **once at import** and reads the per-invocation actor from a
`contextvars.ContextVar`. That is not a style choice. The earlier version
registered a fresh handler per call and unregistered it in a `finally`:

```python
handler = events.register_first(event_name, _add_user_id_header)  # returns None
...
events.unregister(event_name, handler)                            # removes nothing
```

`register_first` returns `None`, so the unregister was a no-op. One handler piled
up per invocation in a warm container; from the **second** message onward the
header was added twice, the signer and the wire disagreed about the duplicated
value, and the call failed with

```
AccessDeniedException: The request signature we calculated does not match the
signature you provided
```

which is why a cold start appeared to "fix" it. This was briefly blamed on the
AWS-managed runtime's bundled botocore; it was not — it reproduces locally in two
calls in one process, with either SDK.

The handler still runs on `PythonFunction` with pinned boto3/botocore
(`lambdas/channel-handler/requirements.txt`), which is harmless and keeps the
deployed SDK identifiable from the import banner.

Rule of thumb: **never register a botocore event handler per request.** Register
once, pass per-request state through a `ContextVar`.

---

## Day-one runbook

```bash
# 1. Deploy (CodeBuild does the Docker work — see scripts/README.md)
uv run python scripts/deploy-with-codebuild.py

# 2. Seed the expert contact(s) from config.yaml into Cognito + DynamoDB
uv run scripts/seed-contacts.py

# 3. Drive the whole path with a fake inbound message
uv run test-scripts/test-channel.py "Pump P-101 is leaking at the seal" \
    --phone 5521988887777 --channel whatsapp

# 4. Approve as the expert (their own chat)
uv run test-scripts/test-channel.py "approve NC-20261021-ab12cd" \
    --phone 5511999998888 --channel whatsapp

# 5. Same technician asks for status — try it from SMS to prove shared history
uv run test-scripts/test-channel.py "status?" \
    --phone 5521988887777 --channel sms
```

`test-scripts/test-channel.py --print-event` builds the SNS envelope offline
with no AWS calls at all.

### What was verified live

Last full pass (deploy #18) covered the whole lifecycle on one stack, with the
`mock_channels`/`mock_email` flags on:

| Check | Result |
|---|---|
| WhatsApp inbound → `search_ncr_history` → `create_case` → `request_expert_validation` | reply with case id, row `PENDING_VALIDATION` |
| `created_by` on the case row | `phone:<number>` (gateway interceptor) |
| Expert in their own chat → `record_validation` | `APPROVED`, `validation.approver` = expert actor |
| Technician asks `status` → `get_case` | `APPROVED` (own-case visibility) |
| Technician requests the contractor e-mail → `send_contractor_email` | `CONTRACTOR_NOTIFIED`, mock notification recorded |
| Expert asks for the queue → `list_cases` | the pending case |
| Same number over SMS | recalls the WhatsApp conversation (Memory keyed by person) |
| Technician *claiming* to be the expert | refused, no `record_validation` mentioned, case untouched |

The middle three ran on **one warm channel-handler container** (one `INIT_START`,
three `START` lines) — the sequence that used to fail with a SigV4 mismatch, see
"The channel handler's `before-sign` handler is registered once per container".


The seed step creates the Cognito user with a synthetic e-mail username,
`<phone>@conformity.local`: the user pool is created with
`UsernameAttributes: ['email']` (the upstream template's default), so `AdminCreateUser` rejects a
bare phone number with *"Username should be an email."* Nothing downstream
depends on the username — the channel handler and Pre-Token Lambda key on the
Cognito `sub`, which becomes `actor_id` — so the real number stays on the user's
`phone_number` attribute and on the contacts row's `phone_identity`.

---

## Local `cdk synth` on a machine without Docker

The upstream template builds its Python Lambdas with
`@aws-cdk/aws-lambda-python-alpha`'s `PythonFunction`, which shells out to
Docker **at synth time** to bundle dependencies. On a machine without Docker
(which is why this repo deploys through CodeBuild) `cdk synth` fails with
`spawnSync docker ENOENT`.

This is expected, not a misconfiguration. Options:

- let CodeBuild do it (normal path), or
- validate TypeScript only with `cd infra-cdk && npx tsc --noEmit`, or
- temporarily stub the loader to fake the docker calls.

Do **not** "fix" it by converting `PythonFunction` to plain `lambda.Function` —
that changes dependency bundling (e.g. `pydantic` for the feedback Lambda) and
diverges from the upstream template.
