"""Security Council — branch-scoped cybersecurity review.

Parallel to app/services/council (the Goal Review Council): same dispatcher /
stage-DAG / provisioning patterns, but the review unit is a BRANCH (repo +
branch), the aspect agents are shell-capable (they scan a server-prepared
read-only checkout with offline scanners), and delivery fans out to every active
goal that references the branch. See SECURITY_COUNCIL_PLAN.md.
"""
