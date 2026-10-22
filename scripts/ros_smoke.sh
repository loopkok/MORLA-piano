#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${ROS_DOMAIN_ID:-}" != "73" ]]; then
  echo 'Run in a dedicated software test domain: export ROS_DOMAIN_ID=73' >&2
  exit 1
fi
exec python3 scripts/ros_smoke.py
