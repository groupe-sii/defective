# Hackathon submission — AWS CDS Partner Hackathon

> Source of truth for the Devpost submission. Copy each `##` section into the
> matching Devpost field. Hackathon page: https://aws-cds-partner.devpost.com/

## Title

Conformity Assistant — Multichannel Agent on Amazon Bedrock AgentCore

## Tagline

A field technician reports an equipment non-conformity by WhatsApp or SMS; a Strands agent searches past Non-Conformity Reports, drafts a handling procedure and opens a case; a conformity expert validates it in their own chat; the validated procedure is e-mailed to the contractor. All on Amazon Bedrock AgentCore, with Cedar-gated expert-only validation.

## Inspiration

Field technicians in industrial plants still report non-conformities on paper or by phone, and the same defect gets re-investigated because nobody can find the previous report. We wanted an assistant that (a) is reachable on the channel a technician already uses (WhatsApp/SMS), (b) grounds its draft procedure in real past reports, (c) keeps a human expert in the loop before anything is sent to a contractor, and (d) shares one conversation across channels so a technician can start on WhatsApp and continue by SMS.

## What it does

- **Technician** sends a message describing a defect (e.g. "Cable C-102 has a damaged outer sheath…"). The agent searches a Bedrock Knowledge Base of 92 past NCRs, cites them by id, drafts a numbered procedure, creates a case, and notifies the expert.
- **Expert** (a different identity, seeded `role=expert`) reviews and replies `approve NC-…` or `reject NC-… <reason>`. Cedar permits `record_validation` only for `role == "expert"`, so a technician cannot even see the tool.
- **Technician** asks `status` → live case state; then `send the email to the contractor` → e-mail dispatched (mocked by default, SES-ready).
- Replies return on the channel the message arrived on; conversation history is shared across channels via AgentCore Memory keyed by person.

## How we built it

- **AgentCore Runtime** (Strands, Nova Pro) with AgentCore Memory (session per person/day, restored tool context filtered so status answers come from live data).
- **AgentCore Gateway** (MCP, JWT authorizer) with a REQUEST interceptor that propagates the caller's `user_id`/`role` from the M2M JWT into tool arguments, because Lambda targets receive no caller identity.
- **Cedar policy engine** attached to the Gateway in `ENFORCE` mode: `record_validation` is expert-only; everything else is technician-or-expert. The engine is re-attached on every deploy (a `DeploymentTimestamp` property forces the custom resource to run).
- **Tools** (one Lambda per group): `search_ncr_history` (Bedrock KB, surfaces `record_id` as `ref`), `create_case`/`get_case`/`list_cases`/`record_validation` (DynamoDB, `created_by-index` + `status-index`), `send_contractor_email`/`request_expert_validation`.
- **Channel handler** (Lambda): normalizes WhatsApp/SMS envelopes, resolves sender → Cognito identity/role via the contacts table, invokes the Runtime with the user-id header, dispatches the reply, writes both directions to a transcript table (7-day TTL) for the demo UI.
- **Infra:** CDK (TypeScript), deployed in-cloud via CodeBuild (ARM64 Runtime). Config-driven (`config.yaml`).
- **Roles are data, not env vars:** the V3 Pre-Token Lambda resolves `role` from the contacts table; promoting someone is a `put_item`.

## Challenges we ran into

- **Cedar created but never enforced:** creating the engine + policy wasn't enough — the Gateway showed `policyEngineConfiguration: null`. Fixed by attaching it inside the custom resource, re-run on every deploy.
- **Pre-Token Lambda had no env vars:** silently defaulted everyone to `role=technician`. Found by reading the deployed Lambda config.
- **AgentCore keeps one warm container per session** that replays history, so the model parroted stale "PENDING_VALIDATION" instead of calling `list_cases`. Fixed with `filter_restored_tool_context=True`, a per-turn `[System directive]` + live DynamoDB data injection, and a tool-call budget hook.
- **`list_cases` ValidationException:** a two-condition `KeyConditionExpression` on a single-key GSI. Fixed by filtering in Python with a fallback when a guessed status filter matches nothing.
- **KB was image-only:** ran an ingestion job for the 92 synthetic text NCRs so `search_ncr_history` returns citable `ref`s.
- **Real SMS/email/WhatsApp inbound:** sandbox, unverified destinations, no WABA, no SES identity — documented as mocks with a self-service SMS path (`docs/SMS_FR_SETUP.md`).

## Accomplishments we're proud of

- A genuinely enforced Cedar policy on the Gateway (not just Lambda-side checks), with an honest smoke test that reports `DETACHED` if it ever regresses.
- The phone emulator + transcript table make the demo recordable without a real handset or WABA.
- The `status` answer is now backed by live DynamoDB data injected into the prompt, so it can't drift even in a warm session.
- `smoke-channels.py` surfaces the two silent-trap failures (detached engine, missing contacts table) that would otherwise be invisible.

## What's next

- Link a WABA and verify an SES identity to take WhatsApp and e-mail live.
- Request SMS production access and register the `CONFORMITE` sender ID.
- Phase C ops console (pending-cases dashboard) on top of the existing FAST chat.
- AgentCore Evaluations for the draft procedure quality.

## Built with

Amazon Bedrock (Nova Pro), Amazon Bedrock AgentCore (Runtime, Gateway, Memory, Cedar policy engine), Amazon Bedrock Knowledge Bases, AWS End User Messaging (SMS), Amazon Cognito, Amazon DynamoDB, AWS Lambda, Amazon SNS, AWS CDK, AWS CodeBuild, AWS Amplify, Strands Agents, Python, TypeScript.

## Try it

```bash
python scripts/deploy-with-codebuild.py
uv run scripts/seed-contacts.py
uv run scripts/ingest-ncr-text.py
uv run scripts/provision-sms.py --simulator
uv run scripts/smoke-channels.py
uv run scripts/phone-emulator.py
```

Repo: https://gitlab.idf.sii.fr/boris.quintana/hackaton (`feat/fast-adoption`)

## Demo video script

See `docs/DEMO_SCRIPT.md` for the click-by-click walkthrough and the real-vs-simulated table.