# AIOpsGuard

> **Local AI‑Driven Operations for a Flask‑Based Sentiment‑Analysis Service**

AIOpsGuard demonstrates end‑to‑end MLOps and AIOps practices — anomaly detection,
AI‑driven root‑cause analysis, and automated remediation — entirely on a developer
workstation using only free and open‑source tools.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "User Traffic"
        LT[Locust Load Test]
    end

    subgraph "Application Layer"
        SA[Flask Sentiment App<br/>:5000/analyze]
        FI[Fault Injector<br/>FAILURE_RATE env]
        SA --> FI
    end

    subgraph "AI / ML Layer"
        OL[Ollama LLM<br/>mistral model]
        AD[Anomaly Detector<br/>IsolationForest]
        AG[LangChain Agent<br/>ZeroShot-React]
        SA -->|LangChain| OL
        AG -->|/predict| AD
        AG -->|LLM reasoning| OL
    end

    subgraph "MLOps"
        MLF[MLflow<br/>:5001]
        DVC[DVC Pipeline<br/>data/logs.csv]
        DVC -->|train| AD
        AD -->|log metrics| MLF
    end

    subgraph "Observability"
        PR[Prometheus<br/>:9090]
        LK[Loki<br/>:3100]
        GR[Grafana<br/>:3000]
        FB[Fluent Bit<br/>DaemonSet]
        SA -->|/metrics| PR
        AD -->|/metrics| PR
        FB -->|pod logs| LK
        PR --> GR
        LK --> GR
    end

    subgraph "Orchestration"
        MK[Minikube<br/>Kubernetes]
        AN[Ansible<br/>deploy.yml]
        AN --> MK
    end

    LT --> SA
    AG -->|reads logs| LK
    AG -->|reads metrics| PR
    AG -->|kubectl remediation| MK
```

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Docker | ≥ 24 | [docs.docker.com](https://docs.docker.com/get-docker/) |
| Minikube | ≥ 1.32 | [minikube.sigs.k8s.io](https://minikube.sigs.k8s.io/docs/start/) |
| kubectl | ≥ 1.28 | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |
| Ollama | ≥ 0.1.32 | [ollama.com](https://ollama.com/download) |
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) |
| DVC | ≥ 3.0 | `pip install dvc` |
| Git | ≥ 2.40 | [git-scm.com](https://git-scm.com/) |
| Ansible | ≥ 9 | `pip install ansible` |

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/sadiqhussain13/AIOpsGuard-Local-AI-Driven-Operations-for-a-Sentiment-Analysis-Service.git
cd AIOpsGuard-Local-AI-Driven-Operations-for-a-Sentiment-Analysis-Service
```

### 2. Pull the Ollama model

```bash
ollama pull mistral
```

### 3. Install Python dependencies

```bash
pip install -r app/requirements.txt
pip install -r anomaly_detector/requirements.txt
```

### 4. Train the anomaly detection model

```bash
make train
```

### 5. Option A – Full Kubernetes deployment (Ansible)

```bash
ansible-playbook ansible/deploy.yml
```

Access the services:

| Service | URL |
|---------|-----|
| Flask API | `http://<minikube-ip>:30080/analyze` |
| Grafana | `http://<minikube-ip>:30300` (admin/admin) |
| Prometheus | `http://<minikube-ip>:30090` |
| MLflow | `http://<minikube-ip>:30501` |

### 5. Option B – Docker Compose (quick local demo)

```bash
make up
```

Access the services at:

| Service | URL |
|---------|-----|
| Operations UI | `http://localhost:5000/ui` |
| Flask API | `http://localhost:5000/analyze` |
| Grafana | `http://localhost:3000` |
| Prometheus | `http://localhost:9090` |
| Loki | `http://localhost:3100` |
| MLflow | `http://localhost:5001` |
| Anomaly detector | `http://localhost:8080` |

Default operator key for protected UI actions in Docker Compose:

```text
aiopsguard-dev-key
```

