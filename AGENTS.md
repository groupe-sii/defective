# AGENTS.md

A **fork of [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore)** (Fullstack AgentCore Solution Template) carrying one use case: a **multichannel conformity assistant** on Amazon Bedrock AgentCore.

A field technician reports an equipment non-conformity over **WhatsApp or SMS**; a Strands agent searches past Non-Conformity Reports in a Bedrock Knowledge Base, drafts a handling procedure and opens a case; a **conformity expert** approves or rejects it in their own 1:1 chat; the validated procedure is e-mailed to the contractor (mocked by default). The reply returns on the channel the message arrived on.

**Read `docs/CONFORMITY_FORK.md` first** — it documents every deviation from upstream FAST. For the hackathon video, `docs/DEMO_SCRIPT.md` is the click-by-click walkthrough with the real-vs-simulated table, and `docs/SMS_FR_SETUP.md` covers the optional real-handset path (sandbox OTP, sender-ID quirks). Everything generic (CDK layout, memory, gateway, Cedar, streaming, frontend) is covered by FAST's own docs in `docs/`.

## Layout

FAST's skeleton is imported at the repo root. First-party code added by this fork is marked **→**.

- `infra-cdk/` — all infrastructure (TypeScript CDK). `config.yaml` drives everything; `lib/backend-construct.ts` is where this fork's resources live.
  - `lib/utils/config-manager.ts` — parses `config.yaml`; the **→ `conformity`** block (region, model, `knowledge_base_id`, locale, mock flags, expert contacts) was added here.
  - `lib/fast-main-stack.ts` — **→** clean top-level outputs (`ChannelTopicArn`, `ContactsTableName`, `CasesTableName`, `TranscriptTableName`) so scripts can look them up by name.
  - `lambdas/channel-handler/` — **→** the inbound messaging brick FAST does not have: `handler.py` (SNS → normalize → resolve user → `InvokeAgentRuntime` → dispatch), plus ported `messages.py` / `dispatcher.py` / `whatsapp.py` and `transcript.py` (both directions of every conversation → `conformity-agent-transcript`, TTL 7 days).
  - `lambdas/pretoken-v3/index.py` — **→** rewritten to resolve `role` from the contacts table (FAST hardcodes a UUID map).
  - `lambdas/{cedar-policy,feedback,oauth2-provider,zip-packager}/` — FAST stock.
