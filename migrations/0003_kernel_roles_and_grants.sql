-- 0003_kernel_roles_and_grants — the cluster roles, and what each may touch.
--
-- GENERATED from migrations/kernel_roles_grants_seed.sql by
-- publish/build_baseline.py. Do not hand-edit: change it there and
-- regenerate, or the two will disagree about the security model, which
-- is the one place they must not.
--
-- ── WHY THIS IS A MIGRATION AND NOT A SETUP SCRIPT ─────────────────────
--
-- These roles ARE the privilege boundary. The control plane lowers
-- itself to `agentos_human` for the two writes that carry authority
-- (app/services/errand_grants.py), and the verifier lowers itself to
-- `agentos_verifier` to write a verdict. Without them, approving an
-- errand fails at runtime with: role "agentos_human" does not exist.
--
-- They used to be created only by `scripts/infra/db_bootstrap.sh`, which
-- runs as part of the EXECUTION PLANE — and the execution plane is
-- optional. So an installation that stopped after the control plane had
-- no `agentos_human`, and found out when somebody approved their first
-- errand.
--
-- A migration is the right home because it is the one thing that runs in
-- every installation, with database access, before anything serves: the
-- migrate job already holds the DSN and the Cloud SQL connection. A
-- Cloud SQL DSN is a unix-socket DSN, so psql on a workstation cannot
-- open it — which is why db_bootstrap.sh needs either a reachable DSN or
-- a conductor pod, and why a control-plane-only install has neither.
--
-- Idempotent: every CREATE ROLE is guarded, and GRANT is already
-- idempotent in PostgreSQL.

-- The parts a schema-only dump cannot carry: roles, grants, seed rows.
--
-- Moving the kernel into the control panel's database was done with
-- pg_dump --schema-only, which is exact for tables and indexes and
-- silently omits three things that turned out to be load-bearing.
--
-- ROLES ARE CLUSTER-WIDE, not schema objects, so agentos_worker,
-- agentos_verifier and agentos_human did not come across. Every gate
-- that asserts least privilege failed with `role "agentos_worker" does
-- not exist` -- and a missing role fails loudly, which is the good
-- case. The bad case would have been a role that existed with more
-- rights than it should have.
--
-- THE GRANTS ARE THE SECURITY MODEL, not decoration. F1 asserts a
-- worker is REFUSED when it writes a contract; that test only means
-- something if the refusal comes from the database. Regenerated from
-- the old cluster rather than retyped, then remapped: the three merged
-- tables are public.* now, and checkpoints is execution_checkpoints.
--
-- SEED ROWS ARE REFERENCE DATA. run_state_machine holds the six
-- versioned state machines every execution FKs to, and
-- admission_buckets the eight admission profiles. Without them the
-- first insert fails on a foreign key -- which is exactly how this was
-- found. Test fixtures were deliberately NOT carried over.

BEGIN;

-- ── roles ────────────────────────────────────────────────────────────
DO $ROLES$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_worker') THEN
        CREATE ROLE agentos_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_verifier') THEN
        CREATE ROLE agentos_verifier NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentos_human') THEN
        CREATE ROLE agentos_human NOLOGIN;
    END IF;
END
$ROLES$;

GRANT USAGE ON SCHEMA kernel TO agentos_worker, agentos_verifier, agentos_human;
GRANT USAGE ON SCHEMA public TO agentos_worker, agentos_verifier, agentos_human;

-- The connecting user must be able to SET ROLE to them.
--
-- PARAMETERISED, because the login's name is the installation's to
-- choose. Grant to whichever exist and say nothing about the ones that
-- do not -- a fresh database legitimately has only some of them.
DO $GRANTS$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY['app_user', 'migrate_user', 'agentos']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = target) THEN
            EXECUTE format(
                'GRANT agentos_worker, agentos_verifier, agentos_human TO %I',
                target);
            RAISE NOTICE 'granted kernel roles to %', target;
        END IF;
    END LOOP;
END
$GRANTS$;