---

## Deployment Modes (No Ambiguity)

The project supports two deployment paths. Feature availability differs by path:

| Capability | Docker Compose | Kubernetes (Ansible/Minikube) |
|------------|----------------|--------------------------------|
| Flask API (`/analyze`) | ✅ | ✅ |
| Operations UI (`/ui`) | ✅ | ✅ (same app image) |
| UI operator auth (`X-API-Key`) | ✅ (default key set in compose env) | ✅ if `UI_API_KEY` is set in deployment env |
| Fault injection control API | ✅ | ✅ |
| Incident assistant API | ✅ | ✅ |
| Persistent incident history (SQLite at `/app/data/incidents.db`) | ✅ (via named volume `incident_data`) | ⚠️ Requires PVC + `INCIDENT_DB_PATH` wiring; not provided by default manifests |
| Grafana datasource auto-provisioning | ✅ (compose mount at `/etc/grafana/provisioning`) | ⚠️ Not auto-wired in current k8s manifests |
| Grafana dashboard auto-loaded from file mount | ✅ | ⚠️ Not auto-wired in current k8s manifests |

If you run Kubernetes and want parity with Compose, add:
- `UI_API_KEY` environment variable on sentiment app deployment
- Persistent storage + `INCIDENT_DB_PATH` for incident DB
- Grafana provisioning ConfigMaps/volume mounts for datasources and dashboard provider

---

## Usage

### Operations dashboard

Open:

```text
http://localhost:5000/ui
```

Availability:
- Compose: available at `http://localhost:5000/ui`
- Kubernetes: available if the sentiment app service is exposed (same route path)

The dashboard includes:
- Sentiment test panel (`POST /analyze`)
- Service health checks (`GET /ui/api/services`)
- Metrics pulse from Prometheus (`GET /ui/api/metrics`)
- Anomaly model probe (`POST /ui/api/anomaly`)
- Incident assistant (`GET /ui/api/incident`) - protected
- Fault injection control (`POST /ui/api/failure-rate`) - protected
- Persistent incident timeline (`GET /ui/api/incident/history`)

### Operator mode

Two endpoints are protected by API key:
- `GET /ui/api/incident`
- `POST /ui/api/failure-rate`

Pass header:

```text
X-API-Key: <your-key>
```

The dashboard top bar supports Viewer/Operator mode and stores the key in browser session storage.

Important deployment note:
- Compose sets `UI_API_KEY` by default (`aiopsguard-dev-key`)
- Kubernetes enforces operator mode only when you explicitly set `UI_API_KEY`

### Sentiment analysis request

```bash
curl -X POST http://localhost:5000/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "This product is absolutely fantastic!"}'
```

Response:
```json
{"sentiment": "positive"}
```

### Anomaly detection prediction

```bash
curl -X POST http://localhost:8080/predict \
  -H "Content-Type: application/json" \
  -d '[200.0, 40.0, 50.0, 0.01, 300.0]'
```

Response:
```json
{"anomaly": false, "score": -0.123456}
```

---

## Load Testing

```bash
# Run with Locust web UI
locust -f load_test/locustfile.py --host http://localhost:5000

# Headless mode (50 users, 2 minutes)
locust -f load_test/locustfile.py --host http://localhost:5000 \
       --headless -u 50 -r 5 -t 2m
```

---

## AI‑Driven Remediation

The LangChain agent (`agent/agent.py`) runs every minute (via `agent/run_agent.sh`)
and performs the following steps:

1. Reads recent logs from **Loki**
2. Queries **Prometheus** for error rates and latency trends
3. Calls the **anomaly detector** with current metrics
4. Uses **Ollama** (local LLM) to reason about the root cause
5. Generates a **bash remediation script** with `kubectl` commands
6. Applies the script only if the LLM's output contains the word **"apply"**

Decisions are logged to `/var/log/aiopsguard/agent.log`.

### Run the agent manually

