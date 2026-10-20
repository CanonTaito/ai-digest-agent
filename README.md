# AI News Digest Agent

A real-world Azure Functions showcase: a serverless agent that scrapes
[The Rundown AI](https://www.therundown.ai) and [TLDR Tech](https://tldr.tech)
every day, filters stories by topic keywords, and emails you a personalised digest.

**Topics tracked:** AI models · Governance · Guardrails & safety · PII & privacy ·
Document processing · AI agents

No LLM calls, no databases, no servers — just a timer trigger, BeautifulSoup,
and Gmail SMTP. Runs on the Azure Functions **Consumption plan** (~$0/month at
1 run/day, covered by the free tier).

## Architecture

```
┌─────────────────────┐   8:00 AM AEST daily (timer trigger)
│  Azure Function     │
│  (Consumption)      │
│                     │
│  1. Fetch HTML ────────► therundown.ai, tldr.tech
│  2. Parse + filter ────► keyword match (topic list in code)
│  3. Dedupe ────────────► Blob Storage (seen_links.json)
│  4. Email ─────────────► Gmail SMTP (port 465)
└─────────────────────┘
```

- **State** (already-seen links) lives in a blob container so the same story
  is never emailed twice — no local files needed.
- **Secrets** (Gmail app password) live in Function App Settings, never in code.

## Repo layout

```
azure-function/        ← deployed to Azure (Timer Trigger, v2 model)
  function_app.py
  requirements.txt
  host.json
digest_agent.py        ← optional local version (Task Scheduler on Windows)
config.example.json    ← config template for the local version
schedule_task.bat      ← registers the local daily 8 AM task
run_digest.bat
```

## Run locally (optional)

```bash
pip install requests beautifulsoup4
copy config.example.json config.json   # fill in Gmail app password
python digest_agent.py
```

Gmail app password: https://myaccount.google.com/apppasswords

## Deploy to Azure

Prerequisites: [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), logged in with `az login`.

```bash
RG=rg-ai-digest-agent
LOC=australiaeast
APP=ai-digest-agent-<unique>
STO=st<unique>

az group create -n $RG -l $LOC
az storage account create -g $RG -n $STO -l $LOC --sku Standard_LRS
az functionapp create -g $RG -n $APP \
  --consumption-plan-location $LOC \
  --runtime python --runtime-version 3.11 \
  --functions-version 4 --os-type Linux \
  --storage-account $STO

az functionapp config appsettings set -g $RG -n $APP --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  ENABLE_ORYX=true \
  GMAIL_FROM="you@gmail.com" \
  GMAIL_TO="you@gmail.com" \
  GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

cd azure-function
Compress-Archive -Path * -DestinationPath deploy.zip -Force
az functionapp deploy -g $RG -n $APP --src-path deploy.zip --type zip
```

`SCM_DO_BUILD_DURING_DEPLOYMENT=true` makes Kudu install `requirements.txt`
during the zip deploy — no `--build-remote` flag needed.

## CI/CD (GitHub Actions)

Every push to `master` that touches `azure-function/` redeploys automatically
via `.github/workflows/deploy.yml`. It authenticates with **OIDC** (no Azure
secret stored in GitHub — trust is scoped to this repo's `master` branch via a
federated credential on the service principal).

Required GitHub secrets:

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | App registration (client) ID |
| `AZURE_TENANT_ID` | Entra tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Subscription GUID |

Logs:

```bash
az functionapp logs logstream -g $RG -n $APP
```

## Tuning

- **Keywords** — edit `TOPIC_KEYWORDS` in `function_app.py` (any headline
  matching one keyword is included; rank = number of matches).
- **Schedule** — default cron `0 0 22 * * *` = 22:00 UTC = 08:00 AEST
  (Queensland). Change in the portal under *Timer trigger* or via
  `NCRONTAB` app setting.
- **Always send** — set `SEND_EVEN_IF_EMPTY=true` to get a "nothing new" email.

## Cost

Consumption plan: 1M free executions/month. This function runs ~30 times/month
with sub-10-second execution — effectively **free**.
