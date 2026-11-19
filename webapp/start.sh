#!/bin/bash
# Start Job Hunt, then open http://localhost:8000 in your browser.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/uvicorn ]; then
  python3 -m venv --system-site-packages .venv && .venv/bin/pip install -q -r ../requirements.txt
fi
echo "Job Hunt running at http://localhost:8000  (Ctrl+C to stop)"
exec .venv/bin/uvicorn app:app --port 8000
