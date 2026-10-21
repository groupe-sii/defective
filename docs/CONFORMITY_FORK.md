# Conformity Assistant — fork notes

This repo is a **fork of [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore)**
(the Fullstack AgentCore Solution Template) carrying a specific use case: a
**multichannel non-conformity assistant** on Amazon Bedrock AgentCore.

Read FAST's own docs under `docs/` for everything generic (deployment, memory,
gateway, Cedar, streaming, frontend). **This file documents only what this fork
changes**, because those are the parts FAST's docs do not cover.

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

## Where it differs from FAST

### 1. An inbound messaging path (FAST has none)

FAST is a *request-driven web chat* template: the browser calls the Runtime. It
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

Nothing about FAST's Runtime, Gateway or frontend was removed — the channel
handler is purely additive.

### 2. The Runtime uses IAM SigV4, not a JWT authorizer

FAST fronts the Runtime with a Cognito **JWT** authorizer because its caller is
a browser. Our primary caller is the **channel-handler Lambda** — a
service-to-service backend. AWS guidance for that case is IAM SigV4 with the
user propagated via the `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header, so
the Runtime is deployed **without** `authorizerConfiguration`.

The trade-off is deliberate and is why `patterns/conformity-agent/utils/auth.py`
reads the user id from the request header first and falls back to a JWT `sub`
(so a future web/ops-console path still works). Both headers are allowlisted.

The **Gateway keeps its JWT authorizer** — that is a separate authorizer and is
what makes the Cedar policy work. The Runtime mints an M2M token for the Gateway
exactly as FAST does.

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

### 5. Tools are Gateway targets, not in-process `@tool`s

Each POC tool file became an AgentCore Gateway Lambda target with a
`tool_spec.json`:

| Target | Tools | Backing store |
|---|---|---|
| `ncr-tool-target` | `search_ncr_history` | Bedrock KB `GXY5L19KJX` |
| `case-tool-target` | `create_case`, `get_case`, `list_cases`, `record_validation` | `NonConformityCases` |
| `mail-tool-target` | `send_contractor_email`, `request_expert_validation` | `NonConformityCases` + SES |

`lambdas/tools/context.py` is **gone** — its job is done by JWT claims + Cedar.

**The target names matter.** Cedar action names are
`<gatewayTargetName>___<toolName>` (triple underscore). Renaming a target in
`backend-construct.ts` without updating `gateway/policies/policy.cedar` silently
denies every call on that target.

### 6. Region and model

- Region: **eu-west-1** (FAST defaults to us-east-1).
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

## Knowledge Base caveat

`hackaton-nci` is currently **image-only**: `Retrieve` returns `byteContent`
PNGs whose captions live in the `x-amz-bedrock-kb-description` metadata key.

`gateway/tools/ncr_tool/ncr_tool_lambda.py` never surfaces base64 — it returns
the caption as `summary` with `modality: image`. When real text NCRs are
ingested the same code returns `modality: text` with **no change**, because it
already prefers `content.text` and only falls back to the caption.

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

## Day-one runbook

```bash
# 1. Deploy (CodeBuild does the Docker work — see scripts/README.md)
python scripts/deploy-with-codebuild.py

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

---

## Local `cdk synth` on a machine without Docker

FAST builds its Python Lambdas with
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
diverges from upstream FAST.
