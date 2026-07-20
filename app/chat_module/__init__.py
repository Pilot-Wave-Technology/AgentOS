"""chat_module — durable, scalable chat ingress/dispatch for the control panel.

Self-contained package implementing the CHAT_SERVICE_PLAN:
  - durable per-thread message queue (Postgres `messages` table, Phase 0 cols)
  - gateway registry + routing seam (multi-gateway ready)
  - signal-only LISTEN/NOTIFY fan-out (cross-instance group threads)
  - advisory-lock serialized dispatcher with collect-window batching

Kept deliberately separated from the rest of `app/`. The only shared
dependencies are `app.config` (connection values) and `app.database` (pool).
Wiring into existing routers is gated behind `CHAT_QUEUE_ENABLED`.
"""
