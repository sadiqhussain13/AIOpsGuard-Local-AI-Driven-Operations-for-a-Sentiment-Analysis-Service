# AIOpsGuard Architecture

## Component Diagram

```mermaid
C4Context
    title AIOpsGuard – System Context

    Person(developer, "Developer", "Runs and monitors the system locally")
    Person(loadtester, "Load Tester", "Generates realistic traffic")

    System(aiopsguard, "AIOpsGuard", "Local AI-driven operations platform")

    Rel(developer, aiopsguard, "Deploys, monitors, and remediates")
    Rel(loadtester, aiopsguard, "Sends HTTP requests")
```

## Detailed Architecture

```mermaid
graph LR
    subgraph "Developer Workstation"
        subgraph "Minikube Cluster"
            subgraph "Application Pods"
                SA["sentiment-app\n(Flask + Gunicorn)\nPort 5000"]
                FI["fault_injector.py\nFAILURE_RATE env"]
                SA --- FI
            end

            subgraph "ML Pods"
                AD["anomaly-detector\n(IsolationForest)\nPort 8080"]
                MLF["mlflow-server\nPort 5001"]
            end

            subgraph "Observability Pods"
                PR["prometheus\nPort 9090"]
                LK["loki\nPort 3100"]
                GR["grafana\nPort 3000"]
                FB["fluent-bit\n(DaemonSet)"]
            end

            subgraph "AI Agent Pod"
                AG["aiops-agent\n(LangChain)"]
            end
        end

        subgraph "Host"
            OL["ollama\nPort 11434\nmistral model"]
            DVC["DVC\ndata version control"]
            AN["Ansible\ndeploy.yml"]
        end
    end

    SA -->|scrape /metrics| PR
    AD -->|scrape /metrics| PR
    FB -->|push logs| LK
    PR --> GR
    LK --> GR
    SA -->|LLM inference| OL
    AG -->|read logs| LK
    AG -->|read metrics| PR
    AG -->|POST /predict| AD
    AG -->|LLM reasoning| OL
    AG -->|kubectl commands| SA
    DVC -->|train| AD
    AD -->|log model| MLF
    AN -->|deploy| SA
```

## Data Flow

### 1. Request Flow

```
Client → [NodePort 30080] → sentiment-app:5000/analyze
         → fault_injector (probabilistic 500)
         → _classify_sentiment()
         → Ollama API (mistral)
         ← "positive|negative|neutral"
         → Prometheus metrics update
         ← JSON response
```

### 2. Anomaly Detection Flow

```
Prometheus scrapes metrics every 15s
↓
AIOps Agent (runs every 60s)
  ├── QueryLoki({app="sentiment-app"}) → recent error logs
  ├── QueryPrometheus(rate(request_error_total[5m])) → error rate
  └── CallAnomalyDetector([rt_ms, cpu%, mem%, err_rate, req_count])
      ↓
  IsolationForest.predict() → anomaly: true/false
      ↓
  Ollama LLM → root-cause analysis + remediation plan
      ↓
  bash script (kubectl scale / kubectl delete pod)
      ↓
  if "apply" in output → execute script
      ↓
  log to /var/log/aiopsguard/agent.log
```

### 3. MLOps Flow

```
data/logs.csv (synthetic)
  ↓ DVC pipeline
anomaly_detector/train_anomaly_model.py
  ↓ StandardScaler + IsolationForest
model/anomaly_model.pkl (DVC-tracked artifact)
  ↓ MLflow logging
mlflow-server:5001 (metrics + model registry)
  ↓ Docker volume mount
anomaly-detector pod (model loaded at startup)
```

## Port Reference

| Service | Internal Port | NodePort |
|---------|-------------|----------|
| sentiment-app | 5000 | 30080 |
| anomaly-detector | 8080 | — (ClusterIP) |
| mlflow | 5001 | 30501 |
| prometheus | 9090 | 30090 |
| loki | 3100 | — (ClusterIP) |
| grafana | 3000 | 30300 |
| ollama | 11434 | — (host) |

## Security Architecture

```
┌──────────────────────────────────────────┐
│  PodSecurityContext                       │
│  runAsNonRoot: true                       │
│  runAsUser: 1001                          │
│  seccompProfile: RuntimeDefault           │
│                                           │
│  Container SecurityContext                │
│  allowPrivilegeEscalation: false          │
│  readOnlyRootFilesystem: true             │
│  capabilities.drop: [ALL]                 │
└──────────────────────────────────────────┘

Secrets:
  - K8s Secret: aiopsguard-secrets
    - MLFLOW_TRACKING_URI
  - Never committed to git (use .gitignore)

Network:
  - Services use ClusterIP internally
  - Only selected ports exposed via NodePort
  - No external internet access required at runtime
```
