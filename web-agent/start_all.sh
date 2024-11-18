#!/bin/sh

# Activate the virtual environment
source /usr/src/venv/bin/activate

# Run worker.py with indices in parallel
python3 worker.py --index 1 &
python3 worker.py --index 2 &

# Wait for all processes to finish
wait