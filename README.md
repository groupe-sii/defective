# Conformity Assistant — Multichannel Agent on Amazon Bedrock AgentCore

> Built for the **AWS CDS Partner Hackathon** ([devpost](https://aws-cds-partner.devpost.com/)).
> A multichannel conformity assistant for an operations team, built with **Strands-Agents** and Amazon Bedrock on top of AWS End User Messaging.

A field technician reports an equipment non-conformity (leak, crack, corrosion, wrong reading, damage)
over **WhatsApp or SMS**. A Strands agent searches past Non-Conformity Reports in an Amazon Bedrock
Knowledge Base, drafts a handling procedure and opens a case. A **conformity expert** approves or rejects
it from their own 1:1 chat. Once approved, the technician can ask the agent to e-mail the validated
procedure to the equipment contractor (mocked by default). Replies return on the channel the message
arrived on, and the conversation is shared across channels (AgentCore Memory, keyed by person).

## Architecture (one paragraph)

AWS End User Messaging (WhatsApp/SMS) → SNS (customer-managed KMS key) → **channel-handler Lambda**
(normalizes the envelope, resolves the sender to a Cognito identity/role, invokes the AgentCore Runtime
with `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`, dispatches the reply, writes both directions to the
transcript table) → **Strands agent on AgentCore Runtime** (Nova Pro, AgentCore Memory, Gateway MCP
client) → **AgentCore Gateway** (JWT authorizer + Cedar policy engine: `record_validation` is
expert-only) → tool Lambdas (`search_ncr_history` → Bedrock KB, `create_case`/`get_case`/`list_cases`/
`record_validation`, `send_contractor_email`/`request_expert_validation`). The frontend is the React chat (Amplify-deployed).

Editable architecture diagram: [`docs/Conformity_Architecture.drawio`](docs/Conformity_Architecture.drawio) (open with [draw.io](https://app.diagrams.net/)).

## Prerequisites

- AWS account in `eu-west-1`, with Bedrock model access to `eu.amazon.nova-pro-v1:0` and a Bedrock
  Knowledge Base holding past NCRs (default KB id `GXY5L19KJX`).
- Node.js 20+, Python 3.11+, `uv` (or `pip`), Docker (only if you deploy locally instead of CodeBuild).
- A Cognito user pool is created by the stack; no manual setup.
- Optional: an AWS End User Messaging Social (WhatsApp) integration and/or an SMS origination for real
  channels. The demo runs fully in mock mode without any of this.

## Deploy (in-cloud via CodeBuild — recommended)

AgentCore Runtime is ARM64-only; building locally on Windows is painful. The script zips git-tracked
files, builds in CodeBuild, streams logs, and tears down build resources on success:

```bash
python scripts/deploy-with-codebuild.py
```

It does **not** seed contacts. After it succeeds:

```bash
uv run scripts/seed-contacts.py                 # create a Cognito user per expert + seed contacts table
uv run scripts/ingest-ncr-text.py               # publish the 92 synthetic NCRs to the KB (idempotent)
uv run scripts/provision-sms.py --simulator     # EUM simulator origination + demo numbers
uv run scripts/smoke-channels.py                # which channels are live, and why
```

Direct deploy also works if you have Docker locally: `cd infra-cdk && cdk deploy`.

Validate the CDK without Docker: `cd infra-cdk && npx tsc --noEmit`.

## Post-deploy: make a channel real (optional)

- **SMS (real handset, French):** `docs/SMS_FR_SETUP.md` — sandbox OTP, sender-ID quirks, verified
  destinations, production-access request.
- **WhatsApp:** link a WABA → `PutWhatsAppBusinessAccountEventDestinations` → `conformity-agent-inbound`,
  then remove `whatsapp` from `conformity.mock_channels`.
- **Email:** verify an SES identity, set `conformity.mock_email: false`, set `contractor_email_from`.

## Demo

**Video:** [Watch on YouTube](https://www.youtube.com/watch?v=aWs6lpiPDRU) *(unlisted)*

Run the two-phone emulator locally:

```bash
uv run scripts/phone-emulator.py --technician 14254147855 --expert 5511999998888 --image demo/sample.png
# open http://localhost:8787
```

Click-by-click walkthrough with the real-vs-simulated table: **`docs/DEMO_SCRIPT.md`**.

End-to-end via CLI (publishes a realistic inbound envelope to the stack's SNS topic and tails the reply):

```bash
uv run test-scripts/test-channel.py "Cable C-102 has a damaged outer sheath, insulation cracked and copper braid visible. Contractor is CableWorks, dr.boris.quintana@gmail.com. Draft a procedure." --channel sms --phone 14254147855
uv run test-scripts/test-channel.py "approve NC-YYYYMMDD-xxxxxx" --channel sms --phone 5511999998888
uv run test-scripts/test-channel.py "status" --channel sms --phone 14254147855
uv run test-scripts/test-channel.py "send the email to the contractor" --channel sms --phone 14254147855
```

## What is mocked (and why)

| Flag | Default | Effect |
|---|---|---|
| `conformity.mock_channels` | `whatsapp,email,rcs` (sms **live**) | Listed channels log the reply instead of sending. Remove a channel to send for real. |
| `conformity.mock_email` | `false` (live SES) | `send_contractor_email` sends via SES from `contractor_email_from` (verified identity required). |
| RCS | always stubbed | `_send_rcs` logs "rcs-not-provisioned" (no RCS agent registered). |
| Expert push notification | mocked | `request_expert_validation` logs the ping; mirrored into the transcript table so the phone emulator lights up. |

The demo runs fully in mock mode with **no phone number, WABA, or SES setup**. Only SMS outbound is live, and only to verified simulator destinations.

## Project layout

```
infra-cdk/                       # all infrastructure (TypeScript CDK), driven by config.yaml
  config.yaml                    # single source of truth for conformity settings
  lambdas/channel-handler/       # SNS -> normalize -> resolve user -> InvokeAgentRuntime -> dispatch
  lambdas/pretoken-v3/           # injects role claim from the contacts table
gateway/tools/{ncr,case,mail}_tool/   # one Lambda per tool group + tool_spec.json
gateway/interceptor/             # copies JWT user_id/role into tool args
gateway/policies/policy.cedar   # record_validation is expert-only
patterns/conformity-agent/       # the agent (basic_agent.py, locales, tools/session.py)
scripts/                        # deploy, seed-contacts, provision-sms, smoke-channels, ingest-ncr-text, phone-emulator
test-scripts/                   # test-channel.py (AgentCore port of mock_send)
demo/phone-emulator.html        # two-phone (or three with --rcs) demo UI
poc-legacy/                     # the pre-AgentCore POC, reference only
```

## Key docs

- `docs/CONFORMITY_FORK.md` — every deviation from the upstream template.
- `docs/DEMO_SCRIPT.md` — click-by-click video walkthrough + real-vs-simulated table.
- `docs/SMS_FR_SETUP.md` — self-service real-handset path (OTP, sender-ID filtering, error table).
- `docs/PHASE_D_CHANNELS.md` — verified channel state.
- `AGENTS.md` — conventions that differ from defaults (Cedar wiring, identity propagation, warm containers, etc.).

## License

Apache-2.0. First-party additions by this fork.
---

## Hackathon submission (Devpost)

**Title:** Conformity Assistant — Multichannel Agent on Amazon Bedrock AgentCore
**Tagline:** A field technician reports an equipment non-conformity by WhatsApp or SMS; a Strands agent searches past Non-Conformity Reports, drafts a handling procedure and opens a case; a conformity expert validates it in their own chat; the validated procedure is e-mailed to the contractor. All on Amazon Bedrock AgentCore, with Cedar-gated expert-only validation.

### Inspiration

Field technicians in industrial plants still report non-conformities on paper or by phone, and the same defect gets re-investigated because nobody can find the previous report. We wanted an assistant that (a) is reachable on the channel a technician already uses (WhatsApp/SMS), (b) grounds its draft procedure in real past reports, (c) keeps a human expert in the loop before anything is sent to a contractor, and (d) shares one conversation across channels so a technician can start on WhatsApp and continue by SMS.

### What it does

- **Technician** sends a message describing a defect. The agent searches a Bedrock Knowledge Base of 92 past NCRs, cites them by id, drafts a numbered procedure, creates a case, and notifies the expert.
- **Expert** (a different identity, seeded `role=expert`) reviews and replies `approve NC-…` or `reject NC-… <reason>`. Cedar permits `record_validation` only for `role == "expert"`, so a technician cannot even see the tool.
- **Technician** asks `status` → live case state; the expert's approval automatically e-mails the validated procedure to the equipment contractor (real SES send), and the technician is pushed an informative notification.
- Replies return on the channel the message arrived on; conversation history is shared across channels via AgentCore Memory keyed by person.

### How we built it

- **AgentCore Runtime** (Strands, Nova Pro) with AgentCore Memory (session per person/day).
- **AgentCore Gateway** (MCP, JWT authorizer) with a REQUEST interceptor that propagates the caller's `user_id`/`role` from the M2M JWT into tool arguments.
- **Cedar policy engine** attached to the Gateway in `ENFORCE` mode: `record_validation` is expert-only.
- **Tools:** `search_ncr_history` (Bedrock KB), `create_case`/`get_case`/`list_cases`/`record_validation` (DynamoDB), `send_contractor_email`/`request_expert_validation` (SES + transcript mirror).
- **Channel handler** (Lambda): normalizes WhatsApp/SMS envelopes, resolves sender → Cognito identity/role, invokes the Runtime, dispatches the reply, writes both directions to a transcript table.
- **Infra:** CDK (TypeScript), deployed in-cloud via CodeBuild. Config-driven (`config.yaml`).

### Challenges

- Cedar created but never enforced (Gateway showed `policyEngineConfiguration: null`) — fixed by attaching it inside the custom resource, re-run on every deploy.
- Pre-Token Lambda had no `CONTACTS_TABLE` env var — silently defaulted everyone to `role=technician`.
- AgentCore keeps one warm container per session that replays history — fixed with `filter_restored_tool_context=True`, a per-turn directive, and live DynamoDB data injection.
- `list_cases` ValidationException on a single-key GSI — fixed by filtering in Python.
- KB was image-only — ran an ingestion job for 92 text NCRs so `search_ncr_history` returns citable `ref`s.
- Real SMS/email/WhatsApp inbound: sandbox, unverified destinations, no WABA, no SES identity — documented as mocks with a self-service SMS path.

### Accomplishments

- A genuinely enforced Cedar policy on the Gateway, with an honest smoke test.
- The phone emulator + transcript table make the demo recordable without a real handset or WABA.
- The `status` answer is backed by live DynamoDB data injected into the prompt.
- `smoke-channels.py` surfaces the two silent-trap failures.

### What's next

- Link a WABA and verify an SES identity to take WhatsApp and e-mail live.
- Request SMS production access and register the `CONFORMITE` sender ID.
- Ops console (pending-cases dashboard) on top of the existing chat.
- AgentCore Evaluations for draft procedure quality.

### Built with

Amazon Bedrock (Nova Pro), Amazon Bedrock AgentCore (Runtime, Gateway, Memory, Cedar), Amazon Bedrock Knowledge Bases, AWS End User Messaging (SMS), Amazon Cognito, Amazon DynamoDB, AWS Lambda, Amazon SNS, AWS CDK, AWS CodeBuild, AWS Amplify, Strands Agents, Python, TypeScript.

### Try it

```bash
python scripts/deploy-with-codebuild.py
uv run scripts/seed-contacts.py
uv run scripts/ingest-ncr-text.py
uv run scripts/provision-sms.py --simulator
uv run scripts/smoke-channels.py
uv run scripts/phone-emulator.py --technician 14254147920 --expert 5511999998888 --image demo/sample.png
```

### Demo video

**Demo video:** [Watch on YouTube](https://www.youtube.com/watch?v=aWs6lpiPDRU)

Click-by-click walkthrough: **`docs/DEMO_SCRIPT.md`**.

### License

Apache-2.0. First-party additions by this fork.
