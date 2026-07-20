"""Goal Review Council — Phase 1.

A run is one triggered council review of a goal; it fans out into stages
(reviewer-agent tasks). Phase 1 runs goal_health → convener. Reviewer agents
submit their review_details via the submit_review tool, which POSTs to
/internal/council/review; the endpoint validates per agent_type and stores the
result on the stage. The dispatcher orchestrates: claim a pending run (globally
one at a time), run each stage's RA, then deliver the convener packet to GO.

See GOAL_REVIEW_COUNCIL_PHASE1.md for the full design.
"""
