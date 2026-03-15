"""AIOps LangChain Zero-Shot-React agent for automated incident remediation.

The agent:
1. Reads recent logs from Loki.
2. Queries Prometheus for metric trends.
3. Calls the anomaly-detector endpoint.
4. Uses Ollama (local LLM) to reason about root cause.
5. Generates a bash remediation script containing kubectl commands.
6. Outputs the script and applies it only when LLM output contains "apply".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from langchain.agents import AgentType, Tool, initialize_agent
from langchain.prompts import PromptTemplate

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "mistral")
LOKI_URL: str = os.environ.get("LOKI_URL", "http://localhost:3100")
PROMETHEUS_URL: str = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ANOMALY_URL: str = os.environ.get(
    "ANOMALY_DETECTOR_URL", "http://anomaly-detector:8080"
)
LOG_FILE: str = os.environ.get("AGENT_LOG_FILE", "/var/log/aiopsguard/agent.log")
NAMESPACE: str = os.environ.get("K8S_NAMESPACE", "default")

# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------


def _query_loki(query: str, limit: int = 50) -> str:
    """Fetch recent log lines from Loki.

    Args:
        query: LogQL query string.
        limit: Maximum number of log lines to return.

    Returns:
        Concatenated log lines as a single string.
    """
    try:
        params = {
            "query": query,
            "limit": limit,
            "start": str(int((time.time() - 300) * 1e9)),  # last 5 min
        }
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        lines: list[str] = []
        for stream in data.get("data", {}).get("result", []):
            for _, log_line in stream.get("values", []):
                lines.append(log_line)
        return "\n".join(lines[-limit:]) if lines else "No logs found"
    except Exception as exc:
        return f"Loki query error: {exc}"


def _query_prometheus(promql: str) -> str:
    """Execute a PromQL instant query and return the result as a string.

    Args:
        promql: PromQL expression.

    Returns:
        JSON-encoded result string.
    """
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": promql},
            timeout=10,
        )
        resp.raise_for_status()
        return json.dumps(resp.json().get("data", {}).get("result", []), indent=2)
    except Exception as exc:
        return f"Prometheus query error: {exc}"


def _call_anomaly_detector(metrics_json: str) -> str:
    """Call the anomaly-detector /predict endpoint.

    Args:
        metrics_json: JSON string – list of 5 numeric values matching the
            feature order: [response_time_ms, cpu_usage_pct,
            memory_usage_pct, error_rate, request_count].

    Returns:
        JSON-encoded anomaly prediction string.
    """
    try:
        payload = json.loads(metrics_json)
        resp = requests.post(
            f"{ANOMALY_URL}/predict", json=payload, timeout=10
        )
        resp.raise_for_status()
        return json.dumps(resp.json())
    except Exception as exc:
        return f"Anomaly detector error: {exc}"


# ---------------------------------------------------------------------------
# LangChain tools
# ---------------------------------------------------------------------------
tools: list[Tool] = [
    Tool(
        name="QueryLoki",
        func=lambda q: _query_loki(q),
        description=(
            "Fetch recent application logs from Loki. "
            "Input: a LogQL query string, e.g. '{app=\"sentiment-app\"}'."
        ),
    ),
    Tool(
        name="QueryPrometheus",
        func=lambda q: _query_prometheus(q),
        description=(
            "Run a PromQL query against Prometheus. "
            "Input: a PromQL expression string, "
            "e.g. 'rate(request_error_total[5m])'."
        ),
    ),
    Tool(
        name="CallAnomalyDetector",
        func=lambda m: _call_anomaly_detector(m),
        description=(
            "Call the anomaly-detector /predict endpoint. "
            "Input: a JSON array of 5 numbers: "
            "[response_time_ms, cpu_usage_pct, memory_usage_pct, error_rate, request_count]."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = PromptTemplate(
    input_variables=["input", "agent_scratchpad"],
    template="""You are an expert AIOps engineer monitoring the AIOpsGuard sentiment-analysis service.

Use the available tools to:
1. Read recent logs from Loki to identify errors.
2. Query Prometheus for error rates and latency trends.
3. Call the anomaly detector with current metrics.
4. Reason about the root cause.
5. Generate a remediation plan as a bash script using kubectl commands.

When you are confident about the remediation, include the phrase "apply" in your final answer
so the scheduler knows to execute the generated bash script.

Namespace: {namespace}

{input}

{agent_scratchpad}
""".replace(
        "{namespace}", NAMESPACE
    ),
)


def _get_llm() -> Any:
    """Initialise the Ollama LLM."""
    from langchain_ollama import OllamaLLM  # type: ignore[import]

    return OllamaLLM(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=0)


def run_agent() -> str:
    """Run the AIOps agent and return the generated remediation text.

    Returns:
        The agent's final output string.
    """
    llm = _get_llm()
    agent = initialize_agent(
        tools=tools,
        llm=llm,
        agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=True,
        handle_parsing_errors=True,
        max_iterations=8,
    )

    task = (
        "Analyse the current health of the sentiment-analysis service. "
        "Check logs, metrics, and the anomaly detector. "
        "If there is an incident, generate a kubectl-based bash remediation script. "
        "If remediation is needed, include the word 'apply' in your conclusion."
    )

    logger.info("Starting AIOps agent …")
    result: str = agent.run(task)
    logger.info("Agent result: %s", result)
    return result


def _write_log(content: str) -> None:
    """Append the agent result to the log file.

    Args:
        content: Text content to append.
    """
    log_path = Path(LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    with open(log_path, "a") as fh:
        fh.write(f"\n{'=' * 60}\n{timestamp}\n{content}\n")


def _extract_bash_script(text: str) -> str | None:
    """Extract a bash script block from the agent output.

    Args:
        text: Raw agent output text.

    Returns:
        The bash script string, or None if no script block found.
    """
    import re

    pattern = re.compile(r"```(?:bash|sh)\s*(.*?)```", re.DOTALL | re.IGNORECASE)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return None


def _apply_script(script: str) -> None:
    """Execute the generated bash script via subprocess.

    Args:
        script: Bash script content to execute.
    """
    script_path = Path("/tmp/aiopsguard_remediation.sh")  # noqa: S108
    script_path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{script}")
    script_path.chmod(0o700)
    logger.info("Executing remediation script …")
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/env", "bash", str(script_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    logger.info("Script stdout: %s", result.stdout)
    if result.returncode != 0:
        logger.error("Script stderr: %s", result.stderr)


def main() -> None:
    """CLI entry point."""
    output = run_agent()
    _write_log(output)

    if "apply" in output.lower():
        script = _extract_bash_script(output)
        if script:
            logger.info("Applying remediation script …")
            _apply_script(script)
        else:
            logger.warning("'apply' found but no bash script block detected")
    else:
        logger.info("No remediation needed at this time")

    print(output)  # noqa: T201 – written to stdout for scheduler capture
    sys.exit(0)


if __name__ == "__main__":
    main()