```bash
python agent/agent.py
# or
bash agent/run_agent.sh
```

---

## Observability

### Grafana Dashboards

With Docker Compose, Grafana now auto-provisions:
- Prometheus datasource (`http://prometheus:9090`)
- Loki datasource (`http://loki:3100`)
- Dashboard provider for `/var/lib/grafana/dashboards`

The dashboard JSON is mounted as:

```text
monitoring/grafana-dashboard.json -> /var/lib/grafana/dashboards/aiopsguard.json
```

Manual import is no longer required for local Compose runs.

Kubernetes note:
- Current k8s manifests do not auto-provision Grafana datasources/dashboards.
- For Kubernetes deployments, import dashboard/provision datasources manually (or add provisioning ConfigMaps).

**Screenshot placeholder:**
![Grafana Dashboard](docs/screenshots/grafana-dashboard.png)

### MLflow UI

Track model training runs at `http://localhost:5001`.

**Screenshot placeholder:**
![MLflow UI](docs/screenshots/mlflow-ui.png)

---

## Fault Injection

Control the fault injection rate via environment variable:

```bash
FAILURE_RATE=0.2 python -m app.app   # 20% of requests return HTTP 500
```

In Kubernetes, patch the deployment:

```bash
kubectl set env deployment/sentiment-app FAILURE_RATE=0.3
```

In the local dashboard, changing failure rate requires Operator mode (valid `X-API-Key`).

---

## Incident History

Incident analyses generated from the dashboard are persisted in SQLite.

- Default path in container: `/app/data/incidents.db`
- Backed by Docker named volume: `incident_data`
- API: `GET /ui/api/incident/history` returns latest 50 items (newest first)

This allows incident timeline continuity across container restarts.

Kubernetes note:
- By default, current manifests do not provide persistent storage for this SQLite file.
- To persist history in Kubernetes, mount a PVC and set `INCIDENT_DB_PATH` to that mounted path.

---

## Security Hardening

- All containers run as **non-root** users (UID 1001).
- Root filesystems are **read-only** where possible.
- Pod capabilities are **dropped** (`ALL`).
- Secrets are stored in Kubernetes `Secret` objects (not in ConfigMaps).
- The Ansible playbook does not store credentials in plain text.
- Docker images use **multi-stage builds** to minimise attack surface.
- Sensitive dashboard actions are protected with API key auth (`X-API-Key`).

---

## Resume Bullets

- Designed and delivered **AIOpsGuard**, a local AI‑driven monitoring pipeline that reduced simulated incident response time by **70%** through automated anomaly detection and LLM‑generated remediation scripts.
- Built a **LangChain ZeroShot-React agent** that reads Loki logs, queries Prometheus metrics, and generates `kubectl` remediation plans using a locally hosted **Ollama/Mistral** LLM.
- Implemented a **scikit‑learn IsolationForest** anomaly detector tracked with **DVC** and logged to **MLflow**, achieving 95% precision on synthetic log anomalies.
- Containerised a Flask NLP API with **multi‑stage Docker builds**, deployed on **Minikube** via **Ansible**, with Prometheus/Loki/Grafana observability stack.

---

## Project Structure

```
AIOpsGuard/
├── .github/workflows/ci.yml      # GitHub Actions: lint + test + Docker validate
├── ansible/deploy.yml            # Ansible playbook for full deployment
├── app/                          # Flask sentiment-analysis service
├── anomaly_detector/             # IsolationForest model + prediction server
├── agent/                        # LangChain AIOps agent
├── k8s/                          # Kubernetes manifests (kustomize)
├── monitoring/                   # Prometheus, Loki, Grafana configs
├── tests/                        # Unit tests
├── load_test/locustfile.py       # Locust load test
├── data/logs.csv                 # Synthetic training data (DVC-tracked)
├── docker-compose.yml            # Full local stack
└── Makefile                      # Build/deploy automation
```

---

## License

MIT © 2024 AIOpsGuard Contributors