- `patterns/conformity-agent/` — **→** this fork's agent (the `backend.pattern` in `config.yaml`).
  - `basic_agent.py` — Runtime entrypoint: Nova Pro, AgentCore Memory, Gateway MCP client.
  - `tools/session.py` — **→** per-request sender context in a `contextvars.ContextVar` (replaces the POC's module-global + `finally: clear_sender()`).
  - `utils/locales.py` — `en_US` / `pt_BR` / `fr_FR` prompts.
  - `utils/{contacts,auth}.py` — **→** role lookup by identity; identity extraction from the runtime header, falling back to a JWT `sub`.
  - `Dockerfile`, `requirements.txt` — **→** adapted from `strands-single-agent` to ship the pattern's own `utils/`.
- `gateway/tools/` — **→** one Lambda per tool group, each with a `tool_spec.json`:
  - `ncr_tool/` → `search_ncr_history` (Bedrock KB)
  - `case_tool/` → `create_case`, `get_case`, `list_cases`, `record_validation`
  - `mail_tool/` → `send_contractor_email`, `request_expert_validation`
- `gateway/interceptor/` — **→** Gateway REQUEST interceptor: copies the
  authenticated M2M JWT's `user_id`/`role` claims into the tool arguments
  (`_caller_identity` / `_caller_role`), because a Lambda target gets no caller
  identity. See `docs/OPEN-IDENTITY-PROPAGATION.md`.
- `gateway/policies/policy.cedar` — **→** rewritten: `record_validation` is expert-gated; everything else is technician-or-expert.
- `scripts/seed-contacts.py` — **→** post-deploy: creates a Cognito user per expert number and seeds the contacts table.
- `scripts/provision-sms.py` — **→** post-deploy: EUM SMS-simulator origination + (optional) FR sender ID and sandbox verified destinations.
- `scripts/smoke-channels.py` — **→** read-only report of which channels are live, with the reason when they are not.
- `scripts/ingest-ncr-text.py` — **→** publishes the synthetic text NCRs to the KB data source and waits for the ingestion job (the KB was image-only before).
- `test-scripts/test-channel.py` — **→** the AgentCore port of the POC's `mock_send.py`: builds a WhatsApp/SMS envelope, publishes it to the stack's SNS topic, tails the reply out of CloudWatch.
- `demo/phone-emulator.html` + `scripts/phone-emulator.py` — **→** local two-phone UI for the demo video: publishes real inbound envelopes, renders replies from the transcript table.
- `poc-legacy/` — **→** the entire pre-AgentCore POC (its `lambdas/`, `template.yaml`, `pack_*.sh`, `mock_send.py`, `destroy.sh`). Kept for reference only; **nothing here is deployed**. `data/` (synthetic NCR bundle) is preserved at the root.
- `frontend/`, `docs/`, `docker/`, `tools/`, `tests/`, `vibe-context/` — FAST stock, pending Phase C (ops console) for the frontend.

## Commands

Deploy is **in-cloud via CodeBuild** — deliberately, because AgentCore Runtime is ARM64-only and a local ARM64 Docker build on Windows is painful:

```bash
python scripts/deploy-with-codebuild.py     # zip source -> CodeBuild -> stack
```

It deploys only git-tracked/staged files, streams logs, and tears down the build resources on success. It does **not** seed contacts.

Other entry points:

```bash
cd infra-cdk && npx tsc --noEmit                 # typecheck (works without Docker)
cd infra-cdk && cdk deploy                       # direct deploy (needs Docker locally)
uv run scripts/seed-contacts.py                  # seed experts + create Cognito users
uv run scripts/seed-contacts.py --list           # inspect the contacts table
uv run scripts/provision-sms.py --simulator      # EUM simulator origination + demo numbers
uv run scripts/provision-sms.py --list           # SMS tier/sender IDs/destinations
uv run scripts/smoke-channels.py                 # which channels are live, and why
uv run scripts/ingest-ncr-text.py                # seed the KB with the 92 text NCRs (idempotent)
uv run test-scripts/test-channel.py "text" --channel whatsapp --phone <digits>
uv run test-scripts/test-channel.py "text" --print-event    # offline envelope
uv run test-scripts/test-agent.py                # FAST's own agent tester
uv run scripts/phone-emulator.py                 # two-phone demo UI (localhost:8787)
python scripts/deploy-frontend.py                # ops console (Phase C)
```

`--phone` also picks the role: numbers seeded as experts are experts, everyone else is a technician. Reuse a number to demo shared history; switch `--channel` to prove cross-channel continuity.

### Local `cdk synth` will fail without Docker

Not a misconfiguration. FAST's `PythonFunction` bundles Python deps by shelling out to Docker **at synth time**; without Docker you get `spawnSync docker ENOENT`. Validate with `npx tsc --noEmit`, or let CodeBuild do the real work. Do **not** convert `PythonFunction` to plain `lambda.Function` to work around it — that changes bundling (e.g. feedback's `pydantic`) and diverges from upstream.

## Conventions that differ from defaults

- **The Runtime is IAM SigV4, the Gateway is JWT.** The channel handler is a service-to-service caller, so the user is propagated with `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` (both that and `Authorization` are allowlisted). The Gateway authorizer is untouched — Cedar depends on it. See `patterns/conformity-agent/utils/auth.py`.
- **Role gating is Cedar + data, never an env var.** The POC's `EXPERT_CONTACTS` variable and `tools/context.py` module global are gone. Role lives in the contacts table keyed by Cognito identity; the Pre-Token Lambda injects it as a claim; Cedar permits `record_validation` only for `role == "expert"`; the tool Lambda re-checks. Cedar is deny-by-default, so a technician doesn't even see the tool.
  - **Three things must line up or the gate is cosmetic.** (1) The Pre-Token Lambda needs `CONTACTS_TABLE` — it lives in `cognito-construct.ts` (created before the backend), so `fast-main-stack.ts` injects the env var and `grantReadData` afterwards. Without it every token says `role=technician` (the silent `DEFAULT_ROLE`) and experts lose `record_validation`. (2) The policy engine must be **attached** to the gateway: creating the engine and the policy is not enough — `GetGateway` showing `policyEngineConfiguration: null` means Cedar is never evaluated, and any later `cdk deploy` that touches the gateway detaches it again. The attach happens in the `cedar-policy` custom resource (step 3 of the AgentCore sequence), and the CDK resource carries a `DeploymentTimestamp` property so it re-attaches on **every** deploy; mode comes from `conformity.policy_engine_mode` (`ENFORCE`, or `LOG_ONLY` to trace without denying). Referencing the engine ARN from `CfnGateway` instead would be circular (gateway → policy → targets → gateway). (3) The target names must match `policy.cedar`.
