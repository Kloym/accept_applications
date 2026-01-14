#!/bin/bash
set -e
echo "Starting Gunicorn..."
exec gunicorn server.server:app \
    --workers 4 \
    --threads 2 \
    --timeout 60 \
    --bind 0.0.0.0:$PORT &


sleep 5
echo "Starting Bot..."
python bot/bot1.py