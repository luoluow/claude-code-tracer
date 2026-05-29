#!/usr/bin/env bash
DATE=$(date +"%Y%m%d%H%M")

cd "$(dirname "$0")"

./forwarder/start.sh > temp/forwarder_$DATE.log 2>&1 &
