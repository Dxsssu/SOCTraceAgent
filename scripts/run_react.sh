#!/usr/bin/env bash
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/_run_paper_agent.sh" react "$@"
