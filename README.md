# Azure Sponsorship Monitor

A web dashboard for monitoring Azure consumption across multiple **Partner Benefits / Sponsorship** subscriptions (offer `Sponsored` / MS-AZR-0036P).

Automatically discovers all subscriptions accessible to the Service Principal and filters by Offer ID — no hardcoded lists, no secrets in the repo.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Docker](https://img.shields.io/badge/docker-ready-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Auto-discovery** — finds all sponsored subscriptions via the Azure API, no JSON config needed
- **Consolidated overview** — all subscriptions in one panel, sorted by consumption
- **Auto-refresh** — fetches data every 3 hours in the background, page updates automatically
- **Per-subscription detail** — daily cost chart, top services, billing metadata
- **Entra ID auth** — Easy Auth integration for team access (Azure deployment)
- **BRSDT decoding** — detects and labels Azure OpenAI / Copilot usage

## Quick start (Docker)

### 1. Install Docker Desktop

```bash
brew install --cask docker
```

### 2. Clone and configure

```bash
git clone https://github.com/jasimancas/azure-sponsorship-monitor.git
cd azure-sponsorship-monitor

cp .env.example .env
# Edit .env with your AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET
```

### 3. Start

```bash
docker compose up
```

Open http://localhost:8000 — the overview loads automatically on first run.

## Service Principal permissions

The SP needs the **Reader** role on each subscription to monitor, or on the Management Group containing them:

```bash
az role assignment create \
  --assignee YOUR_CLIENT_ID \
  --role "Reader" \
  --scope /subscriptions/SUBSCRIPTION_ID
```

## Environment variables

| Variable | Description | Required |
|----------|-------------|----------|
| `AZURE_TENANT_ID` | Azure tenant ID | ✅ |
| `AZURE_CLIENT_ID` | Service Principal client ID | ✅ |
| `AZURE_CLIENT_SECRET` | Service Principal secret | ✅ |
| `AZURE_OFFER_IDS` | Offer IDs to monitor (comma-separated) | ❌ default: `Sponsored` |
| `AZURE_CURRENCY` | Currency for RateCard | ❌ default: `EUR` |
| `AZURE_LOCALE` | Locale for RateCard | ❌ default: `es-ES` |
| `AZURE_REGION_INFO` | Region for RateCard | ❌ default: `ES` |
| `FLASK_SECRET_KEY` | Flask secret key | ✅ in production |
| `MAX_WORKERS` | Parallel fetch threads | ❌ default: `8` |
| `REFRESH_HOURS` | Background refresh interval | ❌ default: `3` |

## Repository structure

```
.
├── app.py                              # Flask application
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── templates/
│   ├── index.html                      # Per-subscription detail view
│   └── overview.html                   # Consolidated dashboard
├── .github/
│   └── workflows/
│       └── docker-build.yml            # Builds and pushes to GHCR on push to main
├── .env.example                        # Environment variable template
└── subscriptions.local.json.example    # Optional local name/budget overrides
```

## Docker image

Every push to `main` builds a Docker image and publishes it to GitHub Container Registry:

```bash
docker pull ghcr.io/jasimancas/azure-sponsorship-monitor:latest
```

## Security

- No secrets in the repository — all credentials via environment variables
- `.env` is gitignored
- Subscriptions are discovered dynamically by Offer ID — no subscription IDs in code
- Managed Identity support for Azure App Service deployment (no SP secret needed in production)

## License

MIT