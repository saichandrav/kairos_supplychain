# SAP BTP Cloud Foundry Deployment & Run Guide

Kairos runs on SAP BTP Cloud Foundry with SAP HANA Cloud backend, or locally with SQLite for offline presentation resilience.

---

## 1. Local Run (FastAPI + SQLite Fallback)

To run Kairos locally on your workstation:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Seed database (if not already seeded)
python -m ingest.load_hana --backend sqlite --seed

# 3. Launch FastAPI server
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Interactive API documentation (Swagger UI): `http://127.0.0.1:8000/docs`

---

## 1a. Live Disruption Sensing (real weather + news)

The Route Optimizer's "Check live conditions" button pulls real current data for every mapped
chokepoint and Indian port (`GET /disruption/live`):

- **Marine weather** (wave height, wind speed) via **Open-Meteo** - no signup, no API key, nothing
  to configure. This half always works.
- **Maritime/trade news** via **NewsAPI.org** - needs a free API key:
  1. Register at <https://newsapi.org/register> (email only, no card).
  2. Copy your key from the dashboard.
  3. In `.env`, set `NEWSAPI_KEY=<your key>` (see `config/env.sample`).
  4. Free "Developer" plan: 100 requests/day, which is plenty for demo use.

  Without `NEWSAPI_KEY`, `/disruption/live` still returns live weather; `news_enabled` is `false`
  and the UI shows a note that news signals are off.

---

## 2. SAP BTP Cloud Foundry Deployment

### Step 1: Log in to BTP Cloud Foundry CLI
```bash
cf login -a https://api.cf.eu10-004.hana.ondemand.com --sso
# Select your Hackfest organization and space
```

### Step 2: Create User-Provided Service for SAP HANA Cloud
```bash
cf create-user-provided-service ff-hana -p '{
  "host": "<your-hana-host>.eu10.hanacloud.ondemand.com",
  "port": 443,
  "user": "<your-hana-user>",
  "password": "<your-hana-password>",
  "schema": "<your-hana-user>"
}'
```

### Step 3: Set Application Environment Variables
```bash
cf set-env kairos-api DB_BACKEND hana
cf set-env kairos-api APP_API_KEY "kairos-secret-key"
```

### Step 4: Deploy with Manifest
```bash
cf push
```

### Step 5: Test from Browser or Curl
```bash
# Public health endpoint
curl https://<your-cf-app-route>.cfapps.eu10-004.hana.ondemand.com/health

# Authenticated watchlist endpoint
curl -H "X-API-Key: kairos-secret-key" \
     https://<your-cf-app-route>.cfapps.eu10-004.hana.ondemand.com/watchlist
```
