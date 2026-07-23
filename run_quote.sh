#!/bin/bash
cd /home/hermes/chain-finder || exit 1
/home/hermes/.hermes/hermes-agent/venv/bin/python3 /home/hermes/chain-finder/quote_cron.py
