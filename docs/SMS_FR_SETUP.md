# Real SMS to a French phone — self-service

This is the optional path. **The demo does not need it**: `conformity.sms.demo_identities.technician`
defaults to the EUM simulator success destination (`14254147755`), which really sends
and records delivery events without any phone, OTP or carrier.

Follow this only if you want a real SMS to land on a handset in the video.

## What the pieces are

| Thing | Value | Cost |
|---|---|---|
| Origination for France | alphanumeric sender ID `CONFORMITE` (`Registered: false`) | $0/month |
| Origination for US simulator | `phone-87bc52bcebf244de87fda9b90c42fec1` (`+1 206 555 2684`) | $1/month |
| Account tier | `SANDBOX` — text spend limit $1/month, max 10 verified destinations | — |

Alphanumeric sender IDs need **no registration in France** (the only rule since
2026-03-01 is `a-z A-Z 0-9`, no dashes), but they are **one-way**: the recipient
cannot reply to `CONFORMITE`. That is why inbound stays synthetic.

In the sandbox you may only send to **verified destination numbers**, and the
verification code may only be sent by an origination **from the destination's
country** — which is why the code comes from `CONFORMITE` and not from the US
simulator.

## Step 1 — check the current state

```bash
uv run scripts/provision-sms.py --list
```

Expected lines: the sender ID `CONFORMITE (FR)`, the simulator number, and your
destination with a `status`. `PENDING` means "code sent, not yet confirmed".

## Step 2 — add the number (this sends the code)

```bash
uv run scripts/provision-sms.py --add +33670022195
```

First code per number is free; the record is created with status `PENDING`. The
script is idempotent: running it again on an existing pending record just
**re-sends** a fresh code.

```bash
uv run scripts/provision-sms.py --add +33670022195    # re-send, no side effects
```

## Step 3 — verify quickly

Verification codes expire. If you wait, `--verify` fails with
`ConflictException`; the fix is simply `--add` again and then verify.

```bash
uv run scripts/provision-sms.py --verify +33670022195 --code 123456
```

Success prints the destination as `VERIFIED`.

## Step 4 — prove the send before touching the demo

```bash
# validates origination + destination without spending a message
uv run scripts/provision-sms.py --send +33670022195 --text "conformity test" --dry-run

# really sends
uv run scripts/provision-sms.py --send +33670022195 --text "conformity test"
```

A `MessageId` in the output means EUM accepted it. If a different error appears,
see the table below.

## Step 5 — point the demo at the handset

In `infra-cdk/config.yaml`:

```yaml
conformity:
  sms:
    demo_identities:
      technician: "33670022195"   # digits only, no "+"
      expert: "14254147167"
```

Then deploy; the dispatcher picks `CONFORMITE` automatically because
`origination_by_country.FR` is set — no code change.

```bash
uv run python scripts/deploy-with-codebuild.py
uv run scripts/smoke-channels.py          # should read: sms LIVE
```

To go back to the simulator, restore `technician: "14254147755"`.

## When the code does not arrive

| Symptom | Meaning | What to do |
|---|---|---|
| `--add` succeeds, no SMS after a few minutes | carrier/aggregator filtering of an unregistered alphanumeric sender ID, or plain latency | wait ~10 min, then `--add` again; if two codes never arrive, treat the sender ID as filtered |
| `ConflictException` on `--verify` | the code expired | `--add` again, verify immediately |
| `DESTINATION_PHONE_NUMBER_NOT_VERIFIED` on `--send` | the destination is not `VERIFIED` yet | finish Step 3 |
| `INVALID_IDENTITY_FOR_DESTINATION_COUNTRY` | you are sending from an origination in the wrong country (`FR` destination from a US simulator, or the reverse) | check `conformity.sms.origination_by_country` |
| `ResourceNotFoundException` | the id/ARN you passed does not exist in that region | `--list` and copy the id, not the console ARN, into config |
| spend-limit error | sandbox cap is $1/month | wait for the month to roll over, or request production access |

### If `CONFORMITE` is filtered by your carrier

France has no self-service long code in this account (`RequestPhoneNumber(FR,
LONG_CODE)` answers "This number requires a registration", and no
`FR_LONG_CODE_*` / `FR_SENDER_ID_*` registration type exists). The realistic
options, in order of effort:

1. **Use the simulator for the demo** — zero effort, and `--sim` keeps the flow
   real up to the carrier.
2. **Request SMS production access** (Support → Service Quotas → "SMS
   Production Access") for `pinpoint-sms-voice-v2`. This removes the
   verified-destination list and the $1 cap, but does not change sender-ID
   filtering.
3. **Buy a French long code through a registration** (needs French company
   details) or a **US toll-free number** with production access — then set it as
   `sms.origination_identity` and drop the `FR` override.

## Where to look when it misbehaves

- `uv run scripts/provision-sms.py --list` — tier, sender IDs, destinations, config
- `uv run scripts/smoke-channels.py` — which channels are live and why
- EUM delivery events: CloudWatch metrics in `eu-west-1` for
  `AWS/SMSVoice` (`NumberOfMessagesDelivered`, `NumberOfMessagesFailed`)
- Channel handler logs: `aws logs tail /aws/lambda/conformity-agent-channel-handler --follow`
- Every answered turn is also in `conformity-agent-transcript`, and the
  `dispatch` attribute on the assistant row shows the literal
  `SendTextMessage` response (or the exact exception).
