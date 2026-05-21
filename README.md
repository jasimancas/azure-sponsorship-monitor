# Azure Subscription Monitor

Panel de monitorización de consumo para suscripciones Azure de tipo **Partner Benefits / Sponsorship** (MS-AZR-0036P).

Descubre automáticamente todas las suscripciones accesibles al Service Principal y filtra por Offer ID — sin listas hardcodeadas.

## Inicio rápido (Docker)

### 1. Instalar Docker Desktop

```bash
brew install --cask docker
# Abre Docker Desktop y espera a que arranque
```

### 2. Clonar y configurar

```bash
git clone https://github.com/TU_USUARIO/azure-subscription-monitor.git
cd azure-subscription-monitor

# Crear tu .env con las credenciales (nunca se sube a Git)
cp .env.example .env
# Edita .env con tu AZURE_TENANT_ID, AZURE_CLIENT_ID y AZURE_CLIENT_SECRET
```

### 3. (Opcional) Nombres y budgets custom

```bash
cp subscriptions.local.json.example subscriptions.local.json
# Edita con los IDs de tus suscripciones y los nombres que quieras mostrar
# Este archivo también está en .gitignore
```

### 4. Arrancar

```bash
docker compose up
```

Abre http://localhost:8000 — el overview carga automáticamente.

## Permisos necesarios del Service Principal

El SP necesita el rol **Reader** en cada suscripción que quieras monitorizar,
o en el Management Group que las contenga:

```bash
az role assignment create \
  --assignee TU_CLIENT_ID \
  --role "Reader" \
  --scope /subscriptions/SUBSCRIPTION_ID
```

## Variables de entorno

| Variable | Descripción | Obligatoria |
|----------|-------------|-------------|
| `AZURE_TENANT_ID` | ID del tenant | ✅ |
| `AZURE_CLIENT_ID` | Client ID del SP | ✅ |
| `AZURE_CLIENT_SECRET` | Secret del SP | ✅ |
| `AZURE_OFFER_IDS` | Offer IDs separados por coma | ❌ (default: `MS-AZR-0036P`) |
| `AZURE_CURRENCY` | Moneda para RateCard | ❌ (default: `EUR`) |
| `FLASK_SECRET_KEY` | Clave secreta Flask | ✅ en producción |
| `MAX_WORKERS` | Hilos paralelos | ❌ (default: `10`) |
| `REFRESH_HOURS` | Frecuencia de refresco | ❌ (default: `3`) |

## Estructura del repo

```
.
├── app.py                         # App Flask principal
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── templates/
│   ├── index.html                 # Vista detalle por suscripción
│   └── overview.html              # Panel consolidado
├── .env.example                   # Plantilla de variables (sin secretos)
├── subscriptions.local.json.example  # Plantilla de overrides locales
└── .gitignore
```

## Seguridad

- Ningún secreto en el repo — todo vía variables de entorno
- `.env` y `subscriptions.local.json` están en `.gitignore`
- Las suscripciones se descubren automáticamente por Offer ID, sin IDs en el código
