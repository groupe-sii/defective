# Phase D — real channels

Phase D turns the mocked outbound path into real WhatsApp / e-mail delivery and
accepts real inbound WhatsApp traffic. It is written so that **everything except
the final switch can land before the Meta/EUM link exists**, because the link is
a manual console step with an external lead time.

## Current state (verified 2026-10-22, account 160885249481)

| Check | Result |
|---|---|
| `ListLinkedWhatsAppBusinessAccounts` | **empty in all 17 enabled regions** (incl. `eu-west-1`) |
| CloudTrail `socialmessaging.amazonaws.com` events, 7 days | **0** — no link was ever attempted in this account |
| `DescribePhoneNumbers` (`pinpoint-sms-voice-v2`) | 0 numbers |
| SES v2 identities | 0 |
| SSM `/conformity-agent/*` | no WABA / phone-number parameters |
| Inbound topic | `conformity-agent-inbound`, encrypted with CMK `42a0e180-…` |
| Topic policy | default account-owner statement only — **EUM Social cannot publish** |
| CMK key policy | root-admin only — **EUM Social cannot encrypt** |
| `config.yaml` | `mock_channels: whatsapp,sms,email,rcs`, `mock_email: true` |

So: no channel is provisioned yet. `eu-west-1` **is** a supported EUM Social
region (`social-messaging.eu-west-1.amazonaws.com`), so the single-region design
holds.

## D0 — Link the WABA (manual, ~15 min + Meta verification)

Prerequisites: Meta Business Manager admin, a WhatsApp Business Account, and a
phone number that can receive an SMS or voice call for Meta's verification. Not a
number already active on the WhatsApp consumer app.

Console: **End User Messaging → Social → WhatsApp → Link account**
(`console.aws.amazon.com/social-messaging`, region **eu-west-1**). The AWS-side
API for this is the embedded-signup pair
`AssociateWhatsAppBusinessAccount` (`signupCallback` / `setupFinalization`), so the
console is the practical path.

Record afterwards:

```
WABA id (EUM)            waba-…            arn:aws:social-messaging:eu-west-1:…:waba/…
WABA id (Meta)           <numeric>
EUM phone number id      phone-number-id-…
Meta phone_number_id     <numeric>
E.164 number             +55…
```

Add them to `config.yaml`:

```yaml
conformity:
  whatsapp:
    waba_id: waba-…
    phone_number_id: phone-number-id-…
    number: "+55…"
```

Then confirm with:

```bash
uv run scripts/smoke-channels.py
```

Business verification can take up to two weeks, but **linking and messaging do
not wait for it**: an unverified WABA can message up to 5 allowlisted test
recipients. That is enough for the demo.

## D1 — Real inbound WhatsApp

1. **Event destination** — `PutWhatsAppBusinessAccountEventDestinations` with
   `id=<waba id>` and our SNS topic ARN. Implemented as
   `scripts/configure-whatsapp.py` (idempotent, re-runnable), not a CDK custom
   resource, because the WABA id only exists after the console step.
2. **Topic policy** — add a statement for `Service: socialmessaging.amazonaws.com`
   on `sns:Publish`, conditioned on `aws:SourceAccount` and `aws:SourceArn` (the
   WABA ARN). AWS documents this as required; without it EUM's publish fails.
3. **Key policy** — add `socialmessaging.amazonaws.com` with
   `kms:GenerateDataKey*` / `kms:Decrypt`, same conditions. The topic is encrypted
   with a CMK, so this is not optional.
4. **Dedupe** — EUM can redeliver a webhook. Key on the WhatsApp `message_id`
   (already parsed) in a short-TTL DynamoDB table or a condition on
   `MessagesTable`; today a redelivery would create a second case.
5. **Verify** — message the business number from an allowlisted test phone, expect
   `on whatsapp resolved <digits> as technician (actor=…)` in
   `/aws/lambda/conformity-agent-channel-handler`.

## D2 — Real outbound WhatsApp

1. Drop `whatsapp` from `conformity.mock_channels` → `sms,email,rcs`.
2. **`originationPhoneNumberId`** must be the **EUM** phone number id
   (`phone-number-id-…`), not Meta's `phone_number_id`. `whatsapp.py` derives it
   from the SNS context's `MetaPhoneNumberIds[].arn` last segment; validate
   against a real webhook payload and fall back to
   `conformity.whatsapp.phone_number_id` (env) so a payload-shape change cannot
   silently break replies.
3. **24-hour service window** — free-form text is only allowed within 24 h of the
   user's last message. Every *reply* is inside the window by construction. But
   `request_expert_validation` is **business-initiated** to the expert's number,
   so on a real channel it needs an **approved message template**. For the demo,
   have the expert send one message first (opens the window), or register a
   utility template. This is the one design change real WhatsApp forces.
4. **Error surfacing** — map `socialmessaging` failures (re-engagement required,
   throttling, blocked recipient) to a technician-visible "could not send" reply
   plus a CloudWatch metric, instead of a raised exception.

## D3 — Real e-mail (SES)

Verify an identity in `eu-west-1` (domain preferred, so DKim/SPF are right), move
out of the SES sandbox or verify the contractor address, then set
`mock_email: false` and `contractor_email_from` to that identity.
`send_contractor_email` should record a `notification.status`
(`SENT` / `FAILED` + reason) instead of assuming success.

Order: SES last, because a sandboxed account still needs every recipient verified
and the contractor address is a placeholder today.

## D4 — SMS via the EUM simulator (the demo transport)

SMS is the first channel to go real because it needs nothing external (no Meta,
no WABA, no phone). We run it on the **EUM SMS simulator**, which keeps every
message inside End User Messaging and emits realistic delivery events.