- **Cedar action names are `<gatewayTargetName>___<toolName>`.** Renaming a target in `backend-construct.ts` without editing `policy.cedar` silently denies that whole target.
- **History is AgentCore Memory keyed by person**, not a `(identity, day)` DynamoDB row. Cross-channel sharing and restart-survival come free. The POC's `sanitize_decimals` is unnecessary — there is no hand-rolled replay.
  - **Restored tool context is filtered.** `AgentCoreMemoryConfig(filter_restored_tool_context=True)` keeps the user/assistant transcript but drops historical toolUse/toolResult blocks. Without it the model answers "status" from a replayed `create_case` payload ("PENDING_VALIDATION") instead of calling `list_cases` — observed live after the expert had already approved the case.
- **`Lambda` sets `AWS_REGION` itself**; `AWS_DEFAULT_REGION` is a *reserved* env var and cannot be set on a Lambda. Lambda-side code reads `AWS_REGION` first. (The Runtime container is not a Lambda and still sets both.)
- **The inbound topic's customer-managed KMS key is required.** AWS End User Messaging cannot publish through `alias/aws/sns`.
- **`config.yaml` is the single source of truth** for the conformity settings (`conformity.region`, `model_id`, `knowledge_base_id`, `locale`, `mock_channels`, `mock_email`, `contractor_email_from`, `expert_contacts`).
- **Two mock flags, independent.** `conformity.mock_channels` (CSV) mocks per channel — listed = log, absent = really send. `conformity.mock_email` separately covers the contractor e-mail.
- **The KB has text NCRs now.** `scripts/ingest-ncr-text.py` publishes the 92 synthetic NCRs from `data/ncr_cable_synthetiques_bundle/` to `s3://hackaton-nci-ingest/ncr-text/` and runs an ingestion job, so `search_ncr_history` returns `modality: text` with a citable `record_id` (`ref`). Image chunks still fall back to the `x-amz-bedrock-kb-description` caption and are never base64.
- **Memory replays the whole session, so replies can drift.** AgentCore keeps one warm container per session (`actor + UTC day`): a freshly deployed image is only picked up by *new* sessions, and a long session makes the model imitate its own earlier wording. When testing a prompt change, use an identity that has not talked today.
- **Case lifecycle**: `NonConformityCases` (PK `case_id`, GSIs `created_by-index` + `status-index`, both `ProjectionType: ALL`), `PENDING_VALIDATION → APPROVED|REJECTED → CONTRACTOR_NOTIFIED`, whole-item `put_item`.
  - **Never add `status` to a `KeyConditionExpression` on `created_by-index`.** That GSI has one key attribute, so a two-condition query is rejected with `ValidationException` — and the model then answers "status" from memory. `list_cases` filters status in Python, and a guessed filter that matches nothing falls back to all of the caller's cases with a note.
- Region/model/KB must stay consistent: `eu-west-1` + `eu.amazon.nova-pro-v1:0` + KB `GXY5L19KJX`. `us.*` inference profiles are rejected in eu-west-1.
- There is **no** test, lint, format, or CI config for first-party code. FAST ships `tests/` and `ruff.toml` but this fork has not adopted them.

## Third-party code

`frontend/`, `docker/`, `tools/`, `vibe-context/`, most of `docs/`, `scripts/{deploy-frontend,deploy-with-codebuild,utils}.py`, and most `infra-cdk/lambdas/*` are stock FAST (Apache-2.0, AWS). When editing them, keep upstream's conventions so future rebases stay clean.