-- ── grants ───────────────────────────────────────────────────────────
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.admission_buckets TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.admission_slots TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON public.approvals TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.assistant_requests TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.execution_checkpoints TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.directives TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.execution_leases TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.execution_mounts TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.execution_transitions TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.executions TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON public.goal_resources TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.projections TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.proposals TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.provider_health TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.questions TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.reconcile_probes TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.run_state_machine TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.standing_answers TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.step_journal TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON public.story_resources TO agentos_worker;
GRANT DELETE, INSERT, SELECT, UPDATE ON kernel.verdict_parity TO agentos_worker;
GRANT INSERT ON kernel.criterion_baselines TO agentos_verifier;
GRANT INSERT, SELECT ON kernel.acceptances TO agentos_human;
GRANT INSERT, SELECT ON kernel.contracts TO agentos_worker;
GRANT INSERT, SELECT ON kernel.criteria TO agentos_worker;
GRANT INSERT, SELECT ON kernel.criterion_results TO agentos_verifier;
GRANT INSERT, SELECT ON kernel.env_deltas TO agentos_worker;
GRANT INSERT, SELECT ON kernel.verdicts TO agentos_verifier;
GRANT SELECT ON kernel.acceptances TO agentos_worker;
GRANT SELECT ON kernel.admission_buckets TO agentos_human;
GRANT SELECT ON kernel.admission_buckets TO agentos_verifier;
GRANT SELECT ON kernel.admission_slots TO agentos_human;
GRANT SELECT ON kernel.admission_slots TO agentos_verifier;
GRANT SELECT ON public.approvals TO agentos_human;
GRANT SELECT ON public.approvals TO agentos_verifier;
GRANT SELECT ON kernel.execution_checkpoints TO agentos_human;
GRANT SELECT ON kernel.execution_checkpoints TO agentos_verifier;
GRANT SELECT ON kernel.contracts TO agentos_human;
GRANT SELECT ON kernel.contracts TO agentos_verifier;
GRANT SELECT ON kernel.criteria TO agentos_human;
GRANT SELECT ON kernel.criteria TO agentos_verifier;
GRANT SELECT ON kernel.criterion_baselines TO agentos_worker;
GRANT SELECT ON kernel.criterion_results TO agentos_human;
GRANT SELECT ON kernel.criterion_results TO agentos_worker;
GRANT SELECT ON kernel.directives TO agentos_human;
GRANT SELECT ON kernel.directives TO agentos_verifier;
GRANT SELECT ON kernel.env_deltas TO agentos_human;
GRANT SELECT ON kernel.env_deltas TO agentos_verifier;
GRANT SELECT ON kernel.execution_leases TO agentos_human;
GRANT SELECT ON kernel.execution_leases TO agentos_verifier;
GRANT SELECT ON kernel.execution_transitions TO agentos_human;
GRANT SELECT ON kernel.execution_transitions TO agentos_verifier;
GRANT SELECT ON kernel.executions TO agentos_human;
GRANT SELECT ON kernel.executions TO agentos_verifier;
GRANT SELECT ON kernel.projections TO agentos_human;
GRANT SELECT ON kernel.projections TO agentos_verifier;
GRANT SELECT ON kernel.proposals TO agentos_human;
GRANT SELECT ON kernel.proposals TO agentos_verifier;
GRANT SELECT ON kernel.questions TO agentos_human;
GRANT SELECT ON kernel.questions TO agentos_verifier;
GRANT SELECT ON kernel.reconcile_probes TO agentos_human;
GRANT SELECT ON kernel.reconcile_probes TO agentos_verifier;
GRANT SELECT ON kernel.run_state_machine TO agentos_human;
GRANT SELECT ON kernel.run_state_machine TO agentos_verifier;
GRANT SELECT ON kernel.standing_answers TO agentos_human;
GRANT SELECT ON kernel.standing_answers TO agentos_verifier;
GRANT SELECT ON kernel.step_journal TO agentos_human;
GRANT SELECT ON kernel.step_journal TO agentos_verifier;
GRANT SELECT ON kernel.verdict_parity TO agentos_human;
GRANT SELECT ON kernel.verdict_parity TO agentos_verifier;
GRANT SELECT ON kernel.verdicts TO agentos_human;
GRANT SELECT ON kernel.verdicts TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.acceptances_id_seq TO agentos_human;
GRANT USAGE ON SEQUENCE kernel.acceptances_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.admission_slots_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.approvals_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.assistant_requests_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.execution_checkpoints_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.contracts_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.criteria_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.criterion_baselines_id_seq TO agentos_verifier;
GRANT USAGE ON SEQUENCE kernel.criterion_baselines_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.criterion_results_id_seq TO agentos_verifier;
GRANT USAGE ON SEQUENCE kernel.criterion_results_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.directives_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.env_deltas_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.execution_leases_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.execution_mounts_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.execution_transitions_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.executions_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.goal_resources_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.projections_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.proposals_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.questions_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.reconcile_probes_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.standing_answers_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.step_journal_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.story_resources_id_seq TO agentos_worker;
GRANT USAGE ON SEQUENCE kernel.verdicts_id_seq TO agentos_verifier;
GRANT USAGE ON SEQUENCE kernel.verdicts_id_seq TO agentos_worker;

COMMIT;
