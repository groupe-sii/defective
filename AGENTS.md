# AGENTS.md

Sample from the AWS Strands Agents SDK: WhatsApp message → AWS End User Messaging → SNS → one Lambda running a Strands agent on Amazon Bedrock → WhatsApp reply. Being used as a basis for a new project, so expect direct edits rather than upstream merges.

## Layout (first-party code is only under `lambdas/`)

- `lambdas/lambda_function.py` — Lambda entrypoint (`lambda_function.lambda_handler`). Parses SNS records, loads per-day history from DynamoDB, invokes the agent, strips `<thinking>...</thinking>` blocks from the reply.
- `lambdas/strands_agent.py` — wraps `strands.Agent`; rebuilds the agent with stored `messages` + `system_prompt` when history exists.
- `lambdas/tools/` — Strands `@tool` functions: `cards.py` (fake card transactions/payments), `promo.py` (DynamoDB-backed promotions with lazy seed on first read).
- `lambdas/utils/` — `whatsapp.py` (parsing + `socialmessaging.send_whatsapp_message` reply), `dynamo.py`, `locales.py` (system prompt + error strings).
- `template.yaml` — all infra: IAM role, Lambda, SNS topic, 2 DynamoDB tables, customer-managed KMS key. Stack name is fixed: `aws-whatsapp-stack`.
- `pack_lambda.sh` — builds `lambda.zip`: resolves deps for linux with `uv pip compile --python-platform linux` (→ `req-linux.txt`), installs them into `package/` with `pip --no-deps` + manylinux wheels, copies `lambdas/*` on top, zips. `pack_and_deploy.sh` calls it; it `rm -rf package` first, so builds are always clean.
- `mock_send.py` — demo driver for mock mode (see Commands): publishes a fake WhatsApp webhook event to the stack's SNS topic, then tails CloudWatch Logs for the agent's reply. Needs local AWS credentials; `--print-event` works offline without any.
- `package/`, `req-linux.txt`, `lambda.zip` — **build artifacts** (gitignored), not source. Never edit files inside them.

## Commands

- **Build**: `./pack_lambda.sh [python-version]` → `lambda.zip`. Needs bash, `7z`, `pip` on PATH, and `uv` (without uv it falls back to plain pip — only safe on a Linux host; see the header comment for the Windows packaging trap it exists to avoid). `pack_and_deploy.sh` runs it for you.
- **First deploy**: `./pack_and_deploy.sh <s3-bucket> <s3-key> [python-version]` (default `3.11`). Needs bash (Git Bash/WSL on Windows), aws CLI with a region configured (`aws configure get region`), `7z`, `uv`, and a pre-existing S3 bucket. Uploads `lambda.zip`, then runs `aws cloudformation create-stack` — so it **fails against an existing stack**.
- **Redeploy** (stack exists): rebuild, upload `lambda.zip` to a **new prefix** (e.g. `assets/r3`), then:
  `aws cloudformation update-stack --stack-name aws-whatsapp-stack --template-body file://template.yaml --parameters ParameterKey=S3BucketName,ParameterValue=<bucket> ParameterKey=S3BucketPath,ParameterValue=assets/r3 ParameterKey=PythonVersion,ParameterValue=python3.11 ParameterKey=LocaleConfig,ParameterValue=en_US ParameterKey=FoundationModelParam,ParameterValue=eu.amazon.nova-pro-v1:0 ParameterKey=MockWhatsApp,ParameterValue=true --capabilities CAPABILITY_IAM`.
  The prefix **must change**: CloudFormation only calls `UpdateFunctionCode` when the computed `S3Key` differs — re-uploading over the same key is a silent no-op (code is currently deployed from `assets/r2`). Safe alternative to blind `update-stack`: create + inspect + execute a change set first. Or `./destroy.sh` first (deletes only the stack; the S3 bucket is yours).
