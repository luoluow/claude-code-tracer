#!/usr/bin/env bash
DATE=$(date +"%Y%m%d%H%M")

cd "$(dirname "$0")"

export TRACER_LOG_DIR="${TRACER_LOG_DIR:-$PWD/temp/logs}"
mkdir -p "$TRACER_LOG_DIR"
./tracer/start.sh > "$TRACER_LOG_DIR/tracer_$DATE.log" 2>&1 &
