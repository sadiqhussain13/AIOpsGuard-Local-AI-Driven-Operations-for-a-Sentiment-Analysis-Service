#!/usr/bin/env bash
# run_agent.sh – Run the AIOpsGuard AI agent on a schedule.
#
# Usage:
#   bash run_agent.sh
#
# Or install as a cron job:
#   * * * * * /app/run_agent.sh >> /var/log/aiopsguard/cron.log 2>&1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${AGENT_LOG_DIR:-/var/log/aiopsguard}"
LOG_FILE="${LOG_DIR}/agent.log"
PYTHON="${PYTHON:-python3}"

mkdir -p "${LOG_DIR}"

echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [run_agent] Starting agent run …" | tee -a "${LOG_FILE}"

OUTPUT="$("${PYTHON}" "${SCRIPT_DIR}/agent.py" 2>&1)" || true

echo "${OUTPUT}" >> "${LOG_FILE}"

echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") [run_agent] Agent run complete." | tee -a "${LOG_FILE}"