- **Demo without a phone number (mock mode)**: the `MockWhatsApp` parameter (default `true`) makes `utils/whatsapp.text_reply` log the reply (`MOCK_WHATSAPP: reply to <phone>: ...`) instead of calling `socialmessaging.send_whatsapp_message` — no WhatsApp number, WABA, or Event Destination needed. Drive it end-to-end with `.venv/Scripts/python mock_send.py "message"` (publishes a fake webhook to the stack SNS topic, prints the reply from `/aws/lambda/WhatsApp-Aws-Integration-Main`); flags: `--phone` (reuse a number to demo per-day history), `--text`, `--print-event` (offline), `--tail-only`. Verified: agent replies with tool calls, `<thinking>` stripped, history row written to `WhatsAppUserHistory`. Set `MockWhatsApp=false` once a real number is wired (that's also when the README's Event Destination console step matters).
- Locale and model are **not** passed by the deploy script — they come from defaults in `template.yaml` (`LocaleConfig: en_US`, `FoundationModelParam: eu.amazon.nova-pro-v1:0`, `MockWhatsApp: true`). Edit `template.yaml` before deploying to change them.
- There is **no** test, lint, format, typecheck, or CI config in this repo — don't go looking for `pytest`/`ruff`/workflows. Note `.gitignore` ignores `tests/`, so any tests you write won't be committed unless you un-ignore that path.

## Smoke-checking code locally

Env vars are read at **module import time** (`LOCALE`, `DEFAULT_MODEL`, `USER_HISTORY_TABLE`, `PROMO_TABLE` — normally injected by the Lambda config in `template.yaml`), and `tools/promo.py` builds a boto3 DynamoDB resource at import time. So importing the Lambda code needs both env vars and resolvable AWS credentials (dummy access keys are enough — no network call happens):

```bash
cd lambdas
LOCALE=en_US DEFAULT_MODEL=eu.amazon.nova-pro-v1:0 \
USER_HISTORY_TABLE=WhatsAppUserHistory PROMO_TABLE=PromotionsList \
AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=eu-west-1 \
../.venv/Scripts/python -c "import lambda_function"
```

Verified working from this repo (Python 3.11 `.venv`; README also documents a plain venv + `pip install -r requirements.txt` — both target 3.11).

## Dependencies: two files, keep in sync

- `requirements.txt` is what actually ships in the Lambda: installed with `--platform manylinux2014_x86_64 --only-binary=:all:`, so every runtime dependency must have manylinux wheels (pure Python is fine). Anything you add for Lambda runtime goes here. Pinned `strands-agents>=1.61.1` deliberately — older pins let the Windows packaging fallback silently resolve to 1.1.0.
- `pyproject.toml` + `uv.lock` are for local dev only. Update them alongside `requirements.txt` when adding a runtime dependency.
- `[dependency-groups] dev` in `pyproject.toml` holds local-only tooling: `botocore[crt]`, which boto3 requires when credentials come from `aws login` (otherwise: "Using the login credential provider requires an additional dependency"). `uv sync` installs it; `mock_send.py` needs it.
- `boto3` is deliberately absent from `requirements.txt` — the Lambda runtime provides it (and `uv.lock` pulls it in transitively via strands-agents).
- `req-linux.txt` is generated by `pack_lambda.sh` on every build (gitignored) — never hand-edit it.

## Conventions that differ from defaults

- **i18n lives in four places**: `utils/locales.py` (system prompt + unsupported-type error), the `translations` dicts in `tools/cards.py` and `tools/promo.py`, and the `LocaleConfig` values in `template.yaml`. Adding a locale means touching all four; supported values today are `en_US` and `pt_BR`.
- The SNS topic's customer-managed KMS key is **required**, not optional: AWS End User Messaging can't publish through the AWS-managed `alias/aws/sns` key (see comment in `template.yaml`). Don't remove or swap it.
- Conversation history: one DynamoDB row per phone + day in `WhatsAppUserHistory` (keys `phone_number` / `day`, day format `%Y/%m/%d`); replay uses the stored `messages` and `system_prompt`. The IAM policy references a GSI `PhoneNumberDayIndex` that the template never creates — `DynamoDB.query_with_gsi` is dead code; don't rely on it without adding the GSI.
- Only `text` WhatsApp messages are handled; anything else returns the locale error and a 500.
- Bedrock: the default model is `eu.amazon.nova-pro-v1:0`, an inference profile matching the stack's region (`eu-west-1`) — `us.*` profiles are rejected there with `ValidationException` (verified). Model access must be enabled in `eu-west-1`.
