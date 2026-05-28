# web  → alert_intake.py  operational API  :$PORT
# ui   → workflow_ui.py   demo dashboard   :7000
# Note: Railway runs web only by default.
# Add a separate service for ui if needed.

web: uvicorn alert_intake:app --host 0.0.0.0 --port $PORT
ui: uvicorn workflow_ui:app --host 0.0.0.0 --port 7000
