#!/bin/sh
# Pobere podatke in osveži statistiko. Za cron: vsaki 2 minuti.
cd "$(dirname "$0")" || exit 1
export TZ=Europe/Ljubljana
python3 collector.py && python3 builder.py
