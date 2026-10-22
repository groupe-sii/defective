# AGENTS.md

A **fork of [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore)** (Fullstack AgentCore Solution Template) carrying one use case: a **multichannel conformity assistant** on Amazon Bedrock AgentCore.

A field technician reports an equipment non-conformity over **WhatsApp or SMS**; a Strands agent searches past Non-Conformity Reports in a Bedrock Knowledge Base, drafts a handling procedure and opens a case; a **conformity expert** approves or rejects it in their own 1:1 chat; the validated procedure is e-mailed to the contractor (mocked by default). The reply returns on the channel the message arrived on.

**Read `docs/CONFORMITY_FORK.md` first** — it documents every deviation from upstream FAST. Everything generic (CDK layout, memory, gateway, Cedar, streaming, frontend) is covered by FAST's own docs in `docs/`.

## Layout

FAST's skeleton is imported at the repo root. First-party code added by this fork is marked **→**.

- `infra-cdk/` — all infrastructure (TypeScript CDK). `config.yaml` drives everything; `lib/backend-construct.ts` is where this fork's resources live.
  - `lib/utils/config-manager.ts` — parses `config.yaml`; the **→ `conformity`** block (region, model, `knowledge_base_id`, locale, mock flags, expert contacts) was added here.
  - `lib/fast-main-stack.ts` — **→** clean top-level outputs (`ChannelTopicArn`, `ContactsTableName`, `CasesTableName`) so scripts can look them up by name.
  - `lambdas/channel-handler/` — **→** the inbound messaging brick FAST does not have: `handler.py` (SNS → normalize → resolve user → `InvokeAgentRuntime` → dispatch), plus ported `messages.py` / `dispatcher.py` / `whatsapp.py`.
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
- `test-scripts/test-channel.py` — **→** the AgentCore port of the POC's `mock_send.py`: builds a WhatsApp/SMS envelope, publishes it to the stack's SNS topic, tails the reply out of CloudWatch.
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
uv run test-scripts/test-channel.py "text" --channel whatsapp --phone <digits>
uv run test-scripts/test-channel.py "text" --print-event    # offline envelope
uv run test-scripts/test-agent.py                # FAST's own agent tester
python scripts/deploy-frontend.py                # ops console (Phase C)
```

`--phone` also picks the role: numbers seeded as experts are experts, everyone else is a technician. Reuse a number to demo shared history; switch `--channel` to prove cross-channel continuity.

### Local `cdk synth` will fail without Docker

Not a misconfiguration. FAST's `PythonFunction` bundles Python deps by shelling out to Docker **at synth time**; without Docker you get `spawnSync docker ENOENT`. Validate with `npx tsc --noEmit`, or let CodeBuild do the real work. Do **not** convert `PythonFunction` to plain `lambda.Function` to work around it — that changes bundling (e.g. feedback's `pydantic`) and diverges from upstream.

## Conventions that differ from defaults

- **The Runtime is IAM SigV4, the Gateway is JWT.** The channel handler is a service-to-service caller, so the user is propagated with `X-Amzn-Bedrock-AgentCore-Runtime-User-Id` (both that and `Authorization` are allowlisted). The Gateway authorizer is untouched — Cedar depends on it. See `patterns/conformity-agent/utils/auth.py`.
- **Role gating is Cedar + data, never an env var.** The POC's `EXPERT_CONTACTS` variable and `tools/context.py` module global are gone. Role lives in the contacts table keyed by Cognito identity; the Pre-Token Lambda injects it as a claim; Cedar permits `record_validation` only for `role == "expert"`; the tool Lambda re-checks. Cedar is deny-by-default, so a technician doesn't even see the tool.
- **Cedar action names are `<gatewayTargetName>___<toolName>`.** Renaming a target in `backend-construct.ts` without editing `policy.cedar` silently denies that whole target.
- **History is AgentCore Memory keyed by person**, not a `(identity, day)` DynamoDB row. Cross-channel sharing and restart-survival come free. The POC's `sanitize_decimals` is unnecessary — there is no hand-rolled replay.
- **`Lambda` sets `AWS_REGION` itself**; `AWS_DEFAULT_REGION` is a *reserved* env var and cannot be set on a Lambda. Lambda-side code reads `AWS_REGION` first. (The Runtime container is not a Lambda and still sets both.)
- **The inbound topic's customer-managed KMS key is required.** AWS End User Messaging cannot publish through `alias/aws/sns`.
- **`config.yaml` is the single source of truth** for the conformity settings (`conformity.region`, `model_id`, `knowledge_base_id`, `locale`, `mock_channels`, `mock_email`, `contractor_email_from`, `expert_contacts`).
- **Two mock flags, independent.** `conformity.mock_channels` (CSV) mocks per channel — listed = log, absent = really send. `conformity.mock_email` separately covers the contractor e-mail.
- **The KB is image-only today.** `ncr_tool_lambda.py` returns the caption from `x-amz-bedrock-kb-description` with `modality: image` and never base64. Ingesting real text NCRs needs no code change.
- **Case lifecycle**: `NonConformityCases` (PK `case_id`, GSIs `created_by-index` + `status-index`, both `ProjectionType: ALL`), `PENDING_VALIDATION → APPROVED|REJECTED → CONTRACTOR_NOTIFIED`, whole-item `put_item`.
- Region/model/KB must stay consistent: `eu-west-1` + `eu.amazon.nova-pro-v1:0` + KB `GXY5L19KJX`. `us.*` inference profiles are rejected in eu-west-1.
- There is **no** test, lint, format, or CI config for first-party code. FAST ships `tests/` and `ruff.toml` but this fork has not adopted them.

## Third-party code

`frontend/`, `docker/`, `tools/`, `vibe-context/`, most of `docs/`, `scripts/{deploy-frontend,deploy-with-codebuild,utils}.py`, and most `infra-cdk/lambdas/*` are stock FAST (Apache-2.0, AWS). When editing them, keep upstream's conventions so future rebases stay clean.
