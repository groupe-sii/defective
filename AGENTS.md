# AGENTS.md

Sample from the AWS Strands Agents SDK: WhatsApp message → AWS End User Messaging → SNS → one Lambda running a Strands agent on Amazon Bedrock → WhatsApp reply. Being used as a basis for a new project, so expect direct edits rather than upstream merges.

## Layout (first-party code is only under `lambdas/`)

- `lambdas/lambda_function.py` — Lambda entrypoint (`lambda_function.lambda_handler`). Parses SNS records, loads per-day history from DynamoDB, invokes the agent, strips `<thinking>...</thinking>` blocks from the reply.
- `lambdas/strands_agent.py` — wraps `strands.Agent`; rebuilds the agent with stored `messages` + `system_prompt` when history exists.
- `lambdas/tools/` — Strands `@tool` functions: `cards.py` (fake card transactions/payments), `promo.py` (DynamoDB-backed promotions with lazy seed on first read).
- `lambdas/utils/` — `whatsapp.py` (parsing + `socialmessaging.send_whatsapp_message` reply), `dynamo.py`, `locales.py` (system prompt + error strings).
- `template.yaml` — all infra: IAM role, Lambda, SNS topic, 2 DynamoDB tables, customer-managed KMS key. Stack name is fixed: `aws-whatsapp-stack`.
- `package/` — **build artifact** (gitignored), not source. `pack_and_deploy.sh` pip-installs `requirements.txt` into it and copies `lambdas/*` on top; it never cleans the folder, so `rm -rf package` when you need a clean build, and never edit files inside it.

## Commands

- Deploy: `./pack_and_deploy.sh <s3-bucket> <s3-key> [python-version]` (default `3.11`). Needs bash (Git Bash/WSL on Windows), aws CLI with a region configured (`aws configure get region`), `7z`, and a pre-existing S3 bucket. Uploads `lambda.zip`, then runs `aws cloudformation create-stack`.
- The script only ever runs `create-stack` — it fails against an existing stack. To redeploy after re-uploading the zip: `aws cloudformation update-stack --stack-name aws-whatsapp-stack --template-body file://template.yaml --parameters ParameterKey=S3BucketName,ParameterValue=... ParameterKey=S3BucketPath,ParameterValue=... ParameterKey=PythonVersion,ParameterValue=python3.11 --capabilities CAPABILITY_IAM`. Or `./destroy.sh` first (deletes only the stack; the S3 bucket is yours).
- Locale and model are **not** passed by the script — they come from defaults at the top of `template.yaml` (`LocaleConfig: en_US`, `FoundationModelParam: us.amazon.nova-pro-v1:0`). Edit `template.yaml` before deploying to change them. After deploy, copy the stack's SNS topic ARN output into AWS End User Messaging → Social → Event Destination manually (one-time, console).
- There is **no** test, lint, format, typecheck, or CI config in this repo — don't go looking for `pytest`/`ruff`/workflows. Note `.gitignore` ignores `tests/`, so any tests you write won't be committed unless you un-ignore that path.
- README says `chmod +x lambda_pack.sh`; that file doesn't exist — it means `pack_and_deploy.sh`.

## Smoke-checking code locally

Env vars are read at **module import time** (`LOCALE`, `DEFAULT_MODEL`, `USER_HISTORY_TABLE`, `PROMO_TABLE` — normally injected by the Lambda config in `template.yaml`), and `tools/promo.py` builds a boto3 DynamoDB resource at import time. So importing the Lambda code needs both env vars and resolvable AWS credentials (dummy access keys are enough — no network call happens):

```bash
cd lambdas
LOCALE=en_US DEFAULT_MODEL=us.amazon.nova-pro-v1:0 \
USER_HISTORY_TABLE=WhatsAppUserHistory PROMO_TABLE=PromotionsList \
AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=us-east-1 \
../.venv/Scripts/python -c "import lambda_function"
```

Verified working from this repo (Python 3.11 `.venv`; README also documents a plain venv + `pip install -r requirements.txt` — both target 3.11).

## Dependencies: two files, keep in sync

- `requirements.txt` is what actually ships in the Lambda: installed with `--platform manylinux2014_x86_64 --only-binary=:all:`, so every runtime dependency must have manylinux wheels (pure Python is fine). Anything you add for Lambda runtime goes here.
- `pyproject.toml` + `uv.lock` are for local dev only. Update them alongside `requirements.txt` when adding a runtime dependency.
- `boto3` is deliberately absent from `requirements.txt` — the Lambda runtime provides it (and `uv.lock` pulls it in transitively via strands-agents).

## Conventions that differ from defaults

- **i18n lives in four places**: `utils/locales.py` (system prompt + unsupported-type error), the `translations` dicts in `tools/cards.py` and `tools/promo.py`, and the `LocaleConfig` values in `template.yaml`. Adding a locale means touching all four; supported values today are `en_US` and `pt_BR`.
- The SNS topic's customer-managed KMS key is **required**, not optional: AWS End User Messaging can't publish through the AWS-managed `alias/aws/sns` key (see comment in `template.yaml`). Don't remove or swap it.
- Conversation history: one DynamoDB row per phone + day in `WhatsAppUserHistory` (keys `phone_number` / `day`, day format `%Y/%m/%d`); replay uses the stored `messages` and `system_prompt`. The IAM policy references a GSI `PhoneNumberDayIndex` that the template never creates — `DynamoDB.query_with_gsi` is dead code; don't rely on it without adding the GSI.
- Only `text` WhatsApp messages are handled; anything else returns the locale error and a 500.
- Bedrock: the default model is a cross-region inference profile (`us.` prefix) — model access must be enabled in both regions.
