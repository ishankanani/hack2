# DACHSER Live Transit Planner — Backend (v2)

FastAPI service on the **real DACHSER dataset**. Engines: dynamic legal-restriction,
ETA, cost, fuel, risk, savings, historical analytics, shipment lifecycle. Live
providers with honest status. Managers add temporary hub delays; everything is audited.

## Run
```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export DATA_DIR=/path/to/the/DACHSER/csvs             # REQUIRED
export STORE_DIR=./store                              # optional (shipment persistence)
uvicorn app.main:app --port 8000                      # docs at /docs
pytest -q                                             # 10 tests green
```
> Python 3.11–3.13. On 3.14 first run `pip install --upgrade pydantic`.

## Endpoints
`/api/network` · `/api/hubs` · `/api/providers` · `/api/holidays` ·
`/api/analytics/relations` (historical lane KPIs) · `/api/disruptions` ·
`/api/weather?node=` · `POST /api/route` (plan+compare+cost+risk+savings+geometry) ·
`GET/POST /api/shipments` · `/api/shipments/{id}` · `POST /api/shipments/{id}/schedule?option=&force=` ·
`POST /api/shipments/{id}/status` · `/api/savings` · `/api/high-value` · `/api/dashboard` ·
`POST/DELETE /api/hubs/{id}/delay` · `/api/audit`

## Modules
`data.py` network+holidays+transfer proxy · `restrictions.py` dynamic ban engine ·
`analytics.py` historical KPIs + disruptions · `cost.py` cost+fuel+cost/kg ·
`risk.py` risk scoring · `eta.py` journey composition · `shipments.py` lifecycle+persistence ·
`providers.py` weather/traffic/routing/holidays · `main.py` API.

## Live providers — honest status
Weather (Open-Meteo), routing geometry (OSRM) and the holiday calendar are keyless-LIVE
(need internet). **Traffic/incidents are NOT_CONFIGURED** until `TOMTOM_API_KEY` /
`HERE_API_KEY` is set. Nothing shows LIVE unless a real fetch succeeded (offline → ERROR).
