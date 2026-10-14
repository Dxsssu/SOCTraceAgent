#!/usr/bin/env bash
set -euo pipefail
agent="$1"
mode="${2:-full}"
if [[ "$agent" != react && "$agent" != expel && "$agent" != qwen-code ]]; then
  echo 'Expected react, expel or qwen-code' >&2; exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."
args=(--seed 42)
case "$mode" in
  full) suffix=full589-steps15; workers="${WORKERS:-3}" ;;
  smoke1) suffix=smoke1-steps15; workers=1; args+=(--sample-size 1) ;;
  smoke10) suffix=smoke10-steps15; workers="${WORKERS:-3}"; args+=(--sample-size 10) ;;
  *) echo "Usage: scripts/run_${agent}.sh [full|smoke1|smoke10]" >&2; exit 2 ;;
esac
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
exec .venv/bin/python -m src.benchmarks.external_agents.resume \
  --agent "$agent" --provider fancybag --model deepseek-v4.1-flash \
  --workers "$workers" --max-steps 15 "${args[@]}" \
  --output "results/$agent/fancybag-deepseek-v4.1-flash/single-step-v2/$suffix"
