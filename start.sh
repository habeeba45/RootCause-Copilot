#!/usr/bin/env bash
# macOS / Linux: start backend and frontend together (run setup steps in README first).
cd "$(dirname "$0")"
(cd backend && source .venv/bin/activate && uvicorn app.main:app --port 8000 --reload) &
(cd frontend && npm run dev) &
wait
