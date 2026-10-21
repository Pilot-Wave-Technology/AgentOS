-- A goal's own leg size and leg count for its executor.
--
-- Set on the goal's settings page beside the executor model. NULL means
-- "the default for the model and the work", exactly as executor_model's
-- NULL does. Frozen onto the run at dispatch (`executions.leg_budget`),
-- because the pod can read executions and not goals, and so editing the
-- goal changes the next run rather than a running one.
ALTER TABLE goals ADD COLUMN IF NOT EXISTS leg_context_tokens INTEGER;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS max_legs INTEGER;

COMMENT ON COLUMN goals.leg_context_tokens IS
    'Tokens of context per executor leg for this goal; NULL = the model''s default. '
    'Frozen onto executions.leg_budget at dispatch.';
COMMENT ON COLUMN goals.max_legs IS
    'Most executor legs a run of this goal may take; -1 = no limit, NULL = default (12). '
    'Frozen onto executions.leg_budget at dispatch.';

ALTER TABLE kernel.executions ADD COLUMN IF NOT EXISTS leg_budget JSONB;

COMMENT ON COLUMN kernel.executions.leg_budget IS
    'The goal''s leg settings pinned at dispatch: {"context_tokens", "max_legs"}, '
    'only the keys a person set. NULL = nobody chose; the defaults apply.';