Verified live on 2026-10-22 (account 160885249481, eu-west-1):

| Constraint | Consequence |
|---|---|
| Origination simulators exist **only in the US** | both demo identities are US numbers |
| A simulator origination can only message destination simulators **in its own country** (`INVALID_IDENTITY_FOR_DESTINATION_COUNTRY`) | French destinations are impossible — a French simulator number is rejected |
| Destination simulators are **fixed, publishable numbers** (one "success", one "failure" per country) | no provisioning, no verification, no OTP |
| Account is in the **sandbox** ($1/month) | irrelevant here: simulator destination numbers are exempt from verification |
| A simulator number is **one-way** | inbound stays synthetic (see below) |

Provisioned and wired:

```
origination  +12065552684  id=phone-87bc52bcebf244de87fda9b90c42fec1  ($1/mo)
technician   +14254147755  US "success" destination -> TEXT_DELIVERED
expert       +14254147167  US "failure" destination -> delivery-failure event
```

Both numbers are seeded as *identities*, not devices: the edge map of the whole
system is `phone -> actor`. The technician is an unseeded number (everyone
unknown is a technician); `14254147167` is seeded `role=expert` in the contacts
table, so it can approve. The failure destination is deliberate: it proves the
delivery-failure path and costs nothing.

```bash
uv run scripts/provision-sms.py --simulator       # request/inspect the origination
uv run scripts/provision-sms.py --list            # tier, sender IDs, simulators, config
uv run test-scripts/test-channel.py "Pump P-101 is leaking at the seal"
uv run test-scripts/test-channel.py "approve NC-20261021-ab12cd" --phone 14254147167
```

The demo loop: the inbound envelope is synthetic (a real carrier cannot deliver
to a simulator number), everything after it is the production path — SNS →
channel handler → actor/role resolution → AgentCore Runtime (Nova Pro + Memory +
Gateway MCP tools) → Cedar-gated `create_case` → **real `SendTextMessage`** →
EUM delivery event. `MOCK_SMS` no longer appears because `sms` is out of
`conformity.mock_channels`.

### recording the demo

`scripts/phone-emulator.py` serves `demo/phone-emulator.html`: two phone frames
(technician = success destination, expert = failure destination) for the video.
Typing in a frame publishes the real inbound envelope; the assistant bubbles are
read from `conformity-agent-transcript`, which the channel handler writes for
both directions (TTL 7 days, `TranscriptTableName` stack output). So the UI shows
the agent's actual answers after the real tool calls — only the carrier is
simulated, which is what the EUM simulator is.

```bash
uv run scripts/phone-emulator.py     # http://localhost:8787
```

### the French path (kept for a real deployment)

Also verified live, in case the demo is ever run with a real phone:

* `RequestPhoneNumber(FR, LONG_CODE)` answers "This number requires a
  registration", and **no `FR_LONG_CODE_*` / `FR_SENDER_ID_*` registration type
  exists** — so a French long code is unavailable to this account.
* French **alphanumeric sender IDs need no registration**. `CONFORMITE` is
  provisioned at $0/month (`Registered: false`, 10 msg/s), and satisfies the
  only rule (since 2026-03-01): a-z/A-Z/0-9, no dashes.
* Sandbox: SMS can only reach **verified destination numbers** (max 10), and the
  verification code may only be sent by an origination from the *destination's*
  country. A dry-run `SendTextMessage` from `CONFORMITE` passes origination
  validation and fails only with `DESTINATION_PHONE_NUMBER_NOT_VERIFIED`.
  `--add +33670022195` did send a code from the sender ID (status `PENDING`), so
  a sender ID *is* accepted as the verification origination — the code simply
  did not reach the handset, so the simulator path won.
* Real inbound SMS with French numbers remains impossible (no two-way
  origination); WhatsApp is the only genuine inbound channel.

Setup, if a real phone is ever available:

```bash
uv run scripts/provision-sms.py --add    +33612345678
uv run scripts/provision-sms.py --verify +33612345678 --code 123456
uv run scripts/provision-sms.py --send   +33612345678 --text "hello" --dry-run
```

Then set `conformity.sms.origination_identity` back to the `CONFORMITE` ARN.
Removing the sandbox restrictions (verified-number list, $1 cap) is a Support
quota request for `SMS Production Access`.

## D5 — Hardening (no channel needed, do this first)

* Inbound dedupe by `message_id` (D1.4).
* SNS subscription: raw delivery is on; add a redrive policy + DLQ, and alarm on
  DLQ depth.
* Alarms: channel-handler `Errors`/`Throttles`, runtime 5xx, per-tool Lambda
  errors, Gateway 5xx.
* Idempotent `create_case` (client token from the inbound `message_id`) so a
  retried SNS delivery cannot duplicate a case.
* Structured log line with `case_id`, `actor_id`, `channel`, `tool` for log
  correlation.
* `scripts/smoke-channels.py`: reports link status, event destination, SES
  identity, SMS number, and which channels are mocked, so "is it live?" is one
  command.

## Suggested sequence

| Step | Depends on | Notes |
|---|---|---|
| D5 hardening + D1.2/D1.3 policies + D2.2/D2.4 code | nothing | deployable today, behaviour unchanged while mocked |
| D4 simulator origination + flip `sms` out of `mock_channels` | nothing | **done** — real `SendTextMessage`, EUM events, no phone needed |
| D0 link the WABA | Meta admin + a number | manual, external |
| D1.1 event destination + D1.5 inbound test | D0 | `scripts/configure-whatsapp.py` |
| D2 flip `mock_channels` + real outbound test | D0 | two phones: technician + expert |
| D3 SES identity + `mock_email: false` | a verified domain | optional for the demo |
