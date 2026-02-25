#!/bin/bash
set -e

# Railway sets PORT=8080; default to 8000 locally
PORT="${PORT:-8000}"

# Point DB at persistent volume when deployed
export HOUSEHOLD_DB_PATH="${HOUSEHOLD_DB_PATH:-/data/household.db}"

exec uvicorn deploy.server:app --host 0.0.0.0 --port "$PORT"
