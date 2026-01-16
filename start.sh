#!/bin/bash
set -e
echo "Starting Gunicorn (SQLite Optimized)..."
gunicorn server.server:app \
    --workers 1 \
    --threads 10 \
    --timeout 120 \
    --bind 0.0.0.0:$PORT &

SERVER_PID=$!

sleep 5
echo "Starting Bot..."
python bot/bot1.py