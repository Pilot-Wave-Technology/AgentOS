# Dashboard templates

Quick reference for the `/dashboard` template layer. For the full implementation
reference — panels, architecture decisions, not-applicable items, roadmap — see
[`documentation/DASHBOARD.md`](../../documentation/DASHBOARD.md).

## Partial layout

```
dashboard/
  index.html                    Page shell, Chart.js init, localStorage "since last visit"
  partials/
    body.html                   Layout switchboard — all-clear vs alert, all includes
    _activity_list.html         Shared row renderer (used by system + personal feeds)

    # System panels (admin & editor)
    system_allclear.html        Green "All systems healthy" banner (replaces health+attention)
    system_health.html          Gateway/DB/nodes health strip
    system_cost.html            Cost: totals, breakdown, forecast, cache savings, pie chart
    system_slos.html            5 SLO tiles: completion, autonomy, recovery, p95, avg duration
    system_audit_trail.html     Ledger chain integrity + total events
    system_attention.html       Operator action inbox
    system_anomalies.html       Cost/failure spike banners
    system_fleet.html           Fleet stat row (goals, agents, users, sessions, tasks)
    system_goal_velocity.html   Goal pace vs deadline (only when deadlines set)
    system_sandbox.html         Podman/Docker sandbox (requires insights plugin)
    system_sessions.html        Live session previews
    system_usage.html           Top tools + top agents by cost
    system_reviews.html         Reviewer quality leaderboard
    system_escalations.html     Escalation hot-spots by goal
    system_cron.html            Scheduled automations
    system_channels.html        Platform channel connectivity
    system_approval_sla.html    Approval wait times
    system_deployments.html     Deployment event feed

    # Personal panels (all users)
    _personal_tiles.html        Shared: goals+tasks+agents+activity (Overview + My Work tabs)
    personal_at_risk.html       Goals-at-risk amber card
    personal_standups.html      Standup digest
```

## The adapter-boundary rule

**Every OpenClaw gateway call goes through `app/services/dashboard_metrics.py`
and nowhere else.** Templates receive pre-formatted strings (`"$142.50"`,
`"1.2M"`, `"3.5s"`) — zero business logic in HTML.

## Adding a panel

1. Add `_system_<name>()` in `dashboard_metrics.py`, return `{..., "_degraded": []}`.
2. Add it to the `asyncio.gather` in `collect_system()` and unpack.
3. Create `partials/system_<name>.html`.
4. Include it in `partials/body.html`.

See `documentation/DASHBOARD.md` for the full how-to and the OpenClaw extension pattern.
