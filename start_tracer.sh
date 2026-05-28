#!/usr/bin/env bash
DATE=$(date +"%Y%m%d%H%M")

cd "$(dirname "$0")"
nohup ./tracer/start.sh > temp/tracer_$DATE.log 2>&1 &

