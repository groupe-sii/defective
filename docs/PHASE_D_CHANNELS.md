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

## D4 — SMS (deferred)

No origination identity exists. Brazilian (+55) destinations need a local long
code or toll-free number with registration; alphanumeric sender IDs are generally
not supported for BR. Keep `sms` mocked until a number is provisioned; the code
path (`pinpoint-sms-voice-v2 SendTextMessage`) and IAM are already in place.

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
| D0 link the WABA | Meta admin + a number | manual, external |
| D1.1 event destination + D1.5 inbound test | D0 | `scripts/configure-whatsapp.py` |
| D2 flip `mock_channels` + real outbound test | D0 | two phones: technician + expert |
| D3 SES identity + `mock_email: false` | a verified domain | optional for the demo |
| D4 SMS | a provisioned number | out of scope until then |
