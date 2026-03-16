const healthGrid = document.getElementById("health-grid");
const metricCards = document.getElementById("metric-cards");
const sparkline = document.getElementById("sparkline");
const sentimentResult = document.getElementById("sentiment-result");
const anomalyResult = document.getElementById("anomaly-result");
const incidentReasoning = document.getElementById("incident-reasoning");
const incidentScript = document.getElementById("incident-script");
const incidentLogs = document.getElementById("incident-logs");
const failureSlider = document.getElementById("failure-slider");
const failureValue = document.getElementById("failure-value");
const failureStatus = document.getElementById("failure-status");

const latencyTrend = [];

function fmt(value, digits = 3) {
  if (value === null || value === undefined) return "n/a";
  if (typeof value !== "number" || Number.isNaN(value)) return String(value);
  return value.toFixed(digits);
}

function drawSparkline(values) {
  const w = 320;
  const h = 80;
  if (!values.length) {
    sparkline.innerHTML = "";
    return;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = Math.max(max - min, 0.001);
  const step = w / Math.max(values.length - 1, 1);
  const pts = values
    .map((v, i) => {
      const x = i * step;
      const y = h - ((v - min) / spread) * (h - 8) - 4;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  sparkline.innerHTML = `
    <polyline fill="none" stroke="#63d5ff" stroke-width="2.2" points="${pts}" />
  `;
}

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error || `Request failed (${resp.status})`);
  }
  return data;
}

async function refreshServices() {
  try {
    const data = await fetchJson("/ui/api/services");
    const rows = Object.entries(data)
      .filter(([name]) => name !== "failure_rate")
      .map(([name, info]) => {
        const cls = info.ok ? "up" : "down";
        return `
          <div class="health-item ${cls}">
            <strong>${name}</strong><br>
            status: ${info.ok ? "UP" : "DOWN"} (${info.status_code})<br>
            latency: ${fmt(info.latency_ms, 1)} ms
          </div>
        `;
      })
      .join("");
    healthGrid.innerHTML = rows;
    if (typeof data.failure_rate === "number") {
      failureSlider.value = String(data.failure_rate);
      failureValue.textContent = data.failure_rate.toFixed(2);
    }
  } catch (err) {
    healthGrid.innerHTML = `<div class="health-item down">${err.message}</div>`;
  }
}

async function refreshMetrics() {
  try {
    const m = await fetchJson("/ui/api/metrics");
    const cards = [
      ["Req/s", fmt(m.request_rps, 3)],
      ["Err/s", fmt(m.error_rps, 3)],
      ["P95 Latency (s)", fmt(m.p95_latency_sec, 3)],
      ["Anomaly Req/s", fmt(m.anomaly_predict_rps, 3)],
      ["Sentiment Up", fmt(m.app_up, 0)],
      ["Anomaly Up", fmt(m.anomaly_up, 0)],
    ];
    metricCards.innerHTML = cards
      .map(
        ([label, value]) =>
          `<div class="metric-card"><div class="label">${label}</div><div class="value">${value}</div></div>`
      )
      .join("");

    if (typeof m.p95_latency_sec === "number") {
      latencyTrend.push(m.p95_latency_sec);
      while (latencyTrend.length > 40) latencyTrend.shift();
      drawSparkline(latencyTrend);
    }
  } catch (err) {
    metricCards.innerHTML = `<div class="metric-card">${err.message}</div>`;
  }
}

document.getElementById("sentiment-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = document.getElementById("sentiment-text").value.trim();
  if (!text) return;

  const t0 = performance.now();
  try {
    const data = await fetchJson("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const elapsed = performance.now() - t0;
    sentimentResult.textContent = `sentiment=${data.sentiment} latency=${elapsed.toFixed(1)}ms`;
  } catch (err) {
    sentimentResult.textContent = err.message;
  }
});

document.getElementById("anomaly-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = {
    response_time_ms: Number(form.get("response_time_ms")),
    cpu_usage_pct: Number(form.get("cpu_usage_pct")),
    memory_usage_pct: Number(form.get("memory_usage_pct")),
    error_rate: Number(form.get("error_rate")),
    request_count: Number(form.get("request_count")),
  };

  try {
    const out = await fetchJson("/ui/api/anomaly", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    anomalyResult.textContent = `anomaly=${out.result.anomaly} score=${fmt(out.result.score, 4)} latency=${fmt(out.latency_ms, 1)}ms`;
  } catch (err) {
    anomalyResult.textContent = err.message;
  }
});

document.getElementById("incident-btn").addEventListener("click", async () => {
  incidentReasoning.textContent = "Loading incident analysis...";
  incidentScript.textContent = "";
  incidentLogs.textContent = "";
  try {
    const out = await fetchJson("/ui/api/incident");
    incidentReasoning.textContent = `[${out.source}] ${out.root_cause}\n\n${out.reasoning}`;
    incidentScript.textContent = out.remediation_script || "No script returned";
    const logs = out.snapshot?.logs || [];
    incidentLogs.textContent = logs.join("\n");
  } catch (err) {
    incidentReasoning.textContent = err.message;
  }
});

failureSlider.addEventListener("input", () => {
  failureValue.textContent = Number(failureSlider.value).toFixed(2);
});

document.getElementById("failure-apply").addEventListener("click", async () => {
  try {
    const out = await fetchJson("/ui/api/failure-rate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ failure_rate: Number(failureSlider.value) }),
    });
    failureStatus.textContent = `Updated failure rate to ${fmt(out.failure_rate, 2)}`;
  } catch (err) {
    failureStatus.textContent = err.message;
  }
});

async function refreshAll() {
  await Promise.all([refreshServices(), refreshMetrics()]);
}

refreshAll();
setInterval(refreshAll, 8000);
