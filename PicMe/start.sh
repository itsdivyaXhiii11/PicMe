#!/bin/bash
# Start script for Oracle Cloud
# Run this after setting up .env

cd "$(dirname "$0")"

# Load environment
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

# Activate virtual environment
source venv/bin/activate

# Number of workers (adjust based on your instance)
# For 2 vCPU + 4GB RAM: 2 workers
# For 4 vCPU + 8GB RAM: 4 workers
WORKERS=${WORKERS:-2}

# Port (Oracle Cloud might require specific ports)
PORT=${PORT:-8000}

echo "Starting PicMe with $WORKERS workers on port $PORT..."

# Use gunicorn with uvicorn workers
exec gunicorn main:app \
    -w $WORKERS \
    -k uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:$PORT \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    --enable-stdio-inheritance