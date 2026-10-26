# Conformity Assistant — Multichannel Agent on Amazon Bedrock AgentCore

> Built for the **AWS CDS Partner Hackathon** ([devpost](https://aws-cds-partner.devpost.com/)).
> A fork of [FAST](https://github.com/awslabs/fullstack-solution-template-for-agentcore) carrying one
> use case: a **multichannel conformity assistant** for an operations team.

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
`record_validation`, `send_contractor_email`/`request_expert_validation`). The frontend is FAST's stock
React chat (Amplify).

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

```bash
uv run scripts/phone-emulator.py                # two-phone UI at http://localhost:8787
uv run scripts/phone-emulator.py --technician 14254147855 --expert 5511999998888
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

- `docs/CONFORMITY_FORK.md` — every deviation from upstream FAST.
- `docs/DEMO_SCRIPT.md` — click-by-click video walkthrough + real-vs-simulated table.
- `docs/SMS_FR_SETUP.md` — self-service real-handset path (OTP, sender-ID filtering, error table).
- `docs/PHASE_D_CHANNELS.md` — verified channel state.
- `AGENTS.md` — conventions that differ from defaults (Cedar wiring, identity propagation, warm containers, etc.).

## License

Apache-2.0 (inherited from FAST). First-party additions by this fork.