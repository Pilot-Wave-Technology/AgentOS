--
-- PostgreSQL database dump
--


-- Dumped from database version 17.9
-- Dumped by pg_dump version 17.10

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

-- *not* creating schema, since initdb creates it


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS '';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: council_notify_story_done(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.council_notify_story_done() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM pg_notify(
        'story_done',
        json_build_object('goal_id', NEW.goal_id, 'story_id', NEW.id)::text
    );
    RETURN NEW;
END;
$$;


--
-- Name: prevent_ledger_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.prevent_ledger_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'ledger_events is append-only: % not allowed', TG_OP;
    RETURN NULL;
END;
$$;


--
-- Name: prune_webhook_deliveries(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.prune_webhook_deliveries() RETURNS integer
    LANGUAGE plpgsql
    AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM webhook_deliveries
     WHERE created_at < NOW() - INTERVAL '7 days';
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;


--
-- Name: refresh_mat_view(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.refresh_mat_view(p_view_name text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
BEGIN
  EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I', p_view_name);
END;
$$;


--
-- Name: update_activity_search_vector(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_activity_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        COALESCE(NEW.summary, '') || ' ' ||
        COALESCE(NEW.content, ''));
    RETURN NEW;
END;
$$;


--
-- Name: update_memory_fields(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_memory_fields() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        COALESCE(NEW.title, '') || ' ' || COALESCE(NEW.content, '') || ' ' ||
        COALESCE(NEW.human_name, '') || ' ' ||
        COALESCE(NEW.agent_template_name, '') || ' ' ||
        COALESCE(array_to_string(NEW.tags, ' '), ''));
    NEW.content_size := length(NEW.content);
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


--
-- Name: update_messages_search_vector(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_messages_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english', COALESCE(NEW.content, ''));
    RETURN NEW;
END;
$$;


--
-- Name: update_tasks_search_vector(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_tasks_search_vector() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.search_vector := to_tsvector('english',
        COALESCE(NEW.title, '') || ' ' ||
        COALESCE(NEW.result, '') || ' ' ||
        COALESCE(NEW.description, '') || ' ' ||
        COALESCE(NEW.blocked_reason, ''));
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: actors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.actors (
    id uuid NOT NULL,
    type text NOT NULL,
    uid text,
    agent_id text,
    display_name text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT actors_type_check CHECK ((type = ANY (ARRAY['user'::text, 'agent'::text, 'system'::text])))
);


--
-- Name: COLUMN actors.created_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.actors.created_at IS 'Creation timestamp. NOTE: actors has no updated_at column — display_name is the only mutable field; mutation time is in ledger_events (AGENT_UPDATED).';


--
-- Name: agent_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    config_version integer NOT NULL,
    model text NOT NULL,
    type text NOT NULL,
    scope text NOT NULL,
    emoji text DEFAULT ''::text NOT NULL,
    tools_allow text[] DEFAULT '{}'::text[] NOT NULL,
    tools_deny text[] DEFAULT '{}'::text[] NOT NULL,
    memory_enabled boolean DEFAULT false NOT NULL,
    max_tokens integer,
    notes text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    CONSTRAINT agent_configs_scope_check CHECK ((scope = ANY (ARRAY['goal'::text, 'task'::text]))),
    CONSTRAINT agent_configs_type_check CHECK ((type = ANY (ARRAY['SHCA'::text, 'IA'::text, 'GO'::text, 'RA'::text, 'Platform'::text, 'PlatformResearch'::text, 'PlatformAssistant'::text])))
);


--
-- Name: agent_contexts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_contexts (
    goal_id text NOT NULL,
    agent_id text NOT NULL,
    additional_instructions text DEFAULT ''::text NOT NULL,
    skills text[] DEFAULT '{}'::text[] NOT NULL,
    workspace_files text[] DEFAULT '{}'::text[] NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text NOT NULL
);


--
-- Name: agent_file_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_file_history (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    filename text NOT NULL,
    content_hash text NOT NULL,
    file_size_bytes integer DEFAULT 0 NOT NULL,
    config_version integer NOT NULL,
    written_at timestamp with time zone DEFAULT now() NOT NULL,
    written_by text
);


--
-- Name: agent_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_files (
    agent_id text NOT NULL,
    filename text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    written_at timestamp with time zone DEFAULT now() NOT NULL,
    written_by text DEFAULT ''::text NOT NULL,
    content_hash text DEFAULT ''::text NOT NULL,
    file_size_bytes integer DEFAULT 0 NOT NULL,
    gateway_synced_at timestamp with time zone
);


--
-- Name: COLUMN agent_files.written_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_files.written_at IS 'Timestamp of the most recent write. Functions as updated_at. No separate created_at column — treat the agent row''s created_at as the file creation time for files written at provisioning.';


--
-- Name: agent_gateway_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_gateway_bindings (
    agent_id text NOT NULL,
    gateway_agent_id text NOT NULL,
    workspace text DEFAULT ''::text NOT NULL,
    synced_config_version integer,
    all_files_hash text,
    last_synced_at timestamp with time zone,
    last_sync_error text,
    last_sync_attempted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    gateway_id text
);


--
-- Name: agent_instances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_instances (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    template_id text NOT NULL,
    gateway_agent_id text NOT NULL,
    task_id text,
    goal_id text NOT NULL,
    workspace text DEFAULT ''::text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    destroyed_at timestamp with time zone,
    status text DEFAULT 'active'::text NOT NULL,
    thread_id text,
    cli_session_id text,
    worker_type text DEFAULT 'standard'::text NOT NULL,
    parent_task_id text,
    total_tokens jsonb DEFAULT '{}'::jsonb,
    owner_actor_id uuid,
    CONSTRAINT agent_instances_status_check CHECK ((status = ANY (ARRAY['active'::text, 'destroyed'::text])))
);


--
-- Name: agent_memories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_memories (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    agent_template_id text NOT NULL,
    agent_template_name text NOT NULL,
    scope text NOT NULL,
    goal_id text,
    thread_id text,
    memory_type text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    human_uid text,
    human_name text,
    source_task_id text,
    source_thread_id text,
    related_files text[] DEFAULT '{}'::text[],
    source text DEFAULT 'agent'::text NOT NULL,
    tags text[] DEFAULT '{}'::text[],
    search_vector tsvector,
    content_size integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT agent_memories_memory_type_check CHECK ((memory_type = ANY (ARRAY['preference'::text, 'observation'::text, 'context'::text, 'convention'::text]))),
    CONSTRAINT agent_memories_scope_check CHECK ((scope = ANY (ARRAY['global'::text, 'goal'::text, 'conversation'::text]))),
    CONSTRAINT agent_memories_source_check CHECK ((source = ANY (ARRAY['agent'::text, 'system'::text, 'human'::text]))),
    CONSTRAINT agent_memories_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text, 'compacted'::text])))
);


--
-- Name: agent_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_metrics (
    id bigint NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    metric text NOT NULL,
    agent_id text,
    goal_id uuid,
    value numeric,
    dimensions jsonb DEFAULT '{}'::jsonb NOT NULL,
    goal_id_text text
);


--
-- Name: COLUMN agent_metrics.goal_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agent_metrics.goal_id IS 'Deprecated — always NULL for rows written after agent_metrics_goal_id_fix migration. Was incorrectly typed UUID; goals.id is a short TEXT slug. Use goal_id_text instead.';


--
-- Name: agent_metrics_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.agent_metrics_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_metrics_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.agent_metrics_id_seq OWNED BY public.agent_metrics.id;


--
-- Name: agent_sync_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_sync_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    from_version integer,
    to_version integer NOT NULL,
    files_synced text[] DEFAULT '{}'::text[] NOT NULL,
    success boolean NOT NULL,
    error_msg text,
    duration_ms integer,
    triggered_by text,
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: agent_workspace_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_workspace_files (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    filename text NOT NULL,
    content text NOT NULL,
    bytes integer NOT NULL,
    snapshot_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot_reason text NOT NULL,
    CONSTRAINT agent_workspace_files_snapshot_reason_check CHECK ((snapshot_reason = ANY (ARRAY['pre_reap'::text, 'periodic'::text, 'manual'::text, 'pre_gateway_restart'::text])))
);


--
-- Name: agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agents (
    id text NOT NULL,
    actor_id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    type text DEFAULT 'SHCA'::text NOT NULL,
    scope text DEFAULT 'task'::text NOT NULL,
    model text DEFAULT 'google/gemini-2.5-flash'::text NOT NULL,
    emoji text DEFAULT ''::text NOT NULL,
    base_system_prompt text DEFAULT ''::text NOT NULL,
    soul text DEFAULT ''::text NOT NULL,
    tools_allow text[] DEFAULT '{}'::text[] NOT NULL,
    tools_deny text[] DEFAULT '{}'::text[] NOT NULL,
    memory_enabled boolean DEFAULT false NOT NULL,
    max_tokens integer,
    notes text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    gateway_agent_id text DEFAULT ''::text NOT NULL,
    workspace text DEFAULT ''::text NOT NULL,
    creation_params jsonb DEFAULT '{}'::jsonb NOT NULL,
    owner_actor_id uuid,
    auto_created boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    config_version integer DEFAULT 1 NOT NULL,
    gateway_synced_version integer,
    gateway_synced_at timestamp with time zone,
    gateway_sync_attempted_at timestamp with time zone,
    is_template boolean DEFAULT false NOT NULL,
    template_id text,
    last_active_at timestamp with time zone,
    CONSTRAINT agents_scope_check CHECK ((scope = ANY (ARRAY['goal'::text, 'task'::text]))),
    CONSTRAINT agents_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleted'::text]))),
    CONSTRAINT agents_type_check CHECK ((type = ANY (ARRAY['SHCA'::text, 'IA'::text, 'GO'::text, 'RA'::text, 'Platform'::text, 'PlatformResearch'::text, 'PlatformAssistant'::text])))
);


--
-- Name: COLUMN agents.base_system_prompt; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.base_system_prompt IS 'DEPRECATED. Content now lives in agent_files (filename=''AGENTS.md''). Pending removal.';


--
-- Name: COLUMN agents.soul; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.soul IS 'DEPRECATED. Content now lives in agent_files (filename=''SOUL.md''). Pending removal.';


--
-- Name: COLUMN agents.gateway_agent_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.gateway_agent_id IS 'LEGACY DUPLICATE of agent_gateway_bindings.gateway_agent_id. The binding table is canonical; do not read this column for sync decisions.';


--
-- Name: COLUMN agents.workspace; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.workspace IS 'LEGACY DUPLICATE of agent_gateway_bindings.workspace. The binding table is canonical.';


--
-- Name: COLUMN agents.creation_params; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.creation_params IS 'DEPRECATED. Provisioning snapshot now in ledger_events (type=''agent_provisioned''). Pending removal.';


--
-- Name: COLUMN agents.auto_created; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.auto_created IS 'DEPRECATED. No downstream consumers. Pending removal.';


--
-- Name: COLUMN agents.gateway_synced_version; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.gateway_synced_version IS 'LEGACY DUPLICATE of agent_gateway_bindings.synced_config_version. Both are null-written on every update; the binding table is canonical. Pending deprecation once all readers are migrated.';


--
-- Name: COLUMN agents.gateway_synced_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.gateway_synced_at IS 'LEGACY DUPLICATE of agent_gateway_bindings.last_synced_at. Pending deprecation.';


--
-- Name: COLUMN agents.is_template; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.agents.is_template IS 'DEPRECATED. Use NOT EXISTS (SELECT 1 FROM templates WHERE templates.id = agents.id). Pending removal.';


--
-- Name: approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.approvals (
    id uuid NOT NULL,
    story_id text NOT NULL,
    kind text NOT NULL,
    payload jsonb,
    payload_ref text,
    channels text[],
    proposed_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    decided_by text,
    decision text,
    feedback text,
    timeout_at timestamp with time zone,
    escalation_count integer DEFAULT 0 NOT NULL
);


--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_log (
    id text NOT NULL,
    goal_id text NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chain_head; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chain_head (
    id integer NOT NULL,
    last_event_id uuid NOT NULL,
    last_hash text NOT NULL,
    event_count bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chain_head_id_check CHECK ((id = 1))
);


--
-- Name: chat_run_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_run_state (
    thread_id text NOT NULL,
    run_id text NOT NULL,
    status text NOT NULL,
    current_tool text DEFAULT ''::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: chat_thread_routing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_thread_routing (
    thread_id text NOT NULL,
    goal_id text NOT NULL,
    agent_id text NOT NULL,
    gateway_agent_id text NOT NULL,
    agent_type text DEFAULT 'SHCA'::text NOT NULL,
    gateway_id text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoints (
    id uuid NOT NULL,
    story_id text NOT NULL,
    name text NOT NULL,
    approval_id uuid,
    payload jsonb,
    git_sha text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: claude_phases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claude_phases (
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    allowed_tools text[] DEFAULT '{}'::text[] NOT NULL,
    denied_tools text[] DEFAULT '{}'::text[] NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT claude_phases_status_check CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text])))
);


--
-- Name: council_review_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.council_review_runs (
    id text NOT NULL,
    goal_id text NOT NULL,
    trigger text DEFAULT 'manual'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    kind text DEFAULT 'goal'::text NOT NULL,
    story_id text,
    reviewed_state text,
    CONSTRAINT council_review_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: council_review_stages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.council_review_stages (
    id text NOT NULL,
    run_id text NOT NULL,
    goal_id text NOT NULL,
    stage text NOT NULL,
    story_id text,
    status text DEFAULT 'pending'::text NOT NULL,
    depends_on text[] DEFAULT '{}'::text[] NOT NULL,
    instance_id text,
    output jsonb,
    error text,
    claimed_by text,
    claimed_at timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reviewed_state text,
    CONSTRAINT council_review_stages_stage_check CHECK ((stage = ANY (ARRAY['goal_health'::text, 'progress'::text, 'story'::text, 'convener'::text]))),
    CONSTRAINT council_review_stages_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'blocked_on_deps'::text, 'running'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: dag_ceiling_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dag_ceiling_config (
    id text DEFAULT 'singleton'::text NOT NULL,
    n integer DEFAULT 4 NOT NULL,
    enforce boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text,
    CONSTRAINT dag_ceiling_config_n_check CHECK ((n >= 1))
);


--
-- Name: dashboard_cache_meta; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_cache_meta (
    view_name text NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: execution_pauses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.execution_pauses (
    scope text NOT NULL,
    scope_id text DEFAULT ''::text NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    paused_by text DEFAULT ''::text NOT NULL,
    paused_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT execution_pauses_scope_check CHECK ((scope = ANY (ARRAY['system'::text, 'goal'::text, 'agent_instance'::text])))
);


--
-- Name: gateway_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gateway_cache (
    cache_key text NOT NULL,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: gateways; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gateways (
    id text NOT NULL,
    name text,
    ws_url text NOT NULL,
    internal_ip text,
    status text DEFAULT 'active'::text NOT NULL,
    weight integer DEFAULT 1 NOT NULL,
    max_agents integer DEFAULT 1000 NOT NULL,
    current_agents integer DEFAULT 0 NOT NULL,
    last_heartbeat_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gateways_status_check CHECK ((status = ANY (ARRAY['active'::text, 'draining'::text, 'down'::text])))
);


--
-- Name: git_merge_group_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_merge_group_nodes (
    merge_group_id text NOT NULL,
    git_node_id text NOT NULL,
    seq integer DEFAULT 0 NOT NULL
);


--
-- Name: git_merge_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_merge_groups (
    id text NOT NULL,
    goal_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: git_merge_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_merge_queue (
    node_id text NOT NULL,
    git_repo_id text NOT NULL,
    goal_id text NOT NULL,
    enqueue_seq bigint NOT NULL,
    status text DEFAULT 'waiting'::text NOT NULL,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    trunk_branch text,
    active_since timestamp with time zone,
    CONSTRAINT git_merge_queue_status_check CHECK ((status = ANY (ARRAY['waiting'::text, 'active'::text, 'merged'::text, 'parked'::text])))
);


--
-- Name: git_merge_queue_enqueue_seq_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.git_merge_queue_enqueue_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: git_merge_queue_enqueue_seq_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.git_merge_queue_enqueue_seq_seq OWNED BY public.git_merge_queue.enqueue_seq;


--
-- Name: git_node_stories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_node_stories (
    git_node_id text NOT NULL,
    story_id text NOT NULL,
    seq integer DEFAULT 0 NOT NULL
);


--
-- Name: git_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_nodes (
    id text NOT NULL,
    git_repo_id text NOT NULL,
    goal_id text NOT NULL,
    branch_name text NOT NULL,
    base_sha text,
    state text DEFAULT 'PLANNED'::text NOT NULL,
    merge_seq integer,
    pr_number integer,
    last_synced_trunk_sha text,
    sync_target_sha text,
    pre_merge_trunk_sha text,
    merge_commit_sha text,
    mergeable text DEFAULT 'unknown'::text NOT NULL,
    conflict_paths jsonb,
    conflict_base_sha text,
    repark_count integer DEFAULT 0 NOT NULL,
    live_resynced_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    trunk_branch text,
    pr_opened_notified_at timestamp with time zone,
    CONSTRAINT git_nodes_mergeable_check CHECK ((mergeable = ANY (ARRAY['yes'::text, 'no'::text, 'unknown'::text]))),
    CONSTRAINT git_nodes_state_check CHECK ((state = ANY (ARRAY['PLANNED'::text, 'BRANCHING'::text, 'BRANCH_CREATED'::text, 'IN_PROGRESS'::text, 'READY'::text, 'QUEUED'::text, 'SYNCING'::text, 'MERGEABLE'::text, 'MERGING'::text, 'MERGED'::text, 'CONFLICTED'::text, 'ABANDONED'::text, 'PROTECTION_BLOCKED'::text, 'ESCALATED'::text])))
);


--
-- Name: git_repo_trunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_repo_trunks (
    git_repo_id text NOT NULL,
    trunk_branch text NOT NULL,
    trunk_sha text,
    trunk_sha_seen_at timestamp with time zone
);


--
-- Name: git_repos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_repos (
    id text NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    clone_url text,
    trunk_branch text DEFAULT 'main'::text NOT NULL,
    trunk_sha text,
    trunk_sha_seen_at timestamp with time zone,
    protected boolean DEFAULT false NOT NULL,
    merge_method text DEFAULT 'merge'::text NOT NULL,
    linear_history boolean DEFAULT false NOT NULL,
    required_checks jsonb DEFAULT '[]'::jsonb NOT NULL,
    required_reviews integer DEFAULT 0 NOT NULL,
    merge_queue_enabled boolean DEFAULT false NOT NULL,
    webhook_ok boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT git_repos_linear_not_merge CHECK ((NOT (linear_history AND (merge_method = 'merge'::text)))),
    CONSTRAINT git_repos_merge_method_check CHECK ((merge_method = ANY (ARRAY['merge'::text, 'squash'::text, 'rebase'::text])))
);


--
-- Name: git_webhook_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.git_webhook_events (
    delivery_id text NOT NULL,
    event text NOT NULL,
    action text,
    git_repo_id text,
    repo_full_name text,
    pr_number integer,
    merged boolean,
    delivered boolean DEFAULT false NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: go_conversation_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.go_conversation_summaries (
    id bigint NOT NULL,
    goal_id text NOT NULL,
    summary text NOT NULL,
    covers_from timestamp with time zone NOT NULL,
    covers_to timestamp with time zone NOT NULL,
    message_count integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: go_conversation_summaries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.go_conversation_summaries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: go_conversation_summaries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.go_conversation_summaries_id_seq OWNED BY public.go_conversation_summaries.id;


--
-- Name: go_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.go_memory (
    id bigint NOT NULL,
    goal_id text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    related_task text DEFAULT ''::text,
    related_resource text DEFAULT ''::text,
    actor text DEFAULT ''::text,
    tier text DEFAULT 'recent'::text,
    compacted_from bigint[],
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: go_memory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.go_memory_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: go_memory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.go_memory_id_seq OWNED BY public.go_memory.id;


--
-- Name: goal_agent_contexts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_agent_contexts (
    goal_id text NOT NULL,
    agent_id text NOT NULL,
    additional_instructions text DEFAULT ''::text NOT NULL,
    skills text[] DEFAULT '{}'::text[] NOT NULL,
    workspace_files text[] DEFAULT '{}'::text[] NOT NULL,
    config_version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text
);


--
-- Name: goal_agent_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_agent_memberships (
    goal_id text NOT NULL,
    agent_id text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    added_by text,
    removed_at timestamp with time zone,
    removed_by text,
    CONSTRAINT goal_agent_memberships_status_check CHECK ((status = ANY (ARRAY['active'::text, 'removed'::text])))
);


--
-- Name: goal_decisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_decisions (
    id text NOT NULL,
    goal_id text NOT NULL,
    decision text NOT NULL,
    rationale text DEFAULT ''::text,
    decided_by text DEFAULT ''::text,
    story_id text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: goal_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_documents (
    goal_id text NOT NULL,
    doc_type text NOT NULL,
    content text,
    requested_at timestamp with time zone,
    generated_at timestamp with time zone,
    generated_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT goal_documents_doc_type_check CHECK ((doc_type = ANY (ARRAY['code'::text, 'progress'::text])))
);


--
-- Name: goal_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_members (
    goal_id text NOT NULL,
    uid text NOT NULL,
    role text DEFAULT 'member'::text NOT NULL,
    added_by text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    pinned_at timestamp with time zone,
    CONSTRAINT goal_members_role_check CHECK ((role = ANY (ARRAY['admin'::text, 'member'::text])))
);


--
-- Name: goal_platform_integrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_platform_integrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    goal_id text NOT NULL,
    integration_id uuid NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    email_domain_allowlist text[] DEFAULT '{}'::text[],
    config jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: goal_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_resources (
    id text NOT NULL,
    goal_id text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    uri text DEFAULT ''::text,
    parent_id text,
    source_task_id text DEFAULT ''::text,
    created_by text NOT NULL,
    locked_by text,
    locked_for text,
    locked_at timestamp with time zone,
    status text DEFAULT 'active'::text,
    metadata jsonb DEFAULT '{}'::jsonb,
    human_context text DEFAULT ''::text,
    manifest jsonb DEFAULT '{}'::jsonb,
    last_scanned_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    repo_owner text DEFAULT ''::text,
    repo_name text DEFAULT ''::text,
    default_branch text DEFAULT 'main'::text,
    clone_url text DEFAULT ''::text,
    branch_name text DEFAULT ''::text,
    base_branch text DEFAULT ''::text,
    head_sha text DEFAULT ''::text,
    file_path text DEFAULT ''::text,
    file_type text DEFAULT ''::text,
    file_size_bytes bigint DEFAULT 0,
    gcs_uri text DEFAULT ''::text,
    last_summary_at timestamp with time zone,
    summary_stale boolean DEFAULT true,
    source_resource_id text DEFAULT ''::text,
    role text DEFAULT 'feature'::text NOT NULL,
    git_repo_id text
);


--
-- Name: goal_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_summaries (
    goal_id text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    requested_at timestamp with time zone,
    generated_at timestamp with time zone,
    generated_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: goal_summary; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goal_summary (
    goal_id text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    checkpoint_at timestamp with time zone,
    version integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: goals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.goals (
    id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    color_tag text DEFAULT '#6366f1'::text NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    memory_goal_max_kb integer,
    memory_conversation_max_kb integer,
    memory_compaction_target_pct integer,
    memory_recent_messages integer,
    memory_auto_summary_interval integer,
    memory_stale_conversation_days integer,
    memory_instance_idle_minutes integer,
    objective text DEFAULT ''::text,
    deadline timestamp with time zone,
    priority text DEFAULT 'medium'::text,
    state text DEFAULT 'active'::text,
    completion_pct smallint DEFAULT 0,
    state_doc jsonb DEFAULT '{}'::jsonb,
    state_version integer DEFAULT 0,
    go_agent_id text DEFAULT ''::text,
    go_thread_id text DEFAULT ''::text,
    settings jsonb DEFAULT '{}'::jsonb,
    orchestrator_thread_id text DEFAULT ''::text,
    weightage numeric DEFAULT 1.0 NOT NULL,
    weightage_recomputed_at timestamp with time zone,
    council_thread_id text DEFAULT ''::text,
    security_thread_id text DEFAULT ''::text,
    CONSTRAINT goals_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleted'::text])))
);


--
-- Name: integration_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.integration_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    goal_resource_id text,
    story_branch_id uuid,
    source_branch_name text NOT NULL,
    target_branch_name text NOT NULL,
    attempt_number integer DEFAULT 1 NOT NULL,
    mechanism text DEFAULT 'git_merge'::text NOT NULL,
    mr_id text,
    mr_url text,
    mr_provider text,
    result text,
    conflict_details jsonb,
    gcs_report_uri text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    updated_at timestamp with time zone,
    branch_goal_resource_id text,
    target_goal_resource_id text,
    CONSTRAINT integration_attempts_actor_type_check CHECK ((actor_type = ANY (ARRAY['story'::text, 'task'::text, 'goal'::text, 'human'::text, 'system'::text]))),
    CONSTRAINT integration_attempts_result_check CHECK ((result = ANY (ARRAY['success'::text, 'conflicted'::text, 'push_rejected'::text, 'aborted'::text])))
);


--
-- Name: join_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.join_requests (
    goal_id text NOT NULL,
    uid text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    message text DEFAULT ''::text NOT NULL,
    CONSTRAINT join_requests_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: ledger_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ledger_events (
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    envelope_version integer DEFAULT 1 NOT NULL,
    event_version integer DEFAULT 1 NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    sequence_number bigint NOT NULL,
    actor_id uuid NOT NULL,
    actor_type text DEFAULT 'human'::text NOT NULL,
    on_behalf_of uuid,
    correlation_id uuid,
    causation_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    context_task_id text DEFAULT ''::text NOT NULL,
    context_goal_id text DEFAULT ''::text NOT NULL,
    context_team_id text DEFAULT ''::text NOT NULL,
    context_conversation_id text DEFAULT ''::text NOT NULL,
    classification text DEFAULT 'normal'::text NOT NULL,
    integrity_hash text NOT NULL,
    previous_hash text DEFAULT 'GENESIS'::text NOT NULL,
    payload jsonb NOT NULL,
    extensions jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text,
    CONSTRAINT ledger_events_actor_type_check CHECK ((actor_type = ANY (ARRAY['human'::text, 'shca'::text, 'independent_agent'::text, 'system'::text, 'goal_orchestrator'::text]))),
    CONSTRAINT ledger_events_classification_check CHECK ((classification = ANY (ARRAY['normal'::text, 'sensitive'::text, 'restricted'::text])))
);


--
-- Name: ledger_events_resolved; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.ledger_events_resolved AS
 SELECT e.event_id,
    e.event_type,
    e."timestamp" AS occurred_at,
    e.actor_id,
    e.actor_type AS event_actor_type,
    a.type AS actor_registry_type,
    a.display_name AS actor_display_name,
    (a.id IS NOT NULL) AS actor_registered,
        CASE
            WHEN (a.id IS NULL) THEN NULL::boolean
            WHEN ((e.actor_type = 'human'::text) AND (a.type = 'user'::text)) THEN true
            WHEN ((e.actor_type = 'system'::text) AND (a.type = 'system'::text)) THEN true
            WHEN ((e.actor_type = ANY (ARRAY['shca'::text, 'independent_agent'::text, 'goal_orchestrator'::text])) AND (a.type = 'agent'::text)) THEN true
            ELSE false
        END AS type_consistent,
    e.on_behalf_of,
    obo.type AS on_behalf_of_type,
    obo.display_name AS on_behalf_of_display_name,
    e.context_goal_id,
    e.context_task_id,
    e.correlation_id
   FROM ((public.ledger_events e
     LEFT JOIN public.actors a ON ((a.id = e.actor_id)))
     LEFT JOIN public.actors obo ON ((obo.id = e.on_behalf_of)));


--
-- Name: ledger_events_sequence_number_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ledger_events_sequence_number_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ledger_events_sequence_number_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ledger_events_sequence_number_seq OWNED BY public.ledger_events.sequence_number;


--
-- Name: memory_prompts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_prompts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_type text NOT NULL,
    prompt_kind text NOT NULL,
    version integer NOT NULL,
    content text NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    created_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    CONSTRAINT memory_prompts_agent_type_check CHECK ((agent_type = ANY (ARRAY['SHCA'::text, 'GO'::text]))),
    CONSTRAINT memory_prompts_prompt_kind_check CHECK ((prompt_kind = ANY (ARRAY['flush_user_prompt'::text, 'flush_system_prompt'::text, 'compaction_summary_system_prompt'::text, 'story_complete_user_prompt'::text, 'story_complete_system_prompt'::text, 'pre_reap_user_prompt'::text, 'pre_reap_system_prompt'::text, 'consolidation_user_prompt'::text, 'consolidation_system_prompt'::text, 'goal_summary_user_prompt'::text, 'goal_summary_system_prompt'::text])))
);


--
-- Name: memory_writes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_writes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    agent_type text,
    goal_id text,
    story_id text,
    file_path text NOT NULL,
    written_by text NOT NULL,
    actor_id uuid,
    bytes_added integer DEFAULT 0 NOT NULL,
    diff_summary text,
    event_type text,
    written_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_writes_written_by_check CHECK ((written_by = ANY (ARRAY['agent'::text, 'bridge'::text, 'admin'::text])))
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id text NOT NULL,
    thread_id text NOT NULL,
    sender text NOT NULL,
    role text DEFAULT 'human'::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    token_count integer DEFAULT 0 NOT NULL,
    card_data jsonb,
    search_vector tsvector,
    status text DEFAULT 'done'::text NOT NULL,
    kind text DEFAULT 'user'::text NOT NULL,
    idempotency_key text,
    batch_id text,
    run_id text,
    claimed_by text,
    claimed_at timestamp with time zone,
    reply_to_message_id text,
    error text,
    platform text,
    external_message_id text,
    CONSTRAINT messages_kind_chk CHECK ((kind = ANY (ARRAY['user'::text, 'system_wake'::text, 'system_inject'::text]))),
    CONSTRAINT messages_role_check CHECK ((role = ANY (ARRAY['human'::text, 'agent'::text]))),
    CONSTRAINT messages_status_chk CHECK ((status = ANY (ARRAY['queued'::text, 'processing'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: model_pricing; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_pricing (
    id bigint NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    provider_label text,
    notes text,
    input_rate numeric(12,8) DEFAULT 0 NOT NULL,
    output_rate numeric(12,8) DEFAULT 0 NOT NULL,
    cache_read_rate numeric(12,8) DEFAULT 0 NOT NULL,
    cache_write_rate numeric(12,8) DEFAULT 0 NOT NULL,
    thinking_rate numeric(12,8),
    effective_from timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: model_pricing_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.model_pricing_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: model_pricing_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.model_pricing_id_seq OWNED BY public.model_pricing.id;


--
-- Name: stories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.stories (
    id text NOT NULL,
    goal_id text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text,
    acceptance_criteria text DEFAULT ''::text,
    assigned_to text DEFAULT ''::text,
    priority text DEFAULT 'medium'::text,
    estimated_effort text DEFAULT ''::text,
    state text DEFAULT 'proposed'::text,
    depends_on text[] DEFAULT '{}'::text[],
    blocked_reason text DEFAULT ''::text,
    result_summary text DEFAULT ''::text,
    progress_summary text DEFAULT ''::text,
    thread_id text DEFAULT ''::text,
    created_by text NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    active_thread_id text DEFAULT ''::text,
    workspace_path text,
    git_branch text,
    current_branch_name text,
    base_ref text,
    current_phase text,
    current_plan_id uuid,
    shca_instance_id text,
    current_claude_session_id text,
    current_claude_state_dir text,
    execution_mode text DEFAULT 'operator'::text NOT NULL,
    workspace_state text DEFAULT 'none'::text NOT NULL,
    workspace_state_error text,
    workspace_provisioned_at timestamp with time zone,
    execution_paused boolean DEFAULT false NOT NULL,
    current_plan_path text,
    last_reviewed_state text,
    review_final boolean DEFAULT false NOT NULL,
    story_kind text DEFAULT 'code'::text NOT NULL,
    merge_group_id text,
    merge_approved_by text,
    merge_approved_at timestamp with time zone,
    is_active_focus boolean DEFAULT false NOT NULL,
    story_weight integer,
    workspace_provision_attempts integer DEFAULT 0 NOT NULL,
    CONSTRAINT stories_execution_mode_check CHECK ((execution_mode = ANY (ARRAY['ia_dispatch'::text, 'operator'::text]))),
    CONSTRAINT stories_state_check CHECK ((state = ANY (ARRAY['proposed'::text, 'accepted'::text, 'in_progress'::text, 'blocked'::text, 'done'::text, 'reviewed'::text, 'rejected'::text, 'cancelled'::text]))),
    CONSTRAINT stories_story_kind_chk CHECK ((story_kind = ANY (ARRAY['code'::text, 'merge'::text]))),
    CONSTRAINT stories_workspace_state_check CHECK ((workspace_state = ANY (ARRAY['none'::text, 'cloning'::text, 'provisioning'::text, 'ready'::text, 'failed'::text, 'deleting'::text, 'deleted'::text])))
);


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id text NOT NULL,
    goal_id text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    assigned_to text DEFAULT ''::text NOT NULL,
    priority text DEFAULT 'medium'::text NOT NULL,
    due_at timestamp with time zone,
    depends_on text[] DEFAULT '{}'::text[] NOT NULL,
    approval_required boolean DEFAULT false NOT NULL,
    state text DEFAULT 'pending'::text NOT NULL,
    correlation_id uuid,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    thread_id text,
    result text,
    template_id text,
    instance_id text,
    parent_task_id text,
    acceptance_criteria text DEFAULT ''::text NOT NULL,
    source_thread_id text,
    blocked_reason text,
    decision_event_id text,
    acp_session_id text,
    retry_count integer DEFAULT 0 NOT NULL,
    max_retries integer DEFAULT 2 NOT NULL,
    dispatched_at timestamp with time zone,
    plan_content text,
    search_vector tsvector,
    category text DEFAULT 'task'::text,
    estimated_effort text DEFAULT ''::text,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    result_summary text DEFAULT ''::text,
    output_resources text[] DEFAULT '{}'::text[],
    blocked_by_resources text[] DEFAULT '{}'::text[],
    story_id text DEFAULT ''::text,
    review_target_id text,
    executor text DEFAULT 'ia'::text NOT NULL,
    phase text,
    claude_session_id text,
    claude_state_dir text,
    last_blocker_question text,
    last_blocker_at timestamp with time zone,
    blocker_count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    credential_cleanup_handle jsonb,
    gate_kind text,
    gate_payload jsonb,
    diff_review_via_gate boolean DEFAULT false NOT NULL,
    stall_redispatch_count integer DEFAULT 0 NOT NULL,
    CONSTRAINT tasks_priority_check CHECK ((priority = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'critical'::text]))),
    CONSTRAINT tasks_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'ready'::text, 'dispatching'::text, 'in_progress'::text, 'blocked'::text, 'halted'::text, 'automated_review'::text, 'awaiting_approval'::text, 'approved'::text, 'rejected'::text, 'done'::text, 'failed'::text, 'cancelled'::text, 'proposed'::text, 'accepted'::text]))),
    CONSTRAINT tasks_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleted'::text])))
);


--
-- Name: token_calls; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_calls (
    id bigint NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    goal_id text,
    agent_id text,
    session_key text,
    run_id text,
    provider text NOT NULL,
    model text NOT NULL,
    input_tokens integer DEFAULT 0 NOT NULL,
    output_tokens integer DEFAULT 0 NOT NULL,
    thinking_tokens integer DEFAULT 0 NOT NULL,
    cache_read_tokens integer DEFAULT 0 NOT NULL,
    cache_write_tokens integer DEFAULT 0 NOT NULL,
    record_type text DEFAULT 'call'::text NOT NULL,
    num_turns integer,
    story_id text
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    uid text NOT NULL,
    actor_id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    photo_url text DEFAULT ''::text NOT NULL,
    system_role text DEFAULT 'user'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    unread_notifications integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deactivated'::text]))),
    CONSTRAINT users_system_role_check CHECK ((system_role = ANY (ARRAY['admin'::text, 'editor'::text, 'member'::text])))
);


--
-- Name: mv_cost_by_goal; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_by_goal AS
 WITH token_records AS (
         SELECT t.recorded_at,
            t.agent_id,
            t.goal_id,
            t.story_id,
            t.run_id,
            regexp_replace(t.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text) AS provider,
            regexp_replace(regexp_replace(t.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text) AS model,
            t.input_tokens AS inp,
            t.output_tokens AS "out",
            t.thinking_tokens AS thinking,
            t.cache_read_tokens AS cr,
            t.cache_write_tokens AS cw,
            (((t.input_tokens + t.output_tokens) + t.cache_read_tokens) + t.cache_write_tokens) AS total_tokens
           FROM public.token_calls t
          WHERE ((t.recorded_at >= (now() - '30 days'::interval)) AND (t.record_type = 'call'::text) AND (t.goal_id IS NOT NULL))
        ), priced AS (
         SELECT r.recorded_at,
            r.agent_id,
            r.goal_id,
            r.story_id,
            r.run_id,
            r.provider,
            r.model,
            r.inp,
            r."out",
            r.thinking,
            r.cr,
            r.cw,
            r.total_tokens,
                CASE
                    WHEN (p_1.id IS NOT NULL) THEN (((((((r.inp)::numeric * p_1.input_rate) + (((r."out" - r.thinking))::numeric * p_1.output_rate)) + ((r.thinking)::numeric * COALESCE(p_1.thinking_rate, p_1.output_rate))) + ((r.cr)::numeric * p_1.cache_read_rate)) + ((r.cw)::numeric * p_1.cache_write_rate)) / 1000000.0)
                    ELSE NULL::numeric
                END AS cost
           FROM (token_records r
             LEFT JOIN LATERAL ( SELECT model_pricing.id,
                    model_pricing.provider,
                    model_pricing.model,
                    model_pricing.provider_label,
                    model_pricing.notes,
                    model_pricing.input_rate,
                    model_pricing.output_rate,
                    model_pricing.cache_read_rate,
                    model_pricing.cache_write_rate,
                    model_pricing.thinking_rate,
                    model_pricing.effective_from,
                    model_pricing.created_at
                   FROM public.model_pricing
                  WHERE ((model_pricing.provider = r.provider) AND (model_pricing.model = r.model) AND (model_pricing.effective_from <= r.recorded_at))
                  ORDER BY model_pricing.effective_from DESC
                 LIMIT 1) p_1 ON (true))
        ), goal_totals AS (
         SELECT upper(p_1.goal_id) AS goal_id,
            sum(p_1.cost) AS goal_cost
           FROM priced p_1
          GROUP BY (upper(p_1.goal_id))
        ), ranked_goals AS (
         SELECT goal_totals.goal_id,
            rank() OVER (ORDER BY goal_totals.goal_cost DESC NULLS LAST) AS goal_rank
           FROM goal_totals
        )
 SELECT row_number() OVER (ORDER BY rg.goal_rank, (sum(p.cost)) DESC NULLS LAST) AS row_num,
    rg.goal_id,
    g.name AS title,
    COALESCE(a.name, a2.name, assignee.display_name,
        CASE
            WHEN (p.agent_id ~ '^(ra|sec)-.+-[0-9a-f]{8}$'::text) THEN regexp_replace(p.agent_id, '-[0-9a-f]{8}$'::text, ''::text)
            ELSE NULL::text
        END, NULLIF(p.agent_id, ''::text), ((p.provider || '/'::text) || p.model)) AS agent_id,
        CASE
            WHEN ((assignee.display_name IS NOT NULL) OR (ph.phase IS NOT NULL)) THEN COALESCE(ph.phase, 'Other work'::text)
            ELSE NULL::text
        END AS phase,
    p.provider,
    p.model,
    sum(p.total_tokens) AS tokens,
    sum(p.cost) AS cost
   FROM ((((((((priced p
     JOIN ranked_goals rg ON ((rg.goal_id = upper(p.goal_id))))
     JOIN public.goals g ON ((g.id = rg.goal_id)))
     LEFT JOIN public.agent_instances ai ON ((((ai.id)::text = p.agent_id) OR (ai.gateway_agent_id = p.agent_id))))
     LEFT JOIN public.agents a ON ((a.id = ai.template_id)))
     LEFT JOIN public.agents a2 ON ((a2.id = regexp_replace(p.agent_id, (('-'::text || lower(p.goal_id)) || '-[0-9a-f-]+$'::text), ''::text))))
     LEFT JOIN public.tasks ph ON ((ph.id = p.run_id)))
     LEFT JOIN public.stories s3 ON ((s3.id = COALESCE(p.story_id, ph.story_id))))
     LEFT JOIN public.users assignee ON ((assignee.uid = s3.assigned_to)))
  WHERE (rg.goal_rank <= 25)
  GROUP BY rg.goal_id, rg.goal_rank, g.name, COALESCE(a.name, a2.name, assignee.display_name,
        CASE
            WHEN (p.agent_id ~ '^(ra|sec)-.+-[0-9a-f]{8}$'::text) THEN regexp_replace(p.agent_id, '-[0-9a-f]{8}$'::text, ''::text)
            ELSE NULL::text
        END, NULLIF(p.agent_id, ''::text), ((p.provider || '/'::text) || p.model)),
        CASE
            WHEN ((assignee.display_name IS NOT NULL) OR (ph.phase IS NOT NULL)) THEN COALESCE(ph.phase, 'Other work'::text)
            ELSE NULL::text
        END, p.provider, p.model
  ORDER BY rg.goal_rank, (sum(p.cost)) DESC NULLS LAST
  WITH NO DATA;


--
-- Name: mv_token_calls_priced; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_token_calls_priced AS
 SELECT row_number() OVER (ORDER BY t.recorded_at, t.session_key, t.agent_id) AS row_num,
    t.recorded_at,
    t.agent_id,
    t.goal_id,
    t.session_key,
    regexp_replace(t.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text) AS provider,
    regexp_replace(regexp_replace(t.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text) AS model,
    t.input_tokens AS inp,
    t.output_tokens AS "out",
    t.thinking_tokens AS thinking,
    t.cache_read_tokens AS cr,
    t.cache_write_tokens AS cw,
    (((t.input_tokens + t.output_tokens) + t.cache_read_tokens) + t.cache_write_tokens) AS total_tokens,
        CASE
            WHEN (p.id IS NOT NULL) THEN (((((((t.input_tokens)::numeric * p.input_rate) + (((t.output_tokens - t.thinking_tokens))::numeric * p.output_rate)) + ((t.thinking_tokens)::numeric * COALESCE(p.thinking_rate, p.output_rate))) + ((t.cache_read_tokens)::numeric * p.cache_read_rate)) + ((t.cache_write_tokens)::numeric * p.cache_write_rate)) / 1000000.0)
            ELSE NULL::numeric
        END AS cost
   FROM (public.token_calls t
     LEFT JOIN LATERAL ( SELECT model_pricing.id,
            model_pricing.provider,
            model_pricing.model,
            model_pricing.provider_label,
            model_pricing.notes,
            model_pricing.input_rate,
            model_pricing.output_rate,
            model_pricing.cache_read_rate,
            model_pricing.cache_write_rate,
            model_pricing.thinking_rate,
            model_pricing.effective_from,
            model_pricing.created_at
           FROM public.model_pricing
          WHERE ((model_pricing.provider = regexp_replace(t.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text)) AND (model_pricing.model = regexp_replace(regexp_replace(t.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text)) AND (model_pricing.effective_from <= t.recorded_at))
          ORDER BY model_pricing.effective_from DESC
         LIMIT 1) p ON (true))
  WHERE ((t.recorded_at >= (now() - '30 days'::interval)) AND (t.record_type = 'call'::text))
  WITH NO DATA;


--
-- Name: mv_cost_by_model; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_by_model AS
 SELECT COALESCE(provider, ''::text) AS provider,
    COALESCE(model, ''::text) AS model,
    sum(total_tokens) AS tokens,
    sum(inp) AS inp,
    sum("out") AS "out",
    sum(thinking) AS thinking,
    sum(cr) AS cr,
    sum(cw) AS cw,
    sum(cost) AS cost
   FROM public.mv_token_calls_priced
  GROUP BY COALESCE(provider, ''::text), COALESCE(model, ''::text)
  ORDER BY (sum(cost)) DESC NULLS LAST
  WITH NO DATA;


--
-- Name: mv_cost_by_story; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_by_story AS
 SELECT row_number() OVER (ORDER BY (sum(cost)) DESC NULLS LAST) AS row_num,
    story_id,
    story_title,
    sum(total_tokens) AS tokens,
    sum(cost) AS cost
   FROM ( SELECT tc.story_id,
            s.title AS story_title,
            (((tc.input_tokens + tc.output_tokens) + tc.cache_read_tokens) + tc.cache_write_tokens) AS total_tokens,
                CASE
                    WHEN (mp.id IS NOT NULL) THEN (((((((tc.input_tokens)::numeric * mp.input_rate) + (((tc.output_tokens - tc.thinking_tokens))::numeric * mp.output_rate)) + ((tc.thinking_tokens)::numeric * COALESCE(mp.thinking_rate, mp.output_rate))) + ((tc.cache_read_tokens)::numeric * mp.cache_read_rate)) + ((tc.cache_write_tokens)::numeric * mp.cache_write_rate)) / 1000000.0)
                    ELSE NULL::numeric
                END AS cost
           FROM ((public.token_calls tc
             JOIN public.stories s ON ((s.id = tc.story_id)))
             LEFT JOIN LATERAL ( SELECT model_pricing.id,
                    model_pricing.provider,
                    model_pricing.model,
                    model_pricing.provider_label,
                    model_pricing.notes,
                    model_pricing.input_rate,
                    model_pricing.output_rate,
                    model_pricing.cache_read_rate,
                    model_pricing.cache_write_rate,
                    model_pricing.thinking_rate,
                    model_pricing.effective_from,
                    model_pricing.created_at
                   FROM public.model_pricing
                  WHERE ((model_pricing.provider = regexp_replace(tc.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text)) AND (model_pricing.model = regexp_replace(regexp_replace(tc.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text)) AND (model_pricing.effective_from <= tc.recorded_at))
                  ORDER BY model_pricing.effective_from DESC
                 LIMIT 1) mp ON (true))
          WHERE ((tc.story_id IS NOT NULL) AND (tc.record_type = 'call'::text) AND (tc.recorded_at >= (now() - '30 days'::interval)))) priced
  GROUP BY story_id, story_title
  ORDER BY (sum(cost)) DESC NULLS LAST
 LIMIT 10
  WITH NO DATA;


--
-- Name: mv_cost_by_task; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_by_task AS
 SELECT row_number() OVER (ORDER BY (sum(p.cost)) DESC NULLS LAST) AS row_num,
    tk.id AS task_id,
    tk.title AS task_title,
    s.title AS story_title,
    sum(p.total_tokens) AS tokens,
    sum(p.cost) AS cost
   FROM (((public.mv_token_calls_priced p
     JOIN public.agent_instances ai ON ((((ai.id)::text = p.agent_id) OR (ai.gateway_agent_id = p.agent_id))))
     JOIN public.tasks tk ON ((tk.id = ai.task_id)))
     LEFT JOIN public.stories s ON ((s.id = tk.story_id)))
  GROUP BY tk.id, tk.title, s.title
  ORDER BY (sum(p.cost)) DESC NULLS LAST
 LIMIT 10
  WITH NO DATA;


--
-- Name: mv_cost_daily; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_daily AS
 SELECT ((recorded_at AT TIME ZONE 'UTC'::text))::date AS day,
    COALESCE(sum(cost), (0)::numeric) AS cost,
    sum(total_tokens) AS tokens
   FROM public.mv_token_calls_priced
  WHERE (recorded_at >= (now() - '30 days'::interval))
  GROUP BY (((recorded_at AT TIME ZONE 'UTC'::text))::date)
  ORDER BY (((recorded_at AT TIME ZONE 'UTC'::text))::date)
  WITH NO DATA;


--
-- Name: mv_cost_daily_provider; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_daily_provider AS
 SELECT ((recorded_at AT TIME ZONE 'UTC'::text))::date AS day,
    provider,
    COALESCE(sum(cost), (0)::numeric) AS cost
   FROM public.mv_token_calls_priced
  WHERE (recorded_at >= (now() - '30 days'::interval))
  GROUP BY (((recorded_at AT TIME ZONE 'UTC'::text))::date), provider
  ORDER BY (((recorded_at AT TIME ZONE 'UTC'::text))::date), provider
  WITH NO DATA;


--
-- Name: mv_cost_top_agents; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_top_agents AS
 WITH token_records AS (
         SELECT t.recorded_at,
            t.agent_id,
            t.goal_id,
            t.story_id,
            t.run_id,
            regexp_replace(t.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text) AS provider,
            regexp_replace(regexp_replace(t.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text) AS model,
            t.input_tokens AS inp,
            t.output_tokens AS "out",
            t.thinking_tokens AS thinking,
            t.cache_read_tokens AS cr,
            t.cache_write_tokens AS cw,
            (((t.input_tokens + t.output_tokens) + t.cache_read_tokens) + t.cache_write_tokens) AS total_tokens
           FROM public.token_calls t
          WHERE ((t.recorded_at >= (now() - '30 days'::interval)) AND (t.record_type = 'call'::text))
        ), priced AS (
         SELECT r.recorded_at,
            r.agent_id,
            r.goal_id,
            r.story_id,
            r.run_id,
            r.provider,
            r.model,
            r.inp,
            r."out",
            r.thinking,
            r.cr,
            r.cw,
            r.total_tokens,
                CASE
                    WHEN (p.id IS NOT NULL) THEN (((((((r.inp)::numeric * p.input_rate) + (((r."out" - r.thinking))::numeric * p.output_rate)) + ((r.thinking)::numeric * COALESCE(p.thinking_rate, p.output_rate))) + ((r.cr)::numeric * p.cache_read_rate)) + ((r.cw)::numeric * p.cache_write_rate)) / 1000000.0)
                    ELSE NULL::numeric
                END AS cost
           FROM (token_records r
             LEFT JOIN LATERAL ( SELECT model_pricing.id,
                    model_pricing.provider,
                    model_pricing.model,
                    model_pricing.provider_label,
                    model_pricing.notes,
                    model_pricing.input_rate,
                    model_pricing.output_rate,
                    model_pricing.cache_read_rate,
                    model_pricing.cache_write_rate,
                    model_pricing.thinking_rate,
                    model_pricing.effective_from,
                    model_pricing.created_at
                   FROM public.model_pricing
                  WHERE ((model_pricing.provider = r.provider) AND (model_pricing.model = r.model) AND (model_pricing.effective_from <= r.recorded_at))
                  ORDER BY model_pricing.effective_from DESC
                 LIMIT 1) p ON (true))
        ), labeled AS (
         SELECT p.total_tokens,
            p.cost,
            COALESCE(a.name, a2.name, assignee.display_name,
                CASE
                    WHEN (p.agent_id ~ '^(ra|sec)-.+-[0-9a-f]{8}$'::text) THEN regexp_replace(p.agent_id, '-[0-9a-f]{8}$'::text, ''::text)
                    ELSE NULL::text
                END, NULLIF(p.agent_id, ''::text), ((p.provider || '/'::text) || p.model)) AS agent_label,
                CASE
                    WHEN ((assignee.display_name IS NOT NULL) OR (ph.phase IS NOT NULL)) THEN COALESCE(ph.phase, 'Other work'::text)
                    ELSE NULL::text
                END AS phase,
            p.provider,
            p.model
           FROM ((((((priced p
             LEFT JOIN public.agent_instances ai ON ((((ai.id)::text = p.agent_id) OR (ai.gateway_agent_id = p.agent_id))))
             LEFT JOIN public.agents a ON ((a.id = ai.template_id)))
             LEFT JOIN public.agents a2 ON ((a2.id = regexp_replace(p.agent_id, (('-'::text || lower(p.goal_id)) || '-[0-9a-f-]+$'::text), ''::text))))
             LEFT JOIN public.tasks ph ON ((ph.id = p.run_id)))
             LEFT JOIN public.stories s3 ON ((s3.id = COALESCE(p.story_id, ph.story_id))))
             LEFT JOIN public.users assignee ON ((assignee.uid = s3.assigned_to)))
        ), agent_totals AS (
         SELECT labeled.agent_label,
            sum(labeled.cost) AS agent_cost
           FROM labeled
          GROUP BY labeled.agent_label
        ), ranked_agents AS (
         SELECT agent_totals.agent_label,
            rank() OVER (ORDER BY agent_totals.agent_cost DESC NULLS LAST) AS agent_rank
           FROM agent_totals
        )
 SELECT row_number() OVER (ORDER BY ra.agent_rank, (sum(l.cost)) DESC NULLS LAST) AS row_num,
    ra.agent_label,
    l.phase,
    l.provider,
    l.model,
    sum(l.total_tokens) AS tokens,
    sum(l.cost) AS cost
   FROM (labeled l
     JOIN ranked_agents ra ON ((ra.agent_label = l.agent_label)))
  WHERE (ra.agent_rank <= 20)
  GROUP BY ra.agent_label, ra.agent_rank, l.phase, l.provider, l.model
  ORDER BY ra.agent_rank, (sum(l.cost)) DESC NULLS LAST
  WITH NO DATA;


--
-- Name: mv_cost_top_tools; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_top_tools AS
 SELECT COALESCE((dimensions ->> 'tool'::text), '?'::text) AS tool,
    sum(value) AS calls
   FROM public.agent_metrics
  WHERE ((recorded_at >= (now() - '30 days'::interval)) AND (metric = 'session.tool'::text))
  GROUP BY COALESCE((dimensions ->> 'tool'::text), '?'::text)
  ORDER BY (sum(value)) DESC
 LIMIT 5
  WITH NO DATA;


--
-- Name: mv_cost_totals; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_cost_totals AS
 SELECT (1)::smallint AS row_id,
    COALESCE(sum(
        CASE
            WHEN (recorded_at >= (now() - '7 days'::interval)) THEN cost
            ELSE NULL::numeric
        END), (0)::numeric) AS week_cost,
    COALESCE(sum(
        CASE
            WHEN ((recorded_at)::date = CURRENT_DATE) THEN cost
            ELSE NULL::numeric
        END), (0)::numeric) AS today_cost
   FROM public.mv_token_calls_priced
  WITH NO DATA;


--
-- Name: mv_dashboard_approval_sla_goals; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_approval_sla_goals AS
 WITH approval_entry AS (
         SELECT le.context_task_id AS task_id,
            max(le."timestamp") AS entered_at
           FROM public.ledger_events le
          WHERE ((le.event_type = 'task.status_changed'::text) AND ((le.payload ->> 'new_status'::text) = 'awaiting_approval'::text))
          GROUP BY le.context_task_id
        )
 SELECT t.goal_id,
    g.name AS goal_name,
    count(*) AS waiting,
    (round(max((EXTRACT(epoch FROM (now() - ae.entered_at)) / 3600.0))))::integer AS oldest_h
   FROM ((public.tasks t
     JOIN public.goals g ON ((g.id = t.goal_id)))
     LEFT JOIN approval_entry ae ON ((ae.task_id = t.id)))
  WHERE ((t.status = 'active'::text) AND (t.state = 'awaiting_approval'::text) AND (g.status = 'active'::text))
  GROUP BY t.goal_id, g.name
  ORDER BY ((round(max((EXTRACT(epoch FROM (now() - ae.entered_at)) / 3600.0))))::integer) DESC NULLS LAST
 LIMIT 6
  WITH NO DATA;


--
-- Name: mv_dashboard_approval_sla_summary; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_approval_sla_summary AS
 WITH approval_entry AS (
         SELECT le.context_task_id AS task_id,
            max(le."timestamp") AS entered_at
           FROM public.ledger_events le
          WHERE ((le.event_type = 'task.status_changed'::text) AND ((le.payload ->> 'new_status'::text) = 'awaiting_approval'::text))
          GROUP BY le.context_task_id
        )
 SELECT (1)::smallint AS row_id,
    count(*) AS waiting,
    round(avg((EXTRACT(epoch FROM (now() - ae.entered_at)) / 3600.0)), 1) AS avg_h,
    (round(max((EXTRACT(epoch FROM (now() - ae.entered_at)) / 3600.0))))::integer AS max_h
   FROM ((public.tasks t
     JOIN public.goals g ON ((g.id = t.goal_id)))
     LEFT JOIN approval_entry ae ON ((ae.task_id = t.id)))
  WHERE ((t.status = 'active'::text) AND (t.state = 'awaiting_approval'::text) AND (g.status = 'active'::text))
  WITH NO DATA;


--
-- Name: platform_channel_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_channel_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    goal_id text NOT NULL,
    integration_id uuid NOT NULL,
    actor_id uuid,
    external_channel_id text,
    resource_kind text,
    thread_id text,
    agent_id text,
    current_story_id text,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: platform_integrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_integrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    platform text NOT NULL,
    workspace_id text,
    credentials_kind text DEFAULT 'api_key'::text NOT NULL,
    credentials_ref text,
    bot_external_id text,
    bot_actor_id uuid,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    redirect_uri text,
    credentials_data text,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    credentials_json jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: mv_dashboard_channels; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_channels AS
 SELECT pi.id AS integration_id,
    pi.platform,
    pi.status,
    pi.workspace_id,
    pi.updated_at,
    count(DISTINCT gpi.goal_id) FILTER (WHERE ((gpi.enabled = true) AND (g.status = 'active'::text))) AS connected_goals,
    count(DISTINCT pcb.id) FILTER (WHERE ((pcb.archived_at IS NULL) AND (g.status = 'active'::text))) AS active_bindings,
    max(pcb.updated_at) FILTER (WHERE (g.status = 'active'::text)) AS last_activity
   FROM (((public.platform_integrations pi
     LEFT JOIN public.goal_platform_integrations gpi ON ((gpi.integration_id = pi.id)))
     LEFT JOIN public.goals g ON ((g.id = gpi.goal_id)))
     LEFT JOIN public.platform_channel_bindings pcb ON (((pcb.integration_id = pi.id) AND (pcb.goal_id = gpi.goal_id))))
  WHERE (pi.status = 'active'::text)
  GROUP BY pi.id, pi.platform, pi.status, pi.workspace_id, pi.updated_at
  ORDER BY pi.platform
  WITH NO DATA;


--
-- Name: mv_dashboard_escalations; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_escalations AS
 SELECT le.context_goal_id AS goal_id,
    g.name AS goal_name,
    count(*) FILTER (WHERE (le.event_type = 'esc.requested'::text)) AS total,
    count(*) FILTER (WHERE (le.event_type = 'esc.timed_out'::text)) AS timed_out,
    count(*) FILTER (WHERE (le.event_type = 'esc.resolved'::text)) AS resolved
   FROM (public.ledger_events le
     LEFT JOIN public.goals g ON ((g.id = le.context_goal_id)))
  WHERE ((le.event_type = ANY (ARRAY['esc.requested'::text, 'esc.resolved'::text, 'esc.timed_out'::text])) AND (le."timestamp" > (now() - '30 days'::interval)) AND (le.context_goal_id <> ''::text) AND (g.status = 'active'::text))
  GROUP BY le.context_goal_id, g.name
  ORDER BY (count(*) FILTER (WHERE (le.event_type = 'esc.requested'::text))) DESC
 LIMIT 6
  WITH NO DATA;


--
-- Name: task_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_reviews (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    task_id text NOT NULL,
    reviewer_agent_id text NOT NULL,
    review_task_id text,
    verdict text NOT NULL,
    quality_score integer,
    findings text[] DEFAULT '{}'::text[] NOT NULL,
    recommendations text[] DEFAULT '{}'::text[] NOT NULL,
    updated_spec text,
    summary text,
    full_content text,
    reviewed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_reviews_quality_score_check CHECK (((quality_score >= 0) AND (quality_score <= 100))),
    CONSTRAINT task_reviews_verdict_check CHECK ((verdict = ANY (ARRAY['approved'::text, 'changes_required'::text, 'rejected'::text])))
);


--
-- Name: mv_dashboard_review_quality; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_review_quality AS
 SELECT tr.reviewer_agent_id,
    count(*) AS reviews,
    count(*) FILTER (WHERE (tr.verdict = 'approved'::text)) AS approved,
    round(avg(tr.quality_score)) AS avg_score
   FROM ((public.task_reviews tr
     JOIN public.tasks t ON ((t.id = tr.task_id)))
     JOIN public.goals g ON (((g.id = t.goal_id) AND (g.status = 'active'::text))))
  WHERE (tr.reviewed_at > (now() - '30 days'::interval))
  GROUP BY tr.reviewer_agent_id
  ORDER BY (count(*)) DESC
 LIMIT 8
  WITH NO DATA;


--
-- Name: mv_dashboard_slos; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_dashboard_slos AS
 WITH completion AS (
         SELECT count(*) FILTER (WHERE (t.state = ANY (ARRAY['done'::text, 'approved'::text]))) AS completed,
            count(*) FILTER (WHERE (t.state = 'failed'::text)) AS failed
           FROM (public.tasks t
             JOIN public.goals g ON ((g.id = t.goal_id)))
          WHERE ((t.status = 'active'::text) AND (g.status = 'active'::text) AND (t.completed_at > (now() - '30 days'::interval)))
        ), escalation AS (
         SELECT count(*) AS escalated
           FROM public.ledger_events
          WHERE ((ledger_events.event_type = 'esc.requested'::text) AND (ledger_events."timestamp" > (now() - '30 days'::interval)))
        ), recovery AS (
         SELECT count(DISTINCT ledger_events.context_task_id) FILTER (WHERE (ledger_events.event_type = 'iter.started'::text)) AS attempted,
            count(DISTINCT ledger_events.context_task_id) FILTER (WHERE (ledger_events.event_type = 'iter.exhausted'::text)) AS exhausted
           FROM public.ledger_events
          WHERE ((ledger_events.event_type = ANY (ARRAY['iter.started'::text, 'iter.exhausted'::text])) AND (ledger_events."timestamp" > (now() - '30 days'::interval)) AND (ledger_events.context_task_id <> ''::text))
        ), duration AS (
         SELECT round(avg((EXTRACT(epoch FROM (tasks.completed_at - tasks.started_at)) / 3600.0)), 1) AS avg_h,
            percentile_cont((0.95)::double precision) WITHIN GROUP (ORDER BY (((EXTRACT(epoch FROM (tasks.completed_at - tasks.started_at)) / 3600.0))::double precision)) AS p95_h
           FROM public.tasks
          WHERE ((tasks.status = 'active'::text) AND (tasks.state = ANY (ARRAY['done'::text, 'approved'::text])) AND (tasks.started_at IS NOT NULL) AND (tasks.completed_at IS NOT NULL) AND (tasks.completed_at > (now() - '30 days'::interval)))
        )
 SELECT (1)::smallint AS row_id,
    c.completed,
    c.failed,
    e.escalated,
    r.attempted,
    r.exhausted,
    d.avg_h,
    d.p95_h
   FROM completion c,
    escalation e,
    recovery r,
    duration d
  WITH NO DATA;


--
-- Name: mv_taximeter_human_activity; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW public.mv_taximeter_human_activity AS
 WITH story_completions AS (
         SELECT (le.payload ->> 'actor_uid'::text) AS uid,
            s.goal_id,
            le."timestamp",
            s.priority,
            s.started_at,
            s.completed_at
           FROM (public.ledger_events le
             JOIN public.stories s ON ((s.id = (le.payload ->> 'story_id'::text))))
          WHERE ((le.event_type = 'story.state_changed'::text) AND ((le.payload ->> 'action'::text) = ANY (ARRAY['done'::text, 'mark-done'::text])) AND ((le.payload ->> 'actor_uid'::text) IS NOT NULL))
        ), cost_story_attribution AS (
         SELECT s.assigned_to AS uid,
            s.goal_id,
            tc.recorded_at,
            s.id AS story_id,
                CASE
                    WHEN (mp.id IS NOT NULL) THEN (((((((tc.input_tokens)::numeric * mp.input_rate) + (((tc.output_tokens - tc.thinking_tokens))::numeric * mp.output_rate)) + ((tc.thinking_tokens)::numeric * COALESCE(mp.thinking_rate, mp.output_rate))) + ((tc.cache_read_tokens)::numeric * mp.cache_read_rate)) + ((tc.cache_write_tokens)::numeric * mp.cache_write_rate)) / 1000000.0)
                    ELSE NULL::numeric
                END AS cost
           FROM ((public.token_calls tc
             JOIN public.stories s ON ((s.id = tc.story_id)))
             LEFT JOIN LATERAL ( SELECT model_pricing.id,
                    model_pricing.provider,
                    model_pricing.model,
                    model_pricing.provider_label,
                    model_pricing.notes,
                    model_pricing.input_rate,
                    model_pricing.output_rate,
                    model_pricing.cache_read_rate,
                    model_pricing.cache_write_rate,
                    model_pricing.thinking_rate,
                    model_pricing.effective_from,
                    model_pricing.created_at
                   FROM public.model_pricing
                  WHERE ((model_pricing.provider = regexp_replace(tc.provider, '-[^-]+-[0-9]+[km]$'::text, ''::text)) AND (model_pricing.model = regexp_replace(regexp_replace(tc.model, '-[0-9]+[km]$'::text, ''::text), '-[0-9]{8}$'::text, ''::text)) AND (model_pricing.effective_from <= tc.recorded_at))
                  ORDER BY model_pricing.effective_from DESC
                 LIMIT 1) mp ON (true))
          WHERE ((tc.record_type = 'call'::text) AND (s.assigned_to IS NOT NULL) AND (s.assigned_to <> ''::text))
        )
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'decisions_made'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'decision.recorded'::text) AND (le.actor_type = 'human'::text))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'task_review_actions'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'task.status_changed'::text) AND (le.actor_type = 'human'::text) AND ((le.payload ->> 'new_status'::text) = ANY (ARRAY['approved'::text, 'rejected'::text])))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'escalations_resolved'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'esc.resolved'::text) AND (le.actor_type = 'human'::text))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'goal_management_actions'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = ANY (ARRAY['goal.created'::text, 'goal.member_added'::text, 'goal.member_removed'::text, 'goal.member_role_changed'::text])) AND (le.actor_type = 'human'::text))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'questions_answered'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'comms.question.answered'::text) AND (le.actor_type = 'human'::text))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'story_assignment_requests'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'story.assignment_changed'::text) AND (le.actor_type = 'human'::text))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    g.id AS goal_id,
    'go_thread_interactions'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM ((public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
     JOIN public.goals g ON (((g.id = le.context_goal_id) AND (g.go_thread_id = le.context_conversation_id))))
  WHERE ((le.event_type = 'comms.message.posted'::text) AND (le.actor_type = 'human'::text) AND (EXISTS ( SELECT 1
           FROM public.ledger_events outcome
          WHERE ((outcome.context_goal_id = le.context_goal_id) AND (outcome.event_type = ANY (ARRAY['story.created'::text, 'story.state_changed'::text, 'story.assignment_changed'::text, 'go.task_created'::text, 'go.task_reassigned'::text])) AND (outcome."timestamp" > le."timestamp") AND (outcome."timestamp" <= (le."timestamp" + '00:04:00'::interval))))))
  GROUP BY u.uid, g.id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT u.uid,
    le.context_goal_id AS goal_id,
    'story_thread_interactions'::text AS metric_key,
    (date_trunc('week'::text, le."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM (public.ledger_events le
     JOIN public.users u ON ((u.actor_id = le.actor_id)))
  WHERE ((le.event_type = 'comms.message.posted'::text) AND (le.actor_type = 'human'::text) AND (NOT (EXISTS ( SELECT 1
           FROM public.goals g2
          WHERE ((g2.id = le.context_goal_id) AND (g2.go_thread_id = le.context_conversation_id))))))
  GROUP BY u.uid, le.context_goal_id, (date_trunc('week'::text, le."timestamp"))
UNION ALL
 SELECT story_completions.uid,
    story_completions.goal_id,
    'weighted_output_completed'::text AS metric_key,
    (date_trunc('week'::text, story_completions."timestamp"))::date AS week_start,
    (sum((
        CASE lower(TRIM(BOTH FROM COALESCE(story_completions.priority, ''::text)))
            WHEN 'low'::text THEN 1
            WHEN 'medium'::text THEN 3
            WHEN 'high'::text THEN 5
            WHEN 'critical'::text THEN 8
            ELSE 3
        END +
        CASE
            WHEN ((story_completions.started_at IS NULL) OR (story_completions.completed_at IS NULL)) THEN 1
            WHEN ((GREATEST(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at)), (0)::numeric) / 86400.0) < (1)::numeric) THEN 1
            WHEN ((GREATEST(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at)), (0)::numeric) / 86400.0) <= (2)::numeric) THEN 2
            WHEN ((GREATEST(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at)), (0)::numeric) / 86400.0) <= (5)::numeric) THEN 3
            WHEN ((GREATEST(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at)), (0)::numeric) / 86400.0) <= (14)::numeric) THEN 5
            WHEN ((GREATEST(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at)), (0)::numeric) / 86400.0) <= (28)::numeric) THEN 8
            ELSE 13
        END)))::numeric AS value
   FROM story_completions
  GROUP BY story_completions.uid, story_completions.goal_id, (date_trunc('week'::text, story_completions."timestamp"))
UNION ALL
 SELECT story_completions.uid,
    story_completions.goal_id,
    'story_completion_time_sum'::text AS metric_key,
    (date_trunc('week'::text, story_completions."timestamp"))::date AS week_start,
    sum(EXTRACT(epoch FROM (story_completions.completed_at - story_completions.started_at))) AS value
   FROM story_completions
  WHERE ((story_completions.started_at IS NOT NULL) AND (story_completions.completed_at IS NOT NULL))
  GROUP BY story_completions.uid, story_completions.goal_id, (date_trunc('week'::text, story_completions."timestamp"))
UNION ALL
 SELECT story_completions.uid,
    story_completions.goal_id,
    'story_completion_time_count'::text AS metric_key,
    (date_trunc('week'::text, story_completions."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM story_completions
  WHERE ((story_completions.started_at IS NOT NULL) AND (story_completions.completed_at IS NOT NULL))
  GROUP BY story_completions.uid, story_completions.goal_id, (date_trunc('week'::text, story_completions."timestamp"))
UNION ALL
 SELECT story_completions.uid,
    story_completions.goal_id,
    'stories_completed'::text AS metric_key,
    (date_trunc('week'::text, story_completions."timestamp"))::date AS week_start,
    (count(*))::numeric AS value
   FROM story_completions
  GROUP BY story_completions.uid, story_completions.goal_id, (date_trunc('week'::text, story_completions."timestamp"))
UNION ALL
 SELECT cost_story_attribution.uid,
    cost_story_attribution.goal_id,
    'cost_per_story_sum'::text AS metric_key,
    (date_trunc('week'::text, cost_story_attribution.recorded_at))::date AS week_start,
    sum(COALESCE(cost_story_attribution.cost, (0)::numeric)) AS value
   FROM cost_story_attribution
  GROUP BY cost_story_attribution.uid, cost_story_attribution.goal_id, (date_trunc('week'::text, cost_story_attribution.recorded_at))
UNION ALL
 SELECT cost_story_attribution.uid,
    cost_story_attribution.goal_id,
    'cost_per_story_count'::text AS metric_key,
    (date_trunc('week'::text, cost_story_attribution.recorded_at))::date AS week_start,
    (count(DISTINCT cost_story_attribution.story_id))::numeric AS value
   FROM cost_story_attribution
  GROUP BY cost_story_attribution.uid, cost_story_attribution.goal_id, (date_trunc('week'::text, cost_story_attribution.recorded_at))
  WITH NO DATA;


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id text NOT NULL,
    uid text NOT NULL,
    type text NOT NULL,
    title text NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    goal_id text DEFAULT ''::text NOT NULL,
    agent_id text DEFAULT ''::text NOT NULL,
    thread_id text DEFAULT ''::text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    link text DEFAULT ''::text NOT NULL,
    read boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: openapi_specs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.openapi_specs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    spec_url text,
    spec_content jsonb NOT NULL,
    base_url text,
    credential_id uuid,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: plan_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.plan_versions (
    id uuid NOT NULL,
    story_id text NOT NULL,
    version integer NOT NULL,
    parent_plan_id uuid,
    plan_doc_path text,
    task_dag jsonb,
    risk_level text,
    proposed_by text,
    claude_session_id text,
    branch_id uuid,
    reason text,
    status text DEFAULT 'proposed'::text NOT NULL,
    approved_at timestamp with time zone,
    approved_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: platform_discoveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_discoveries (
    id text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    url text DEFAULT ''::text NOT NULL,
    released_at text,
    source text DEFAULT ''::text NOT NULL,
    fingerprint text NOT NULL,
    status text DEFAULT 'new'::text NOT NULL,
    registry_finding_id text,
    discovered_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    queued_at timestamp with time zone DEFAULT now(),
    sweep_id text,
    CONSTRAINT platform_discoveries_category_check CHECK ((category = ANY (ARRAY['model'::text, 'tool'::text, 'skill'::text]))),
    CONSTRAINT platform_discoveries_status_check CHECK ((status = ANY (ARRAY['new'::text, 'researching'::text, 'researched'::text, 'reviewed'::text, 'failed'::text, 'dismissed'::text])))
);


--
-- Name: platform_processed_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_processed_events (
    platform text NOT NULL,
    external_event_id text NOT NULL,
    processed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: platform_recreate_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_recreate_state (
    id text DEFAULT 'singleton'::text NOT NULL,
    last_build_sha text DEFAULT ''::text NOT NULL,
    recreated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: platform_registry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_registry (
    id text NOT NULL,
    source text NOT NULL,
    category text NOT NULL,
    fingerprint text NOT NULL,
    target_role text DEFAULT 'all'::text NOT NULL,
    provider_family text,
    title text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    impact jsonb DEFAULT '{}'::jsonb NOT NULL,
    finding_status text DEFAULT 'new'::text NOT NULL,
    verdict text DEFAULT 'pending'::text NOT NULL,
    adoption_path text DEFAULT ''::text NOT NULL,
    supported_today boolean,
    reviewer_notes text DEFAULT ''::text NOT NULL,
    reviewed_at timestamp with time zone,
    impl_status text DEFAULT 'none'::text NOT NULL,
    story_id text,
    source_agent_id text,
    run_id text,
    branch text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    op_type text,
    op_params jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT platform_registry_category_check CHECK ((category = ANY (ARRAY['model'::text, 'tool'::text, 'mcp'::text, 'skill'::text]))),
    CONSTRAINT platform_registry_finding_status_check CHECK ((finding_status = ANY (ARRAY['new'::text, 'updated'::text, 'stale'::text]))),
    CONSTRAINT platform_registry_impl_status_check CHECK ((impl_status = ANY (ARRAY['none'::text, 'story_created'::text, 'implemented'::text]))),
    CONSTRAINT platform_registry_op_type_check CHECK (((op_type IS NULL) OR (op_type = ANY (ARRAY['change_template_model'::text, 'add_skill'::text, 'add_tool'::text, 'create_credential'::text])))),
    CONSTRAINT platform_registry_source_check CHECK ((source = ANY (ARRAY['model_research'::text, 'tool_research'::text, 'skill_research'::text]))),
    CONSTRAINT platform_registry_target_role_check CHECK ((target_role = ANY (ARRAY['GO'::text, 'SHCA'::text, 'IA'::text, 'RA'::text, 'council'::text, 'all'::text]))),
    CONSTRAINT platform_registry_verdict_check CHECK ((verdict = ANY (ARRAY['pending'::text, 'adopt_now'::text, 'adopt_with_build'::text, 'not_applicable'::text, 'rejected'::text, 'out_of_scope'::text])))
);


--
-- Name: platform_research_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_research_findings (
    id text NOT NULL,
    dimension text NOT NULL,
    question text DEFAULT ''::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    worker_agent_id text,
    branch text DEFAULT ''::text NOT NULL,
    supersedes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    run_id text
);


--
-- Name: platform_research_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_research_runs (
    id text NOT NULL,
    trigger text DEFAULT 'manual'::text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    dimensions text[] DEFAULT '{}'::text[] NOT NULL,
    current_index integer DEFAULT 0 NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT platform_research_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'reporting'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: platform_schedule_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_schedule_state (
    id text DEFAULT 'singleton'::text NOT NULL,
    last_discovery_at timestamp with time zone,
    last_deepdive_at timestamp with time zone,
    discovery_enabled boolean
);


--
-- Name: platform_suggestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_suggestions (
    id text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    rationale text DEFAULT ''::text NOT NULL,
    evidence jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'proposed'::text NOT NULL,
    proposed_by text DEFAULT 'platform-convener'::text NOT NULL,
    decided_by text,
    repo_ref text,
    finding_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_suggestions_status_check CHECK ((status = ANY (ARRAY['proposed'::text, 'accepted'::text, 'in_progress'::text, 'implemented'::text, 'rejected'::text, 'superseded'::text])))
);


--
-- Name: platform_sweeps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_sweeps (
    id text NOT NULL,
    trigger text DEFAULT 'manual'::text NOT NULL,
    status text DEFAULT 'researching'::text NOT NULL,
    kinds text[] DEFAULT '{}'::text[] NOT NULL,
    producers_done text[] DEFAULT '{}'::text[] NOT NULL,
    question text DEFAULT ''::text NOT NULL,
    thread_id text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    nudges integer DEFAULT 0 NOT NULL,
    CONSTRAINT platform_sweeps_status_check CHECK ((status = ANY (ARRAY['researching'::text, 'reviewing'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: platform_user_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_user_map (
    platform text NOT NULL,
    external_user_id text NOT NULL,
    actor_id uuid,
    display_name text,
    email text,
    mapping_status text DEFAULT 'confirmed'::text NOT NULL,
    oauth_token_ref text,
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_access; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_access (
    resource_id text NOT NULL,
    agent_id text NOT NULL,
    goal_id text NOT NULL,
    access_level text DEFAULT 'read'::text NOT NULL,
    constraints jsonb DEFAULT '{}'::jsonb NOT NULL,
    granted_by text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT resource_access_access_level_check CHECK ((access_level = ANY (ARRAY['none'::text, 'read'::text, 'read-write'::text])))
);


--
-- Name: resource_activity_signal_notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_activity_signal_notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    signal_id uuid NOT NULL,
    actor_id text NOT NULL,
    notified_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: resource_activity_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_activity_signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    repo_goal_resource_id text NOT NULL,
    target_branch_name text NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    activity_type text NOT NULL,
    assigned_to text,
    status text DEFAULT 'in_progress'::text NOT NULL,
    metadata jsonb,
    notified_ids text[],
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    abandoned_at timestamp with time zone,
    CONSTRAINT resource_activity_signals_activity_type_check CHECK ((activity_type = ANY (ARRAY['integrating'::text, 'editing'::text, 'reviewing'::text, 'deploying'::text]))),
    CONSTRAINT resource_activity_signals_actor_type_check CHECK ((actor_type = ANY (ARRAY['story'::text, 'task'::text, 'goal'::text, 'human'::text, 'system'::text]))),
    CONSTRAINT resource_activity_signals_status_check CHECK ((status = ANY (ARRAY['in_progress'::text, 'completed'::text, 'abandoned'::text])))
);


--
-- Name: resource_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_attachments (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    resource_id text NOT NULL,
    goal_id text NOT NULL,
    filename text NOT NULL,
    file_type text NOT NULL,
    file_size_bytes bigint DEFAULT 0,
    gcs_uri text NOT NULL,
    uploaded_by text NOT NULL,
    content_hash text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: resource_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_events (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    resource_id text NOT NULL,
    goal_id text NOT NULL,
    event_type text NOT NULL,
    actor text NOT NULL,
    details jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: resource_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_links (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    goal_resource_id text NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    role text NOT NULL,
    metadata jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    removed_at timestamp with time zone,
    CONSTRAINT resource_links_entity_type_check CHECK ((entity_type = ANY (ARRAY['goal'::text, 'story'::text, 'task'::text]))),
    CONSTRAINT resource_links_role_check CHECK ((role = ANY (ARRAY['trunk'::text, 'feature'::text, 'input'::text, 'output'::text, 'dependency'::text])))
);


--
-- Name: TABLE resource_links; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.resource_links IS 'DEPRECATED (2026-06-16): zero rows, zero writers. Its purpose (linking resources at any level) is already covered by goal_resources (goal scope), story_resources (story scope), and task_resource_deps (task scope). See docs/story-integration/REDESIGN.md §4.2.';


--
-- Name: resource_renders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_renders (
    resource_id text NOT NULL,
    rendered_html text DEFAULT ''::text NOT NULL,
    render_status text DEFAULT 'pending'::text NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    retry_count integer DEFAULT 0 NOT NULL,
    retry_after timestamp with time zone,
    CONSTRAINT resource_renders_render_status_check CHECK ((render_status = ANY (ARRAY['pending'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: resource_summaries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resource_summaries (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    resource_id text NOT NULL,
    goal_id text NOT NULL,
    summary_type text DEFAULT 'full'::text NOT NULL,
    content text NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    summary_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    previous_scan_sha text DEFAULT ''::text,
    scan_duration_ms integer DEFAULT 0,
    token_count integer DEFAULT 0,
    scan_config jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.resources (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    goal_id text NOT NULL,
    name text NOT NULL,
    type text NOT NULL,
    uri text DEFAULT ''::text NOT NULL,
    default_access text DEFAULT 'read'::text NOT NULL,
    credentials_secret text DEFAULT ''::text NOT NULL,
    local_path text DEFAULT ''::text NOT NULL,
    protected_branches text[] DEFAULT '{}'::text[] NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    added_by text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    gcp_project text DEFAULT ''::text NOT NULL,
    dataset text DEFAULT ''::text NOT NULL,
    bucket text DEFAULT ''::text NOT NULL,
    path_prefix text DEFAULT ''::text NOT NULL,
    CONSTRAINT resources_default_access_check CHECK ((default_access = ANY (ARRAY['none'::text, 'read'::text, 'read-write'::text]))),
    CONSTRAINT resources_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deleted'::text])))
);


--
-- Name: security_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_findings (
    id text NOT NULL,
    run_id text NOT NULL,
    aspect text NOT NULL,
    severity text DEFAULT 'info'::text NOT NULL,
    file text DEFAULT ''::text NOT NULL,
    line integer,
    rule text DEFAULT ''::text NOT NULL,
    tool text DEFAULT ''::text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'open'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT security_findings_severity_check CHECK ((severity = ANY (ARRAY['critical'::text, 'high'::text, 'medium'::text, 'low'::text, 'info'::text])))
);


--
-- Name: security_review_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_review_runs (
    id text NOT NULL,
    repo_owner text DEFAULT ''::text NOT NULL,
    repo_name text DEFAULT ''::text NOT NULL,
    branch_name text DEFAULT ''::text NOT NULL,
    clone_url text DEFAULT ''::text NOT NULL,
    head_sha text DEFAULT ''::text NOT NULL,
    resource_id text,
    trigger text DEFAULT 'manual'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    started_at timestamp with time zone,
    CONSTRAINT security_review_runs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'running'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: security_review_skips; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_review_skips (
    id text NOT NULL,
    clone_url text NOT NULL,
    branch_name text NOT NULL,
    head_sha text NOT NULL,
    reviewed_run_id text,
    skip_count integer DEFAULT 1 NOT NULL,
    first_skipped_at timestamp with time zone DEFAULT now() NOT NULL,
    last_skipped_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: security_review_stages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_review_stages (
    id text NOT NULL,
    run_id text NOT NULL,
    aspect text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    depends_on text[] DEFAULT '{}'::text[] NOT NULL,
    instance_id text,
    output jsonb,
    error text,
    claimed_by text,
    claimed_at timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT security_review_stages_aspect_check CHECK ((aspect = ANY (ARRAY['secrets'::text, 'sast'::text, 'sca'::text, 'access'::text, 'infra'::text, 'convener'::text]))),
    CONSTRAINT security_review_stages_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'blocked_on_deps'::text, 'running'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: shca_template_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shca_template_files (
    template_id text NOT NULL,
    filename text NOT NULL,
    content text DEFAULT ''::text NOT NULL
);


--
-- Name: TABLE shca_template_files; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.shca_template_files IS 'DEPRECATED. Superseded by template_files. Data backfilled by migrations 037+038. Pending removal.';


--
-- Name: shca_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shca_templates (
    id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    model text DEFAULT 'google/gemini-2.5-flash'::text NOT NULL,
    created_by text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT shca_templates_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: TABLE shca_templates; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.shca_templates IS 'DEPRECATED. Superseded by templates (type=''SHCA'', owner_actor_id IS NULL). Data backfilled by migrations 037+038. Pending removal.';


--
-- Name: signup_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.signup_requests (
    uid text NOT NULL,
    email text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    photo_url text DEFAULT ''::text NOT NULL,
    requested_role text DEFAULT 'member'::text NOT NULL,
    message text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT signup_requests_requested_role_check CHECK ((requested_role = ANY (ARRAY['editor'::text, 'member'::text]))),
    CONSTRAINT signup_requests_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])))
);


--
-- Name: skill_push_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skill_push_state (
    skill_name text NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skills (
    name text NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    applicable_to text[] DEFAULT '{}'::text[] NOT NULL,
    source_path text,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT skills_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text])))
);


--
-- Name: skills_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.skills_history (
    history_id bigint NOT NULL,
    skill_name text NOT NULL,
    version integer NOT NULL,
    title text NOT NULL,
    description text NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    applicable_to text[] DEFAULT '{}'::text[] NOT NULL,
    status text NOT NULL,
    changed_by text,
    change_reason text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: skills_history_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.skills_history_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: skills_history_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.skills_history_history_id_seq OWNED BY public.skills_history.history_id;


--
-- Name: slack_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_config (
    goal_id text NOT NULL,
    workspace_id text NOT NULL,
    goal_channel_id text DEFAULT ''::text NOT NULL,
    goal_channel_name text DEFAULT ''::text NOT NULL,
    orch_channel_id text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    goal_thread_ts text DEFAULT ''::text,
    orch_thread_ts text DEFAULT ''::text,
    orch_channel_name text DEFAULT ''::text NOT NULL
);


--
-- Name: slack_story_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_story_channels (
    goal_id text NOT NULL,
    story_id text NOT NULL,
    slack_channel_id text NOT NULL,
    slack_channel_name text DEFAULT ''::text NOT NULL,
    user_uid text DEFAULT ''::text NOT NULL,
    thread_id text DEFAULT ''::text NOT NULL,
    agent_id text DEFAULT ''::text NOT NULL,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: slack_thread_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_thread_map (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    slack_channel_id text NOT NULL,
    slack_thread_ts text NOT NULL,
    goal_id text NOT NULL,
    thread_id text DEFAULT ''::text,
    story_id text DEFAULT ''::text,
    agent_id text DEFAULT ''::text,
    thread_type text DEFAULT 'go'::text NOT NULL,
    user_uid text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: slack_user_goal_channels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_user_goal_channels (
    goal_id text NOT NULL,
    user_uid text NOT NULL,
    slack_channel_id text NOT NULL,
    slack_channel_name text DEFAULT ''::text NOT NULL,
    thread_id text DEFAULT ''::text NOT NULL,
    agent_id text DEFAULT ''::text NOT NULL,
    current_story_id text,
    archived_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: slack_user_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_user_map (
    slack_user_id text NOT NULL,
    workspace_id text NOT NULL,
    user_uid text NOT NULL,
    slack_display_name text DEFAULT ''::text,
    slack_email text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: slack_workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.slack_workspaces (
    workspace_id text NOT NULL,
    workspace_name text DEFAULT ''::text NOT NULL,
    bot_token text DEFAULT ''::text NOT NULL,
    bot_user_id text DEFAULT ''::text NOT NULL,
    app_id text DEFAULT ''::text NOT NULL,
    installed_by text DEFAULT ''::text,
    scopes text[] DEFAULT '{}'::text[],
    status text DEFAULT 'active'::text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: story_branches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.story_branches (
    id uuid NOT NULL,
    story_id text NOT NULL,
    branch_name text NOT NULL,
    parent_branch text,
    purpose text,
    plan_version_id uuid,
    worktree_path text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    merged_at timestamp with time zone,
    abandoned_at timestamp with time zone,
    abandoned_reason text,
    status text DEFAULT 'active'::text NOT NULL,
    repo_goal_resource_id text
);


--
-- Name: TABLE story_branches; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.story_branches IS 'DEPRECATED (2026-06-16): empty in all environments; wrong axis (1 story → N branches vs the real model of N stories → 1 shared branch). Branch assignment is done via story_resources (relation=''produces'', goal_resources kind=''branch''). See docs/story-integration/REDESIGN.md §4.2.';


--
-- Name: story_claude_execution_contexts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.story_claude_execution_contexts (
    story_id text NOT NULL,
    workspace_path text,
    shca_instance_id text,
    claude_session_id text,
    claude_state_dir text,
    current_branch_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE story_claude_execution_contexts; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.story_claude_execution_contexts IS 'DEPRECATED (2026-06-16): zero writers (code reads always fell through to stories.shca_instance_id). Two problems: name baked in executor ("Claude"); mixed git state + agent identity + Claude-specific state (3NF violation). Replaced by story_workspaces (git state only) + stories.shca_instance_id (instance pointer). See docs/story-integration/REDESIGN.md §4.2.';


--
-- Name: story_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.story_resources (
    story_id text NOT NULL,
    resource_id text NOT NULL,
    relation text DEFAULT 'needs'::text NOT NULL,
    notes text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT story_resources_relation_check CHECK ((relation = ANY (ARRAY['needs'::text, 'produces'::text, 'modifies'::text])))
);


--
-- Name: story_reviews; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.story_reviews (
    id text NOT NULL,
    story_id text NOT NULL,
    goal_id text NOT NULL,
    reviewed_state text NOT NULL,
    review_text text,
    reviewer_agent_id text,
    run_id text,
    reviewed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: story_workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.story_workspaces (
    story_id text NOT NULL,
    workspace_path text NOT NULL,
    current_branch_name text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: task_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_activity (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    goal_id text NOT NULL,
    agent_id text,
    instance_id text,
    type text NOT NULL,
    summary text NOT NULL,
    content text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    search_vector tsvector
);


--
-- Name: task_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_events (
    id text NOT NULL,
    task_id text NOT NULL,
    goal_id text NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    actor text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    note text DEFAULT ''::text NOT NULL
);


--
-- Name: task_resource_deps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_resource_deps (
    task_id text NOT NULL,
    resource_id text NOT NULL,
    dep_type text DEFAULT 'requires'::text NOT NULL,
    notes text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now(),
    created_by text DEFAULT ''::text NOT NULL
);


--
-- Name: task_resources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_resources (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    task_id text NOT NULL,
    resource_id text NOT NULL,
    access_level text DEFAULT 'read'::text NOT NULL,
    branch text DEFAULT ''::text NOT NULL,
    paths text[] DEFAULT '{}'::text[] NOT NULL,
    constraints jsonb,
    granted_by text DEFAULT 'system'::text NOT NULL,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT task_resources_access_level_check CHECK ((access_level = ANY (ARRAY['none'::text, 'read'::text, 'read-write'::text])))
);


--
-- Name: template_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.template_files (
    template_id text NOT NULL,
    filename text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: COLUMN template_files.updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.template_files.updated_at IS 'Timestamp of the most recent content write. No separate created_at column — check template_history for the earliest version containing this filename to establish creation time.';


--
-- Name: template_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.template_history (
    history_id bigint NOT NULL,
    template_id text NOT NULL,
    version integer NOT NULL,
    snapshot jsonb NOT NULL,
    changed_by text,
    change_reason text,
    changed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: template_history_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.template_history_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: template_history_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.template_history_history_id_seq OWNED BY public.template_history.history_id;


--
-- Name: template_tool_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.template_tool_profiles (
    template_id text NOT NULL,
    profile_id text NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.templates (
    id text NOT NULL,
    type text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    model text DEFAULT 'google/gemini-2.5-flash'::text NOT NULL,
    emoji text DEFAULT ''::text NOT NULL,
    tools_allow text[] DEFAULT '{}'::text[] NOT NULL,
    tools_deny text[] DEFAULT '{}'::text[] NOT NULL,
    memory_enabled boolean DEFAULT false NOT NULL,
    max_tokens integer,
    notes text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    parent_template_id text,
    owner_actor_id uuid,
    CONSTRAINT templates_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text]))),
    CONSTRAINT templates_type_check CHECK ((type = ANY (ARRAY['SHCA'::text, 'IA'::text, 'GO'::text, 'RA'::text, 'Platform'::text, 'PlatformResearch'::text, 'PlatformAssistant'::text])))
);


--
-- Name: thread_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.thread_participants (
    thread_id text NOT NULL,
    uid text NOT NULL,
    unread_count integer DEFAULT 0 NOT NULL,
    last_read_message_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: threads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.threads (
    id text NOT NULL,
    goal_id text NOT NULL,
    agent_id text DEFAULT ''::text NOT NULL,
    task_id text,
    correlation_id uuid,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    title text DEFAULT 'New conversation'::text NOT NULL,
    instance_id text,
    status text DEFAULT 'open'::text NOT NULL,
    participants text[] DEFAULT '{}'::text[],
    left_goal_at timestamp with time zone
);


--
-- Name: token_calls_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.token_calls_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: token_calls_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.token_calls_id_seq OWNED BY public.token_calls.id;


--
-- Name: token_usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.token_usage (
    goal_id text NOT NULL,
    agent_id text NOT NULL,
    total_tokens bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tool_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_credentials (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    credential_type text NOT NULL,
    encrypted_value text NOT NULL,
    header_name text,
    rotated_at timestamp with time zone,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    CONSTRAINT tool_credentials_api_key_header_required CHECK (((credential_type <> 'api_key'::text) OR ((header_name IS NOT NULL) AND (header_name <> ''::text)))),
    CONSTRAINT tool_credentials_credential_type_check CHECK ((credential_type = ANY (ARRAY['bearer'::text, 'api_key'::text, 'basic'::text]))),
    CONSTRAINT tool_credentials_status_check CHECK ((status = ANY (ARRAY['active'::text, 'pending'::text])))
);


--
-- Name: tool_profile_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_profile_tools (
    profile_id text NOT NULL,
    tool_name text NOT NULL,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tool_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_profiles (
    id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT tool_profiles_status_check CHECK ((status = ANY (ARRAY['active'::text, 'deprecated'::text])))
);


--
-- Name: tool_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tool_versions (
    id bigint NOT NULL,
    tool_name text NOT NULL,
    version integer NOT NULL,
    source_code text,
    parameters_schema jsonb,
    output_schema jsonb,
    prompt_section text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: tool_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tool_versions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tool_versions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tool_versions_id_seq OWNED BY public.tool_versions.id;


--
-- Name: tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tools (
    name text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    group_name text DEFAULT ''::text NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    source text DEFAULT 'core'::text NOT NULL,
    status text DEFAULT 'enabled'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    tier text,
    parameters_schema jsonb,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_code text,
    created_by text,
    credential_id uuid,
    manifest jsonb,
    delivery text DEFAULT 'legacy'::text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    prompt_section text,
    output_schema jsonb,
    catalog_managed boolean DEFAULT false NOT NULL,
    is_core boolean DEFAULT false NOT NULL,
    CONSTRAINT tools_delivery_check CHECK ((delivery = ANY (ARRAY['legacy'::text, 'mcp'::text]))),
    CONSTRAINT tools_native_active_only CHECK ((NOT ((catalog_managed = false) AND (tier IS NULL) AND (status <> ALL (ARRAY['enabled'::text, 'beta'::text]))))),
    CONSTRAINT tools_native_is_core CHECK ((NOT ((tier IS NULL) AND (is_core = false)))),
    CONSTRAINT tools_source_check CHECK (((source = ANY (ARRAY['core'::text, 'custom'::text, 'platform'::text])) OR (source ~~ 'plugin:%'::text))),
    CONSTRAINT tools_source_tier_consistency CHECK ((((source = 'custom'::text) AND (tier IS NOT NULL)) OR ((source <> 'custom'::text) AND (tier IS NULL)))),
    CONSTRAINT tools_status_check CHECK ((status = ANY (ARRAY['enabled'::text, 'beta'::text, 'disabled'::text, 'deprecated'::text, 'unavailable'::text]))),
    CONSTRAINT tools_tier_check CHECK (((tier IS NULL) OR (tier = ANY (ARRAY['http_action'::text, 'openapi'::text, 'typescript'::text, 'mcp_server'::text, 'mcp_tool'::text]))))
);


--
-- Name: user_preferences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_preferences (
    user_id text NOT NULL,
    tooltips_enabled boolean DEFAULT true NOT NULL,
    hover_help_enabled boolean DEFAULT true NOT NULL,
    startup_wizard_enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_shca_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_shca_configs (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    user_uid text NOT NULL,
    user_actor_id uuid NOT NULL,
    template_id text NOT NULL,
    agent_id text NOT NULL,
    display_name text NOT NULL,
    identity_md text DEFAULT ''::text NOT NULL,
    user_md text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_shca_configs_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: TABLE user_shca_configs; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.user_shca_configs IS 'DEPRECATED. Superseded by agents.owner_actor_id + agents.template_id. Data backfilled by migrations 037+038. Pending removal.';


--
-- Name: user_tour_progress; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_tour_progress (
    user_id text NOT NULL,
    milestone_id text NOT NULL,
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_deliveries (
    id bigint NOT NULL,
    channel text NOT NULL,
    delivery_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_deliveries_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.webhook_deliveries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: webhook_deliveries_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.webhook_deliveries_id_seq OWNED BY public.webhook_deliveries.id;


--
-- Name: agent_metrics id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_metrics ALTER COLUMN id SET DEFAULT nextval('public.agent_metrics_id_seq'::regclass);


--
-- Name: git_merge_queue enqueue_seq; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_queue ALTER COLUMN enqueue_seq SET DEFAULT nextval('public.git_merge_queue_enqueue_seq_seq'::regclass);


--
-- Name: go_conversation_summaries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_conversation_summaries ALTER COLUMN id SET DEFAULT nextval('public.go_conversation_summaries_id_seq'::regclass);


--
-- Name: go_memory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_memory ALTER COLUMN id SET DEFAULT nextval('public.go_memory_id_seq'::regclass);


--
-- Name: ledger_events sequence_number; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_events ALTER COLUMN sequence_number SET DEFAULT nextval('public.ledger_events_sequence_number_seq'::regclass);


--
-- Name: model_pricing id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing ALTER COLUMN id SET DEFAULT nextval('public.model_pricing_id_seq'::regclass);


--
-- Name: skills_history history_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills_history ALTER COLUMN history_id SET DEFAULT nextval('public.skills_history_history_id_seq'::regclass);


--
-- Name: template_history history_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_history ALTER COLUMN history_id SET DEFAULT nextval('public.template_history_history_id_seq'::regclass);


--
-- Name: token_calls id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_calls ALTER COLUMN id SET DEFAULT nextval('public.token_calls_id_seq'::regclass);


--
-- Name: tool_versions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_versions ALTER COLUMN id SET DEFAULT nextval('public.tool_versions_id_seq'::regclass);


--
-- Name: webhook_deliveries id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries ALTER COLUMN id SET DEFAULT nextval('public.webhook_deliveries_id_seq'::regclass);


--
-- Name: actors actors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actors
    ADD CONSTRAINT actors_pkey PRIMARY KEY (id);


--
-- Name: agent_configs agent_configs_agent_id_config_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_configs
    ADD CONSTRAINT agent_configs_agent_id_config_version_key UNIQUE (agent_id, config_version);


--
-- Name: agent_configs agent_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_configs
    ADD CONSTRAINT agent_configs_pkey PRIMARY KEY (id);


--
-- Name: agent_contexts agent_contexts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_contexts
    ADD CONSTRAINT agent_contexts_pkey PRIMARY KEY (goal_id, agent_id);


--
-- Name: agent_file_history agent_file_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_file_history
    ADD CONSTRAINT agent_file_history_pkey PRIMARY KEY (id);


--
-- Name: agent_files agent_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_files
    ADD CONSTRAINT agent_files_pkey PRIMARY KEY (agent_id, filename);


--
-- Name: agent_gateway_bindings agent_gateway_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gateway_bindings
    ADD CONSTRAINT agent_gateway_bindings_pkey PRIMARY KEY (agent_id);


--
-- Name: agent_instances agent_instances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instances
    ADD CONSTRAINT agent_instances_pkey PRIMARY KEY (id);


--
-- Name: agent_memories agent_memories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_memories
    ADD CONSTRAINT agent_memories_pkey PRIMARY KEY (id);


--
-- Name: agent_metrics agent_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_metrics
    ADD CONSTRAINT agent_metrics_pkey PRIMARY KEY (id);


--
-- Name: agent_sync_log agent_sync_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sync_log
    ADD CONSTRAINT agent_sync_log_pkey PRIMARY KEY (id);


--
-- Name: agent_workspace_files agent_workspace_files_agent_id_filename_snapshot_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_workspace_files
    ADD CONSTRAINT agent_workspace_files_agent_id_filename_snapshot_at_key UNIQUE (agent_id, filename, snapshot_at);


--
-- Name: agent_workspace_files agent_workspace_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_workspace_files
    ADD CONSTRAINT agent_workspace_files_pkey PRIMARY KEY (id);


--
-- Name: agents agents_actor_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_actor_id_key UNIQUE (actor_id);


--
-- Name: agents agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_pkey PRIMARY KEY (id);


--
-- Name: approvals approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_pkey PRIMARY KEY (id);


--
-- Name: audit_log audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);


--
-- Name: chain_head chain_head_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_head
    ADD CONSTRAINT chain_head_pkey PRIMARY KEY (id);


--
-- Name: chat_run_state chat_run_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_run_state
    ADD CONSTRAINT chat_run_state_pkey PRIMARY KEY (thread_id);


--
-- Name: chat_thread_routing chat_thread_routing_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_thread_routing
    ADD CONSTRAINT chat_thread_routing_pkey PRIMARY KEY (thread_id);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (id);


--
-- Name: claude_phases claude_phases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claude_phases
    ADD CONSTRAINT claude_phases_pkey PRIMARY KEY (name);


--
-- Name: council_review_runs council_review_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.council_review_runs
    ADD CONSTRAINT council_review_runs_pkey PRIMARY KEY (id);


--
-- Name: council_review_stages council_review_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.council_review_stages
    ADD CONSTRAINT council_review_stages_pkey PRIMARY KEY (id);


--
-- Name: dag_ceiling_config dag_ceiling_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dag_ceiling_config
    ADD CONSTRAINT dag_ceiling_config_pkey PRIMARY KEY (id);


--
-- Name: dashboard_cache_meta dashboard_cache_meta_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_cache_meta
    ADD CONSTRAINT dashboard_cache_meta_pkey PRIMARY KEY (view_name);


--
-- Name: execution_pauses execution_pauses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_pauses
    ADD CONSTRAINT execution_pauses_pkey PRIMARY KEY (scope, scope_id);


--
-- Name: gateway_cache gateway_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gateway_cache
    ADD CONSTRAINT gateway_cache_pkey PRIMARY KEY (cache_key);


--
-- Name: gateways gateways_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gateways
    ADD CONSTRAINT gateways_pkey PRIMARY KEY (id);


--
-- Name: git_merge_group_nodes git_merge_group_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_group_nodes
    ADD CONSTRAINT git_merge_group_nodes_pkey PRIMARY KEY (merge_group_id, git_node_id);


--
-- Name: git_merge_groups git_merge_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_groups
    ADD CONSTRAINT git_merge_groups_pkey PRIMARY KEY (id);


--
-- Name: git_merge_queue git_merge_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_queue
    ADD CONSTRAINT git_merge_queue_pkey PRIMARY KEY (node_id);


--
-- Name: git_node_stories git_node_stories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_node_stories
    ADD CONSTRAINT git_node_stories_pkey PRIMARY KEY (git_node_id, story_id);


--
-- Name: git_nodes git_nodes_git_repo_id_branch_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_nodes
    ADD CONSTRAINT git_nodes_git_repo_id_branch_name_key UNIQUE (git_repo_id, branch_name);


--
-- Name: git_nodes git_nodes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_nodes
    ADD CONSTRAINT git_nodes_pkey PRIMARY KEY (id);


--
-- Name: git_repo_trunks git_repo_trunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_repo_trunks
    ADD CONSTRAINT git_repo_trunks_pkey PRIMARY KEY (git_repo_id, trunk_branch);


--
-- Name: git_repos git_repos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_repos
    ADD CONSTRAINT git_repos_pkey PRIMARY KEY (id);


--
-- Name: git_webhook_events git_webhook_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_webhook_events
    ADD CONSTRAINT git_webhook_events_pkey PRIMARY KEY (delivery_id);


--
-- Name: go_conversation_summaries go_conversation_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_conversation_summaries
    ADD CONSTRAINT go_conversation_summaries_pkey PRIMARY KEY (id);


--
-- Name: go_memory go_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_memory
    ADD CONSTRAINT go_memory_pkey PRIMARY KEY (id);


--
-- Name: goal_agent_contexts goal_agent_contexts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_agent_contexts
    ADD CONSTRAINT goal_agent_contexts_pkey PRIMARY KEY (goal_id, agent_id);


--
-- Name: goal_agent_memberships goal_agent_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_agent_memberships
    ADD CONSTRAINT goal_agent_memberships_pkey PRIMARY KEY (goal_id, agent_id);


--
-- Name: goal_decisions goal_decisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_decisions
    ADD CONSTRAINT goal_decisions_pkey PRIMARY KEY (id);


--
-- Name: goal_documents goal_documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_documents
    ADD CONSTRAINT goal_documents_pkey PRIMARY KEY (goal_id, doc_type);


--
-- Name: goal_members goal_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_members
    ADD CONSTRAINT goal_members_pkey PRIMARY KEY (goal_id, uid);


--
-- Name: goal_platform_integrations goal_platform_integrations_goal_id_integration_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_platform_integrations
    ADD CONSTRAINT goal_platform_integrations_goal_id_integration_id_key UNIQUE (goal_id, integration_id);


--
-- Name: goal_platform_integrations goal_platform_integrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_platform_integrations
    ADD CONSTRAINT goal_platform_integrations_pkey PRIMARY KEY (id);


--
-- Name: goal_resources goal_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_resources
    ADD CONSTRAINT goal_resources_pkey PRIMARY KEY (id);


--
-- Name: goal_summaries goal_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_summaries
    ADD CONSTRAINT goal_summaries_pkey PRIMARY KEY (goal_id);


--
-- Name: goal_summary goal_summary_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_summary
    ADD CONSTRAINT goal_summary_pkey PRIMARY KEY (goal_id);


--
-- Name: goals goals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goals
    ADD CONSTRAINT goals_pkey PRIMARY KEY (id);


--
-- Name: integration_attempts integration_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_attempts
    ADD CONSTRAINT integration_attempts_pkey PRIMARY KEY (id);


--
-- Name: join_requests join_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.join_requests
    ADD CONSTRAINT join_requests_pkey PRIMARY KEY (goal_id, uid);


--
-- Name: ledger_events ledger_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_events
    ADD CONSTRAINT ledger_events_pkey PRIMARY KEY (event_id);


--
-- Name: ledger_events ledger_events_sequence_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ledger_events
    ADD CONSTRAINT ledger_events_sequence_number_key UNIQUE (sequence_number);


--
-- Name: memory_prompts memory_prompts_agent_type_prompt_kind_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_prompts
    ADD CONSTRAINT memory_prompts_agent_type_prompt_kind_version_key UNIQUE (agent_type, prompt_kind, version);


--
-- Name: memory_prompts memory_prompts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_prompts
    ADD CONSTRAINT memory_prompts_pkey PRIMARY KEY (id);


--
-- Name: memory_writes memory_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_writes
    ADD CONSTRAINT memory_writes_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: model_pricing model_pricing_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_pricing
    ADD CONSTRAINT model_pricing_pkey PRIMARY KEY (id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);


--
-- Name: openapi_specs openapi_specs_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.openapi_specs
    ADD CONSTRAINT openapi_specs_name_key UNIQUE (name);


--
-- Name: openapi_specs openapi_specs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.openapi_specs
    ADD CONSTRAINT openapi_specs_pkey PRIMARY KEY (id);


--
-- Name: platform_integrations pi_credentials_kind_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.platform_integrations
    ADD CONSTRAINT pi_credentials_kind_check CHECK ((credentials_kind = ANY (ARRAY['api_key'::text, 'oauth'::text]))) NOT VALID;


--
-- Name: platform_integrations pi_status_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.platform_integrations
    ADD CONSTRAINT pi_status_check CHECK ((status = ANY (ARRAY['active'::text, 'disabled'::text, 'pending_auth'::text, 'revoked'::text]))) NOT VALID;


--
-- Name: plan_versions plan_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_versions
    ADD CONSTRAINT plan_versions_pkey PRIMARY KEY (id);


--
-- Name: platform_channel_bindings platform_channel_bindings_integration_id_external_channel_i_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_channel_bindings
    ADD CONSTRAINT platform_channel_bindings_integration_id_external_channel_i_key UNIQUE (integration_id, external_channel_id);


--
-- Name: platform_channel_bindings platform_channel_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_channel_bindings
    ADD CONSTRAINT platform_channel_bindings_pkey PRIMARY KEY (id);


--
-- Name: platform_discoveries platform_discoveries_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_discoveries
    ADD CONSTRAINT platform_discoveries_fingerprint_key UNIQUE (fingerprint);


--
-- Name: platform_discoveries platform_discoveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_discoveries
    ADD CONSTRAINT platform_discoveries_pkey PRIMARY KEY (id);


--
-- Name: platform_integrations platform_integrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_integrations
    ADD CONSTRAINT platform_integrations_pkey PRIMARY KEY (id);


--
-- Name: platform_integrations platform_integrations_platform_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_integrations
    ADD CONSTRAINT platform_integrations_platform_key UNIQUE (platform);


--
-- Name: platform_processed_events platform_processed_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_processed_events
    ADD CONSTRAINT platform_processed_events_pkey PRIMARY KEY (platform, external_event_id);


--
-- Name: platform_recreate_state platform_recreate_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_recreate_state
    ADD CONSTRAINT platform_recreate_state_pkey PRIMARY KEY (id);


--
-- Name: platform_registry platform_registry_fingerprint_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_registry
    ADD CONSTRAINT platform_registry_fingerprint_key UNIQUE (fingerprint);


--
-- Name: platform_registry platform_registry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_registry
    ADD CONSTRAINT platform_registry_pkey PRIMARY KEY (id);


--
-- Name: platform_research_findings platform_research_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_research_findings
    ADD CONSTRAINT platform_research_findings_pkey PRIMARY KEY (id);


--
-- Name: platform_research_runs platform_research_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_research_runs
    ADD CONSTRAINT platform_research_runs_pkey PRIMARY KEY (id);


--
-- Name: platform_schedule_state platform_schedule_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_schedule_state
    ADD CONSTRAINT platform_schedule_state_pkey PRIMARY KEY (id);


--
-- Name: platform_suggestions platform_suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_suggestions
    ADD CONSTRAINT platform_suggestions_pkey PRIMARY KEY (id);


--
-- Name: platform_sweeps platform_sweeps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_sweeps
    ADD CONSTRAINT platform_sweeps_pkey PRIMARY KEY (id);


--
-- Name: platform_user_map platform_user_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_user_map
    ADD CONSTRAINT platform_user_map_pkey PRIMARY KEY (platform, external_user_id);


--
-- Name: platform_user_map pum_mapping_status_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.platform_user_map
    ADD CONSTRAINT pum_mapping_status_check CHECK ((mapping_status = ANY (ARRAY['confirmed'::text, 'pending_review'::text, 'rejected'::text]))) NOT VALID;


--
-- Name: resource_access resource_access_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_pkey PRIMARY KEY (resource_id, agent_id);


--
-- Name: resource_activity_signal_notifications resource_activity_signal_notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_signal_notifications
    ADD CONSTRAINT resource_activity_signal_notifications_pkey PRIMARY KEY (id);


--
-- Name: resource_activity_signals resource_activity_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_signals
    ADD CONSTRAINT resource_activity_signals_pkey PRIMARY KEY (id);


--
-- Name: resource_attachments resource_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_attachments
    ADD CONSTRAINT resource_attachments_pkey PRIMARY KEY (id);


--
-- Name: resource_events resource_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_events
    ADD CONSTRAINT resource_events_pkey PRIMARY KEY (id);


--
-- Name: resource_links resource_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_links
    ADD CONSTRAINT resource_links_pkey PRIMARY KEY (id);


--
-- Name: resource_renders resource_renders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_renders
    ADD CONSTRAINT resource_renders_pkey PRIMARY KEY (resource_id);


--
-- Name: resource_summaries resource_summaries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_summaries
    ADD CONSTRAINT resource_summaries_pkey PRIMARY KEY (id);


--
-- Name: resources resources_goal_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resources
    ADD CONSTRAINT resources_goal_id_name_key UNIQUE (goal_id, name);


--
-- Name: resources resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resources
    ADD CONSTRAINT resources_pkey PRIMARY KEY (id);


--
-- Name: security_findings security_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_findings
    ADD CONSTRAINT security_findings_pkey PRIMARY KEY (id);


--
-- Name: security_review_runs security_review_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_review_runs
    ADD CONSTRAINT security_review_runs_pkey PRIMARY KEY (id);


--
-- Name: security_review_skips security_review_skips_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_review_skips
    ADD CONSTRAINT security_review_skips_pkey PRIMARY KEY (id);


--
-- Name: security_review_stages security_review_stages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_review_stages
    ADD CONSTRAINT security_review_stages_pkey PRIMARY KEY (id);


--
-- Name: shca_template_files shca_template_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shca_template_files
    ADD CONSTRAINT shca_template_files_pkey PRIMARY KEY (template_id, filename);


--
-- Name: shca_templates shca_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shca_templates
    ADD CONSTRAINT shca_templates_pkey PRIMARY KEY (id);


--
-- Name: signup_requests signup_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.signup_requests
    ADD CONSTRAINT signup_requests_pkey PRIMARY KEY (uid);


--
-- Name: skill_push_state skill_push_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skill_push_state
    ADD CONSTRAINT skill_push_state_pkey PRIMARY KEY (skill_name);


--
-- Name: skills_history skills_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills_history
    ADD CONSTRAINT skills_history_pkey PRIMARY KEY (history_id);


--
-- Name: skills skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.skills
    ADD CONSTRAINT skills_pkey PRIMARY KEY (name);


--
-- Name: slack_config slack_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_config
    ADD CONSTRAINT slack_config_pkey PRIMARY KEY (goal_id);


--
-- Name: slack_story_channels slack_story_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_story_channels
    ADD CONSTRAINT slack_story_channels_pkey PRIMARY KEY (goal_id, story_id);


--
-- Name: slack_thread_map slack_thread_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_thread_map
    ADD CONSTRAINT slack_thread_map_pkey PRIMARY KEY (id);


--
-- Name: slack_thread_map slack_thread_map_slack_channel_id_slack_thread_ts_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_thread_map
    ADD CONSTRAINT slack_thread_map_slack_channel_id_slack_thread_ts_key UNIQUE (slack_channel_id, slack_thread_ts);


--
-- Name: slack_user_goal_channels slack_user_goal_channels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_user_goal_channels
    ADD CONSTRAINT slack_user_goal_channels_pkey PRIMARY KEY (goal_id, user_uid);


--
-- Name: slack_user_map slack_user_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_user_map
    ADD CONSTRAINT slack_user_map_pkey PRIMARY KEY (slack_user_id, workspace_id);


--
-- Name: slack_workspaces slack_workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_workspaces
    ADD CONSTRAINT slack_workspaces_pkey PRIMARY KEY (workspace_id);


--
-- Name: stories stories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stories
    ADD CONSTRAINT stories_pkey PRIMARY KEY (id);


--
-- Name: story_branches story_branches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_branches
    ADD CONSTRAINT story_branches_pkey PRIMARY KEY (id);


--
-- Name: story_claude_execution_contexts story_claude_execution_contexts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_claude_execution_contexts
    ADD CONSTRAINT story_claude_execution_contexts_pkey PRIMARY KEY (story_id);


--
-- Name: story_resources story_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_resources
    ADD CONSTRAINT story_resources_pkey PRIMARY KEY (story_id, resource_id, relation);


--
-- Name: story_reviews story_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_reviews
    ADD CONSTRAINT story_reviews_pkey PRIMARY KEY (id);


--
-- Name: story_reviews story_reviews_story_id_reviewed_state_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_reviews
    ADD CONSTRAINT story_reviews_story_id_reviewed_state_key UNIQUE (story_id, reviewed_state);


--
-- Name: story_workspaces story_workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_workspaces
    ADD CONSTRAINT story_workspaces_pkey PRIMARY KEY (story_id);


--
-- Name: task_activity task_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_activity
    ADD CONSTRAINT task_activity_pkey PRIMARY KEY (id);


--
-- Name: task_events task_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_events
    ADD CONSTRAINT task_events_pkey PRIMARY KEY (id);


--
-- Name: task_resource_deps task_resource_deps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resource_deps
    ADD CONSTRAINT task_resource_deps_pkey PRIMARY KEY (task_id, resource_id, dep_type);


--
-- Name: task_resources task_resources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resources
    ADD CONSTRAINT task_resources_pkey PRIMARY KEY (id);


--
-- Name: task_resources task_resources_task_id_resource_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resources
    ADD CONSTRAINT task_resources_task_id_resource_id_key UNIQUE (task_id, resource_id);


--
-- Name: task_reviews task_reviews_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reviews
    ADD CONSTRAINT task_reviews_pkey PRIMARY KEY (id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);


--
-- Name: template_files template_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_files
    ADD CONSTRAINT template_files_pkey PRIMARY KEY (template_id, filename);


--
-- Name: template_history template_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_history
    ADD CONSTRAINT template_history_pkey PRIMARY KEY (history_id);


--
-- Name: template_tool_profiles template_tool_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_tool_profiles
    ADD CONSTRAINT template_tool_profiles_pkey PRIMARY KEY (template_id, profile_id);


--
-- Name: templates templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.templates
    ADD CONSTRAINT templates_pkey PRIMARY KEY (id);


--
-- Name: thread_participants thread_participants_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_participants
    ADD CONSTRAINT thread_participants_pkey PRIMARY KEY (thread_id, uid);


--
-- Name: threads threads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_pkey PRIMARY KEY (id);


--
-- Name: token_calls token_calls_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_calls
    ADD CONSTRAINT token_calls_pkey PRIMARY KEY (id);


--
-- Name: token_usage token_usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.token_usage
    ADD CONSTRAINT token_usage_pkey PRIMARY KEY (goal_id, agent_id);


--
-- Name: tool_credentials tool_credentials_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_credentials
    ADD CONSTRAINT tool_credentials_name_key UNIQUE (name);


--
-- Name: tool_credentials tool_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_credentials
    ADD CONSTRAINT tool_credentials_pkey PRIMARY KEY (id);


--
-- Name: tool_profile_tools tool_profile_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_profile_tools
    ADD CONSTRAINT tool_profile_tools_pkey PRIMARY KEY (profile_id, tool_name);


--
-- Name: tool_profiles tool_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_profiles
    ADD CONSTRAINT tool_profiles_pkey PRIMARY KEY (id);


--
-- Name: tool_versions tool_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_versions
    ADD CONSTRAINT tool_versions_pkey PRIMARY KEY (id);


--
-- Name: tool_versions tool_versions_tool_name_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_versions
    ADD CONSTRAINT tool_versions_tool_name_version_key UNIQUE (tool_name, version);


--
-- Name: tools tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_pkey PRIMARY KEY (name);


--
-- Name: goal_resources uq_goal_resource_kind_name; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_resources
    ADD CONSTRAINT uq_goal_resource_kind_name UNIQUE (goal_id, kind, name);


--
-- Name: user_preferences user_preferences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_pkey PRIMARY KEY (user_id);


--
-- Name: user_shca_configs user_shca_configs_agent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_shca_configs
    ADD CONSTRAINT user_shca_configs_agent_id_key UNIQUE (agent_id);


--
-- Name: user_shca_configs user_shca_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_shca_configs
    ADD CONSTRAINT user_shca_configs_pkey PRIMARY KEY (id);


--
-- Name: user_tour_progress user_tour_progress_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tour_progress
    ADD CONSTRAINT user_tour_progress_pkey PRIMARY KEY (user_id, milestone_id);


--
-- Name: users users_actor_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_actor_id_key UNIQUE (actor_id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (uid);


--
-- Name: webhook_deliveries webhook_deliveries_channel_delivery_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_channel_delivery_id_key UNIQUE (channel, delivery_id);


--
-- Name: webhook_deliveries webhook_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_deliveries
    ADD CONSTRAINT webhook_deliveries_pkey PRIMARY KEY (id);


--
-- Name: agent_instances_one_active_per_thread_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX agent_instances_one_active_per_thread_uq ON public.agent_instances USING btree (template_id, goal_id, thread_id) WHERE ((status = 'active'::text) AND (thread_id IS NOT NULL));


--
-- Name: agent_instances_one_go_per_goal_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX agent_instances_one_go_per_goal_uq ON public.agent_instances USING btree (goal_id) WHERE ((template_id = 'go'::text) AND (status = 'active'::text));


--
-- Name: agent_metrics_agent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metrics_agent_idx ON public.agent_metrics USING btree (agent_id, metric) WHERE (agent_id IS NOT NULL);


--
-- Name: agent_metrics_goal_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metrics_goal_idx ON public.agent_metrics USING btree (goal_id, metric) WHERE (goal_id IS NOT NULL);


--
-- Name: agent_metrics_goal_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metrics_goal_text_idx ON public.agent_metrics USING btree (goal_id_text, metric) WHERE (goal_id_text IS NOT NULL);


--
-- Name: agent_metrics_metric_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agent_metrics_metric_time_idx ON public.agent_metrics USING btree (metric, recorded_at DESC);


--
-- Name: agents_is_template_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agents_is_template_idx ON public.agents USING btree (is_template);


--
-- Name: agents_template_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX agents_template_id_idx ON public.agents USING btree (template_id);


--
-- Name: council_runs_active_dedup; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX council_runs_active_dedup ON public.council_review_runs USING btree (goal_id, kind, COALESCE(story_id, ''::text), COALESCE(reviewed_state, ''::text)) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));


--
-- Name: council_runs_single_running; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX council_runs_single_running ON public.council_review_runs USING btree (status) WHERE (status = 'running'::text);


--
-- Name: council_stages_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX council_stages_claim_idx ON public.council_review_stages USING btree (status, created_at);


--
-- Name: council_stages_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX council_stages_run_idx ON public.council_review_stages USING btree (run_id);


--
-- Name: council_stages_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX council_stages_unique ON public.council_review_stages USING btree (run_id, stage, COALESCE(story_id, ''::text));


--
-- Name: git_merge_queue_one_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX git_merge_queue_one_active ON public.git_merge_queue USING btree (git_repo_id, trunk_branch) WHERE (status = 'active'::text);


--
-- Name: git_merge_queue_scan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_merge_queue_scan ON public.git_merge_queue USING btree (git_repo_id, status, enqueue_seq);


--
-- Name: git_node_stories_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_node_stories_story ON public.git_node_stories USING btree (story_id);


--
-- Name: git_nodes_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_nodes_goal ON public.git_nodes USING btree (goal_id);


--
-- Name: git_nodes_repo_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_nodes_repo_state ON public.git_nodes USING btree (git_repo_id, state);


--
-- Name: git_repos_owner_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX git_repos_owner_name ON public.git_repos USING btree (owner, name);


--
-- Name: git_webhook_events_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_webhook_events_repo ON public.git_webhook_events USING btree (git_repo_id, received_at DESC);


--
-- Name: git_webhook_events_undelivered; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX git_webhook_events_undelivered ON public.git_webhook_events USING btree (received_at) WHERE (delivered = false);


--
-- Name: goal_documents_goal_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goal_documents_goal_id_idx ON public.goal_documents USING btree (goal_id);


--
-- Name: goal_summaries_requested_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX goal_summaries_requested_at_idx ON public.goal_summaries USING btree (requested_at);


--
-- Name: idx_actors_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_actors_agent_id ON public.actors USING btree (agent_id) WHERE (agent_id IS NOT NULL);


--
-- Name: idx_actors_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_actors_type ON public.actors USING btree (type);


--
-- Name: idx_actors_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_actors_uid ON public.actors USING btree (uid) WHERE (uid IS NOT NULL);


--
-- Name: idx_agent_configs_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_configs_agent ON public.agent_configs USING btree (agent_id, config_version DESC);


--
-- Name: idx_agent_file_history_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_file_history_agent ON public.agent_file_history USING btree (agent_id, filename, written_at DESC);


--
-- Name: idx_agent_gateway_bindings_gateway; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_gateway_bindings_gateway ON public.agent_gateway_bindings USING btree (gateway_id) WHERE (gateway_id IS NOT NULL);


--
-- Name: idx_agent_instances_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_active ON public.agent_instances USING btree (status) WHERE (status = 'active'::text);


--
-- Name: idx_agent_instances_gateway; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_gateway ON public.agent_instances USING btree (gateway_agent_id);


--
-- Name: idx_agent_instances_parent_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_parent_task ON public.agent_instances USING btree (parent_task_id, status) WHERE (parent_task_id IS NOT NULL);


--
-- Name: idx_agent_instances_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_task ON public.agent_instances USING btree (task_id);


--
-- Name: idx_agent_instances_template; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_template ON public.agent_instances USING btree (template_id);


--
-- Name: idx_agent_instances_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_instances_thread ON public.agent_instances USING btree (thread_id) WHERE (status = 'active'::text);


--
-- Name: idx_agent_sync_log_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_sync_log_agent ON public.agent_sync_log USING btree (agent_id, synced_at DESC);


--
-- Name: idx_agents_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agents_owner ON public.agents USING btree (owner_actor_id) WHERE (owner_actor_id IS NOT NULL);


--
-- Name: idx_approvals_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approvals_pending ON public.approvals USING btree (story_id) WHERE (decided_at IS NULL);


--
-- Name: idx_approvals_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_approvals_story ON public.approvals USING btree (story_id);


--
-- Name: idx_audit_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_audit_goal ON public.audit_log USING btree (goal_id, created_at DESC);


--
-- Name: idx_chat_run_state_stale; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chat_run_state_stale ON public.chat_run_state USING btree (updated_at);


--
-- Name: idx_checkpoints_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_checkpoints_story ON public.checkpoints USING btree (story_id);


--
-- Name: idx_claude_phases_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_claude_phases_status ON public.claude_phases USING btree (status) WHERE (status = 'active'::text);


--
-- Name: idx_go_conv_summaries_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_go_conv_summaries_goal ON public.go_conversation_summaries USING btree (goal_id, created_at DESC);


--
-- Name: idx_go_memory_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_go_memory_category ON public.go_memory USING btree (goal_id, category);


--
-- Name: idx_go_memory_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_go_memory_goal ON public.go_memory USING btree (goal_id, tier, created_at DESC);


--
-- Name: idx_goal_agent_memberships_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_agent_memberships_active ON public.goal_agent_memberships USING btree (goal_id) WHERE (status = 'active'::text);


--
-- Name: idx_goal_agent_memberships_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_agent_memberships_agent ON public.goal_agent_memberships USING btree (agent_id);


--
-- Name: idx_goal_decisions_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_decisions_goal ON public.goal_decisions USING btree (goal_id);


--
-- Name: idx_goal_resources_git_repo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_git_repo ON public.goal_resources USING btree (git_repo_id) WHERE (git_repo_id IS NOT NULL);


--
-- Name: idx_goal_resources_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_goal ON public.goal_resources USING btree (goal_id, status);


--
-- Name: idx_goal_resources_locked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_locked ON public.goal_resources USING btree (locked_by) WHERE (locked_by IS NOT NULL);


--
-- Name: idx_goal_resources_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_parent ON public.goal_resources USING btree (parent_id) WHERE (parent_id IS NOT NULL);


--
-- Name: idx_goal_resources_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_role ON public.goal_resources USING btree (goal_id, role) WHERE (role <> 'feature'::text);


--
-- Name: idx_goal_resources_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_goal_resources_task ON public.goal_resources USING btree (source_task_id) WHERE (source_task_id <> ''::text);


--
-- Name: idx_gpi_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpi_goal ON public.goal_platform_integrations USING btree (goal_id) WHERE (enabled = true);


--
-- Name: idx_gpi_integration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gpi_integration ON public.goal_platform_integrations USING btree (integration_id) WHERE (enabled = true);


--
-- Name: idx_integration_attempts_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_integration_attempts_actor ON public.integration_attempts USING btree (actor_type, actor_id);


--
-- Name: idx_integration_attempts_branch_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_integration_attempts_branch_resource ON public.integration_attempts USING btree (branch_goal_resource_id);


--
-- Name: idx_integration_attempts_goal_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_integration_attempts_goal_resource ON public.integration_attempts USING btree (goal_resource_id);


--
-- Name: idx_ledger_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_actor ON public.ledger_events USING btree (actor_id, "timestamp");


--
-- Name: idx_ledger_corr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_corr ON public.ledger_events USING btree (correlation_id);


--
-- Name: idx_ledger_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_goal ON public.ledger_events USING btree (context_goal_id) WHERE (context_goal_id <> ''::text);


--
-- Name: idx_ledger_payload; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_payload ON public.ledger_events USING gin (payload);


--
-- Name: idx_ledger_sequence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_sequence ON public.ledger_events USING btree (sequence_number);


--
-- Name: idx_ledger_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_task ON public.ledger_events USING btree (context_task_id) WHERE (context_task_id <> ''::text);


--
-- Name: idx_ledger_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ledger_type ON public.ledger_events USING btree (event_type, "timestamp");


--
-- Name: idx_memories_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_search ON public.agent_memories USING gin (search_vector);


--
-- Name: idx_memories_template; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_template ON public.agent_memories USING btree (agent_template_id, scope, status);


--
-- Name: idx_memories_template_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_template_goal ON public.agent_memories USING btree (agent_template_id, goal_id, scope, status) WHERE (goal_id IS NOT NULL);


--
-- Name: idx_memories_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memories_thread ON public.agent_memories USING btree (thread_id, status) WHERE (thread_id IS NOT NULL);


--
-- Name: idx_memory_prompts_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_prompts_recent ON public.memory_prompts USING btree (agent_type, prompt_kind, created_at DESC);


--
-- Name: idx_memory_writes_agent_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_writes_agent_time ON public.memory_writes USING btree (agent_id, written_at DESC);


--
-- Name: idx_memory_writes_goal_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_writes_goal_time ON public.memory_writes USING btree (goal_id, written_at DESC) WHERE (goal_id IS NOT NULL);


--
-- Name: idx_memory_writes_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_writes_recent ON public.memory_writes USING btree (written_at DESC);


--
-- Name: idx_messages_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_fts ON public.messages USING gin (search_vector);


--
-- Name: idx_messages_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_idempotency ON public.messages USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: idx_messages_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_pending ON public.messages USING btree (thread_id, created_at) WHERE (status = ANY (ARRAY['queued'::text, 'processing'::text]));


--
-- Name: idx_messages_platform_ext; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_platform_ext ON public.messages USING btree (platform, external_message_id) WHERE (external_message_id IS NOT NULL);


--
-- Name: idx_messages_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_thread ON public.messages USING btree (thread_id, created_at);


--
-- Name: idx_notif_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notif_thread ON public.notifications USING btree (uid, thread_id, created_at DESC);


--
-- Name: idx_notif_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notif_uid ON public.notifications USING btree (uid, read, created_at DESC);


--
-- Name: idx_pcb_external; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pcb_external ON public.platform_channel_bindings USING btree (integration_id, external_channel_id) WHERE (archived_at IS NULL);


--
-- Name: idx_pcb_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pcb_goal ON public.platform_channel_bindings USING btree (goal_id) WHERE (archived_at IS NULL);


--
-- Name: idx_pcb_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pcb_thread ON public.platform_channel_bindings USING btree (thread_id) WHERE ((archived_at IS NULL) AND (thread_id IS NOT NULL));


--
-- Name: idx_plan_versions_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_versions_story ON public.plan_versions USING btree (story_id);


--
-- Name: idx_plan_versions_story_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_plan_versions_story_version ON public.plan_versions USING btree (story_id, version);


--
-- Name: idx_platform_user_map_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_user_map_actor ON public.platform_user_map USING btree (actor_id, platform) WHERE (actor_id IS NOT NULL);


--
-- Name: idx_platform_user_map_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_user_map_email ON public.platform_user_map USING btree (email, platform) WHERE (email IS NOT NULL);


--
-- Name: idx_platform_user_map_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_user_map_pending ON public.platform_user_map USING btree (platform, mapping_status) WHERE (mapping_status = 'pending_review'::text);


--
-- Name: idx_rasn_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rasn_actor ON public.resource_activity_signal_notifications USING btree (actor_id);


--
-- Name: idx_rasn_signal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rasn_signal ON public.resource_activity_signal_notifications USING btree (signal_id);


--
-- Name: idx_resource_access_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_access_agent ON public.resource_access USING btree (agent_id, goal_id);


--
-- Name: idx_resource_access_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_access_goal ON public.resource_access USING btree (goal_id);


--
-- Name: idx_resource_activity_signals_actor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_activity_signals_actor ON public.resource_activity_signals USING btree (actor_type, actor_id);


--
-- Name: idx_resource_activity_signals_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_activity_signals_target ON public.resource_activity_signals USING btree (repo_goal_resource_id, target_branch_name, status);


--
-- Name: idx_resource_attachments_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_attachments_goal ON public.resource_attachments USING btree (goal_id);


--
-- Name: idx_resource_attachments_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_attachments_resource ON public.resource_attachments USING btree (resource_id);


--
-- Name: idx_resource_events_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_events_resource ON public.resource_events USING btree (resource_id, created_at);


--
-- Name: idx_resource_links_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_links_entity ON public.resource_links USING btree (entity_type, entity_id);


--
-- Name: idx_resource_links_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_links_resource ON public.resource_links USING btree (goal_resource_id, role);


--
-- Name: idx_resource_summaries_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_summaries_goal ON public.resource_summaries USING btree (goal_id);


--
-- Name: idx_resource_summaries_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_resource_summaries_resource ON public.resource_summaries USING btree (resource_id, summary_type, created_at DESC);


--
-- Name: idx_slack_config_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_config_channel ON public.slack_config USING btree (goal_channel_id);


--
-- Name: idx_slack_config_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_config_workspace ON public.slack_config USING btree (workspace_id);


--
-- Name: idx_slack_story_channels_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_story_channels_active ON public.slack_story_channels USING btree (goal_id) WHERE (archived_at IS NULL);


--
-- Name: idx_slack_story_channels_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_slack_story_channels_channel ON public.slack_story_channels USING btree (slack_channel_id);


--
-- Name: idx_slack_story_channels_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_story_channels_thread ON public.slack_story_channels USING btree (thread_id) WHERE (thread_id <> ''::text);


--
-- Name: idx_slack_thread_map_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_thread_map_goal ON public.slack_thread_map USING btree (goal_id);


--
-- Name: idx_slack_thread_map_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_thread_map_thread ON public.slack_thread_map USING btree (thread_id) WHERE (thread_id <> ''::text);


--
-- Name: idx_slack_user_goal_channels_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_user_goal_channels_active ON public.slack_user_goal_channels USING btree (goal_id) WHERE (archived_at IS NULL);


--
-- Name: idx_slack_user_goal_channels_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_slack_user_goal_channels_channel ON public.slack_user_goal_channels USING btree (slack_channel_id);


--
-- Name: idx_slack_user_goal_channels_current_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_user_goal_channels_current_story ON public.slack_user_goal_channels USING btree (current_story_id) WHERE (current_story_id IS NOT NULL);


--
-- Name: idx_slack_user_goal_channels_thread; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_user_goal_channels_thread ON public.slack_user_goal_channels USING btree (thread_id) WHERE (thread_id <> ''::text);


--
-- Name: idx_slack_user_map_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_slack_user_map_uid ON public.slack_user_map USING btree (user_uid);


--
-- Name: idx_stories_assigned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_assigned ON public.stories USING btree (assigned_to, state);


--
-- Name: idx_stories_current_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_current_phase ON public.stories USING btree (current_phase) WHERE (current_phase IS NOT NULL);


--
-- Name: idx_stories_execution_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_execution_mode ON public.stories USING btree (execution_mode);


--
-- Name: idx_stories_execution_paused; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_execution_paused ON public.stories USING btree (execution_paused) WHERE (execution_paused = true);


--
-- Name: idx_stories_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_goal ON public.stories USING btree (goal_id, state);


--
-- Name: idx_stories_merge_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_merge_group ON public.stories USING btree (merge_group_id) WHERE (merge_group_id IS NOT NULL);


--
-- Name: idx_stories_workspace; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_workspace ON public.stories USING btree (workspace_path) WHERE (workspace_path IS NOT NULL);


--
-- Name: idx_stories_workspace_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_stories_workspace_state ON public.stories USING btree (workspace_state) WHERE (workspace_state <> ALL (ARRAY['none'::text, 'deleted'::text]));


--
-- Name: idx_story_branches_active_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_story_branches_active_name ON public.story_branches USING btree (story_id, branch_name) WHERE (status = 'active'::text);


--
-- Name: idx_story_branches_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_story_branches_story ON public.story_branches USING btree (story_id);


--
-- Name: idx_story_resources_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_story_resources_resource ON public.story_resources USING btree (resource_id);


--
-- Name: idx_story_resources_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_story_resources_story ON public.story_resources USING btree (story_id);


--
-- Name: idx_task_activity_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_activity_fts ON public.task_activity USING gin (search_vector);


--
-- Name: idx_task_activity_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_activity_goal ON public.task_activity USING btree (goal_id);


--
-- Name: idx_task_activity_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_activity_task ON public.task_activity USING btree (task_id);


--
-- Name: idx_task_activity_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_activity_type ON public.task_activity USING btree (task_id, type);


--
-- Name: idx_task_events; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_events ON public.task_events USING btree (task_id, created_at);


--
-- Name: idx_task_resource_deps_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_resource_deps_resource ON public.task_resource_deps USING btree (resource_id);


--
-- Name: idx_task_resources_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_resources_resource ON public.task_resources USING btree (resource_id);


--
-- Name: idx_task_resources_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_resources_task ON public.task_resources USING btree (task_id);


--
-- Name: idx_task_reviews_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_task_reviews_task ON public.task_reviews USING btree (task_id, reviewed_at DESC);


--
-- Name: idx_tasks_assigned; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_assigned ON public.tasks USING btree (goal_id, assigned_to, status);


--
-- Name: idx_tasks_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_category ON public.tasks USING btree (goal_id, category, status);


--
-- Name: idx_tasks_claude_session; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_claude_session ON public.tasks USING btree (claude_session_id) WHERE (claude_session_id IS NOT NULL);


--
-- Name: idx_tasks_completed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_completed_at ON public.tasks USING btree (completed_at DESC) WHERE (completed_at IS NOT NULL);


--
-- Name: idx_tasks_decision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_decision ON public.tasks USING btree (decision_event_id) WHERE (decision_event_id IS NOT NULL);


--
-- Name: idx_tasks_executor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_executor ON public.tasks USING btree (executor);


--
-- Name: idx_tasks_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_fts ON public.tasks USING gin (search_vector);


--
-- Name: idx_tasks_goal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_goal ON public.tasks USING btree (goal_id, status);


--
-- Name: idx_tasks_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_parent ON public.tasks USING btree (parent_task_id) WHERE (parent_task_id IS NOT NULL);


--
-- Name: idx_tasks_ready; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_ready ON public.tasks USING btree (goal_id, state) WHERE (state = 'ready'::text);


--
-- Name: idx_tasks_source_thread_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_source_thread_id ON public.tasks USING btree (source_thread_id) WHERE (source_thread_id IS NOT NULL);


--
-- Name: idx_tasks_started_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_started_at ON public.tasks USING btree (started_at DESC) WHERE (started_at IS NOT NULL);


--
-- Name: idx_tasks_state; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_state ON public.tasks USING btree (goal_id, status, state);


--
-- Name: idx_tasks_story; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_story ON public.tasks USING btree (story_id) WHERE (story_id <> ''::text);


--
-- Name: idx_tasks_story_blocked_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_story_blocked_at ON public.tasks USING btree (story_id, last_blocker_at DESC) WHERE (status = 'blocked'::text);


--
-- Name: idx_tasks_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tasks_updated_at ON public.tasks USING btree (updated_at DESC) WHERE (status = 'active'::text);


--
-- Name: idx_thread_participants_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_thread_participants_uid ON public.thread_participants USING btree (uid);


--
-- Name: idx_threads_goal_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_threads_goal_agent ON public.threads USING btree (goal_id, agent_id, created_at DESC);


--
-- Name: idx_token_calls_call_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_calls_call_recorded_at ON public.token_calls USING btree (recorded_at DESC) WHERE (record_type = 'call'::text);


--
-- Name: idx_token_calls_story_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_token_calls_story_id ON public.token_calls USING btree (story_id) WHERE (story_id IS NOT NULL);


--
-- Name: idx_user_shca_configs_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_shca_configs_agent ON public.user_shca_configs USING btree (agent_id);


--
-- Name: idx_user_shca_configs_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_shca_configs_user ON public.user_shca_configs USING btree (user_uid, status);


--
-- Name: idx_webhook_deliveries_channel_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_webhook_deliveries_channel_created ON public.webhook_deliveries USING btree (channel, created_at);


--
-- Name: idx_workspace_files_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspace_files_agent ON public.agent_workspace_files USING btree (agent_id);


--
-- Name: idx_workspace_files_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workspace_files_latest ON public.agent_workspace_files USING btree (agent_id, filename, snapshot_at DESC);


--
-- Name: model_pricing_lookup_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX model_pricing_lookup_idx ON public.model_pricing USING btree (provider, model, effective_from DESC);


--
-- Name: model_pricing_unique_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX model_pricing_unique_idx ON public.model_pricing USING btree (provider, model, effective_from);


--
-- Name: mv_cost_by_goal_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_by_goal_pk ON public.mv_cost_by_goal USING btree (row_num);


--
-- Name: mv_cost_by_model_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_by_model_pk ON public.mv_cost_by_model USING btree (provider, model);


--
-- Name: mv_cost_by_story_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_by_story_pk ON public.mv_cost_by_story USING btree (row_num);


--
-- Name: mv_cost_by_task_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_by_task_pk ON public.mv_cost_by_task USING btree (row_num);


--
-- Name: mv_cost_daily_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_daily_pk ON public.mv_cost_daily USING btree (day);


--
-- Name: mv_cost_daily_provider_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_daily_provider_pk ON public.mv_cost_daily_provider USING btree (day, provider);


--
-- Name: mv_cost_top_agents_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_top_agents_pk ON public.mv_cost_top_agents USING btree (row_num);


--
-- Name: mv_cost_top_tools_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_top_tools_pk ON public.mv_cost_top_tools USING btree (tool);


--
-- Name: mv_cost_totals_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_cost_totals_pk ON public.mv_cost_totals USING btree (row_id);


--
-- Name: mv_dashboard_approval_sla_goals_goal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_approval_sla_goals_goal_id ON public.mv_dashboard_approval_sla_goals USING btree (goal_id);


--
-- Name: mv_dashboard_approval_sla_summary_row_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_approval_sla_summary_row_id ON public.mv_dashboard_approval_sla_summary USING btree (row_id);


--
-- Name: mv_dashboard_channels_integration_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_channels_integration_id ON public.mv_dashboard_channels USING btree (integration_id);


--
-- Name: mv_dashboard_escalations_goal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_escalations_goal_id ON public.mv_dashboard_escalations USING btree (goal_id);


--
-- Name: mv_dashboard_review_quality_agent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_review_quality_agent_id ON public.mv_dashboard_review_quality USING btree (reviewer_agent_id);


--
-- Name: mv_dashboard_slos_row_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_dashboard_slos_row_id ON public.mv_dashboard_slos USING btree (row_id);


--
-- Name: mv_taximeter_human_activity_pk; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_taximeter_human_activity_pk ON public.mv_taximeter_human_activity USING btree (uid, goal_id, metric_key, week_start);


--
-- Name: mv_token_calls_priced_goal_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mv_token_calls_priced_goal_id ON public.mv_token_calls_priced USING btree (goal_id);


--
-- Name: mv_token_calls_priced_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mv_token_calls_priced_recorded_at ON public.mv_token_calls_priced USING btree (recorded_at);


--
-- Name: mv_token_calls_priced_row_num; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX mv_token_calls_priced_row_num ON public.mv_token_calls_priced USING btree (row_num);


--
-- Name: mv_token_calls_priced_session_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mv_token_calls_priced_session_key ON public.mv_token_calls_priced USING btree (session_key);


--
-- Name: platform_discoveries_cat_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_discoveries_cat_status ON public.platform_discoveries USING btree (category, status);


--
-- Name: platform_discoveries_queue_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_discoveries_queue_order ON public.platform_discoveries USING btree (priority DESC, queued_at) WHERE (status = 'new'::text);


--
-- Name: platform_discoveries_single_researching; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX platform_discoveries_single_researching ON public.platform_discoveries USING btree (status) WHERE (status = 'researching'::text);


--
-- Name: platform_registry_cat_role_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_registry_cat_role_idx ON public.platform_registry USING btree (category, target_role);


--
-- Name: platform_registry_verdict_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_registry_verdict_idx ON public.platform_registry USING btree (verdict, impl_status);


--
-- Name: platform_research_findings_dim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_research_findings_dim_idx ON public.platform_research_findings USING btree (dimension, created_at DESC);


--
-- Name: platform_research_runs_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_research_runs_status_idx ON public.platform_research_runs USING btree (status, created_at DESC);


--
-- Name: platform_suggestions_category_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_suggestions_category_idx ON public.platform_suggestions USING btree (category);


--
-- Name: platform_suggestions_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_suggestions_status_idx ON public.platform_suggestions USING btree (status);


--
-- Name: platform_sweeps_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX platform_sweeps_status_idx ON public.platform_sweeps USING btree (status, created_at DESC);


--
-- Name: resource_renders_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX resource_renders_status_idx ON public.resource_renders USING btree (render_status);


--
-- Name: security_findings_run_aspect_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_findings_run_aspect_idx ON public.security_findings USING btree (run_id, aspect);


--
-- Name: security_findings_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_findings_run_idx ON public.security_findings USING btree (run_id);


--
-- Name: security_findings_severity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_findings_severity_idx ON public.security_findings USING btree (run_id, severity);


--
-- Name: security_review_skips_branch; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX security_review_skips_branch ON public.security_review_skips USING btree (lower(clone_url), lower(branch_name));


--
-- Name: security_review_skips_last_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_review_skips_last_idx ON public.security_review_skips USING btree (last_skipped_at);


--
-- Name: security_runs_active_per_branch; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX security_runs_active_per_branch ON public.security_review_runs USING btree (lower(clone_url), lower(branch_name)) WHERE (status = ANY (ARRAY['pending'::text, 'running'::text]));


--
-- Name: security_runs_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_runs_created_idx ON public.security_review_runs USING btree (created_at);


--
-- Name: security_runs_single_running; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX security_runs_single_running ON public.security_review_runs USING btree (status) WHERE (status = 'running'::text);


--
-- Name: security_stages_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_stages_claim_idx ON public.security_review_stages USING btree (status, created_at);


--
-- Name: security_stages_run_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX security_stages_run_idx ON public.security_review_stages USING btree (run_id);


--
-- Name: security_stages_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX security_stages_unique ON public.security_review_stages USING btree (run_id, aspect);


--
-- Name: skills_applicable_to_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX skills_applicable_to_gin ON public.skills USING gin (applicable_to);


--
-- Name: skills_history_name_ver_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX skills_history_name_ver_idx ON public.skills_history USING btree (skill_name, version DESC);


--
-- Name: skills_history_name_ver_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX skills_history_name_ver_uq ON public.skills_history USING btree (skill_name, version);


--
-- Name: skills_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX skills_status_idx ON public.skills USING btree (status);


--
-- Name: story_reviews_goal_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX story_reviews_goal_idx ON public.story_reviews USING btree (goal_id);


--
-- Name: story_reviews_story_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX story_reviews_story_idx ON public.story_reviews USING btree (story_id, reviewed_at DESC);


--
-- Name: tasks_ccphase_parent_state_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tasks_ccphase_parent_state_idx ON public.tasks USING btree (parent_task_id, state) WHERE (executor = 'claude-code-phase'::text);


--
-- Name: template_history_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX template_history_uq ON public.template_history USING btree (template_id, version);


--
-- Name: templates_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX templates_owner_idx ON public.templates USING btree (owner_actor_id) WHERE (owner_actor_id IS NOT NULL);


--
-- Name: templates_parent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX templates_parent_idx ON public.templates USING btree (parent_template_id) WHERE (parent_template_id IS NOT NULL);


--
-- Name: templates_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX templates_status_idx ON public.templates USING btree (status);


--
-- Name: templates_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX templates_type_idx ON public.templates USING btree (type);


--
-- Name: token_calls_agent_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX token_calls_agent_time_idx ON public.token_calls USING btree (agent_id, recorded_at DESC) WHERE (agent_id IS NOT NULL);


--
-- Name: token_calls_goal_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX token_calls_goal_time_idx ON public.token_calls USING btree (goal_id, recorded_at DESC) WHERE (goal_id IS NOT NULL);


--
-- Name: token_calls_goal_upper_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX token_calls_goal_upper_time_idx ON public.token_calls USING btree (upper(goal_id), recorded_at DESC) WHERE (goal_id IS NOT NULL);


--
-- Name: token_calls_model_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX token_calls_model_time_idx ON public.token_calls USING btree (provider, model, recorded_at DESC);


--
-- Name: token_calls_time_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX token_calls_time_idx ON public.token_calls USING btree (recorded_at DESC);


--
-- Name: tool_profile_tools_tool_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_profile_tools_tool_idx ON public.tool_profile_tools USING btree (tool_name);


--
-- Name: tool_profiles_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_profiles_status_idx ON public.tool_profiles USING btree (status);


--
-- Name: tool_versions_tool_name_version_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tool_versions_tool_name_version_idx ON public.tool_versions USING btree (tool_name, version DESC);


--
-- Name: tools_credential_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tools_credential_idx ON public.tools USING btree (credential_id) WHERE (credential_id IS NOT NULL);


--
-- Name: tools_group_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tools_group_idx ON public.tools USING btree (group_name);


--
-- Name: tools_status_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tools_status_idx ON public.tools USING btree (status);


--
-- Name: tools_tier_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tools_tier_idx ON public.tools USING btree (tier) WHERE (tier IS NOT NULL);


--
-- Name: ttp_profile_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ttp_profile_idx ON public.template_tool_profiles USING btree (profile_id);


--
-- Name: ttp_template_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ttp_template_idx ON public.template_tool_profiles USING btree (template_id, "position");


--
-- Name: uq_active_memory_prompt; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_active_memory_prompt ON public.memory_prompts USING btree (agent_type, prompt_kind) WHERE (is_active = true);


--
-- Name: uq_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_idempotency ON public.ledger_events USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: uq_platform_channel_bindings_goal_integration_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_platform_channel_bindings_goal_integration_channel ON public.platform_channel_bindings USING btree (goal_id, integration_id, external_channel_id);


--
-- Name: stories stories_notify_story_done; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER stories_notify_story_done AFTER UPDATE OF state ON public.stories FOR EACH ROW WHEN (((new.state = 'done'::text) AND (old.state IS DISTINCT FROM 'done'::text))) EXECUTE FUNCTION public.council_notify_story_done();


--
-- Name: task_activity trg_activity_fts; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_activity_fts BEFORE INSERT OR UPDATE ON public.task_activity FOR EACH ROW EXECUTE FUNCTION public.update_activity_search_vector();


--
-- Name: agent_memories trg_memory_fields; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_memory_fields BEFORE INSERT OR UPDATE ON public.agent_memories FOR EACH ROW EXECUTE FUNCTION public.update_memory_fields();


--
-- Name: messages trg_messages_fts; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_messages_fts BEFORE INSERT OR UPDATE ON public.messages FOR EACH ROW EXECUTE FUNCTION public.update_messages_search_vector();


--
-- Name: ledger_events trg_no_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_no_delete BEFORE DELETE ON public.ledger_events FOR EACH ROW EXECUTE FUNCTION public.prevent_ledger_mutation();


--
-- Name: ledger_events trg_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_no_update BEFORE UPDATE ON public.ledger_events FOR EACH ROW EXECUTE FUNCTION public.prevent_ledger_mutation();


--
-- Name: tasks trg_tasks_fts; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tasks_fts BEFORE INSERT OR UPDATE ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.update_tasks_search_vector();


--
-- Name: agent_configs agent_configs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_configs
    ADD CONSTRAINT agent_configs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_contexts agent_contexts_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_contexts
    ADD CONSTRAINT agent_contexts_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_contexts agent_contexts_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_contexts
    ADD CONSTRAINT agent_contexts_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: agent_file_history agent_file_history_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_file_history
    ADD CONSTRAINT agent_file_history_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_files agent_files_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_files
    ADD CONSTRAINT agent_files_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_gateway_bindings agent_gateway_bindings_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gateway_bindings
    ADD CONSTRAINT agent_gateway_bindings_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agent_gateway_bindings agent_gateway_bindings_gateway_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_gateway_bindings
    ADD CONSTRAINT agent_gateway_bindings_gateway_id_fkey FOREIGN KEY (gateway_id) REFERENCES public.gateways(id);


--
-- Name: agent_instances agent_instances_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instances
    ADD CONSTRAINT agent_instances_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: agent_instances agent_instances_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_instances
    ADD CONSTRAINT agent_instances_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.agents(id);


--
-- Name: agent_sync_log agent_sync_log_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_sync_log
    ADD CONSTRAINT agent_sync_log_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: agents agents_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agents
    ADD CONSTRAINT agents_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.templates(id);


--
-- Name: approvals approvals_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.approvals
    ADD CONSTRAINT approvals_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: chain_head chain_head_last_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chain_head
    ADD CONSTRAINT chain_head_last_event_id_fkey FOREIGN KEY (last_event_id) REFERENCES public.ledger_events(event_id);


--
-- Name: checkpoints checkpoints_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: council_review_runs council_review_runs_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.council_review_runs
    ADD CONSTRAINT council_review_runs_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: council_review_stages council_review_stages_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.council_review_stages
    ADD CONSTRAINT council_review_stages_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.council_review_runs(id) ON DELETE CASCADE;


--
-- Name: actors fk_actors_agent_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.actors
    ADD CONSTRAINT fk_actors_agent_id FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: git_merge_group_nodes git_merge_group_nodes_git_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_group_nodes
    ADD CONSTRAINT git_merge_group_nodes_git_node_id_fkey FOREIGN KEY (git_node_id) REFERENCES public.git_nodes(id);


--
-- Name: git_merge_group_nodes git_merge_group_nodes_merge_group_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_group_nodes
    ADD CONSTRAINT git_merge_group_nodes_merge_group_id_fkey FOREIGN KEY (merge_group_id) REFERENCES public.git_merge_groups(id) ON DELETE CASCADE;


--
-- Name: git_merge_groups git_merge_groups_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_groups
    ADD CONSTRAINT git_merge_groups_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: git_merge_queue git_merge_queue_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_merge_queue
    ADD CONSTRAINT git_merge_queue_node_id_fkey FOREIGN KEY (node_id) REFERENCES public.git_nodes(id) ON DELETE CASCADE;


--
-- Name: git_node_stories git_node_stories_git_node_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_node_stories
    ADD CONSTRAINT git_node_stories_git_node_id_fkey FOREIGN KEY (git_node_id) REFERENCES public.git_nodes(id) ON DELETE CASCADE;


--
-- Name: git_nodes git_nodes_git_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_nodes
    ADD CONSTRAINT git_nodes_git_repo_id_fkey FOREIGN KEY (git_repo_id) REFERENCES public.git_repos(id);


--
-- Name: git_nodes git_nodes_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_nodes
    ADD CONSTRAINT git_nodes_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: git_repo_trunks git_repo_trunks_git_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.git_repo_trunks
    ADD CONSTRAINT git_repo_trunks_git_repo_id_fkey FOREIGN KEY (git_repo_id) REFERENCES public.git_repos(id);


--
-- Name: go_conversation_summaries go_conversation_summaries_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_conversation_summaries
    ADD CONSTRAINT go_conversation_summaries_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: go_memory go_memory_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.go_memory
    ADD CONSTRAINT go_memory_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_agent_contexts goal_agent_contexts_goal_id_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_agent_contexts
    ADD CONSTRAINT goal_agent_contexts_goal_id_agent_id_fkey FOREIGN KEY (goal_id, agent_id) REFERENCES public.goal_agent_memberships(goal_id, agent_id);


--
-- Name: goal_agent_memberships goal_agent_memberships_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_agent_memberships
    ADD CONSTRAINT goal_agent_memberships_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: goal_agent_memberships goal_agent_memberships_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_agent_memberships
    ADD CONSTRAINT goal_agent_memberships_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_decisions goal_decisions_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_decisions
    ADD CONSTRAINT goal_decisions_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_documents goal_documents_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_documents
    ADD CONSTRAINT goal_documents_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: goal_members goal_members_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_members
    ADD CONSTRAINT goal_members_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_members goal_members_uid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_members
    ADD CONSTRAINT goal_members_uid_fkey FOREIGN KEY (uid) REFERENCES public.users(uid);


--
-- Name: goal_platform_integrations goal_platform_integrations_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_platform_integrations
    ADD CONSTRAINT goal_platform_integrations_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_platform_integrations goal_platform_integrations_integration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_platform_integrations
    ADD CONSTRAINT goal_platform_integrations_integration_id_fkey FOREIGN KEY (integration_id) REFERENCES public.platform_integrations(id);


--
-- Name: goal_resources goal_resources_git_repo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_resources
    ADD CONSTRAINT goal_resources_git_repo_id_fkey FOREIGN KEY (git_repo_id) REFERENCES public.git_repos(id);


--
-- Name: goal_resources goal_resources_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_resources
    ADD CONSTRAINT goal_resources_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: goal_resources goal_resources_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_resources
    ADD CONSTRAINT goal_resources_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.goal_resources(id);


--
-- Name: goal_summaries goal_summaries_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_summaries
    ADD CONSTRAINT goal_summaries_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: goal_summary goal_summary_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.goal_summary
    ADD CONSTRAINT goal_summary_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: integration_attempts integration_attempts_branch_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_attempts
    ADD CONSTRAINT integration_attempts_branch_goal_resource_id_fkey FOREIGN KEY (branch_goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: integration_attempts integration_attempts_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_attempts
    ADD CONSTRAINT integration_attempts_goal_resource_id_fkey FOREIGN KEY (goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: integration_attempts integration_attempts_story_branch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_attempts
    ADD CONSTRAINT integration_attempts_story_branch_id_fkey FOREIGN KEY (story_branch_id) REFERENCES public.story_branches(id);


--
-- Name: integration_attempts integration_attempts_target_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.integration_attempts
    ADD CONSTRAINT integration_attempts_target_goal_resource_id_fkey FOREIGN KEY (target_goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: join_requests join_requests_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.join_requests
    ADD CONSTRAINT join_requests_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: join_requests join_requests_uid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.join_requests
    ADD CONSTRAINT join_requests_uid_fkey FOREIGN KEY (uid) REFERENCES public.users(uid);


--
-- Name: messages messages_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id);


--
-- Name: notifications notifications_uid_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_uid_fkey FOREIGN KEY (uid) REFERENCES public.users(uid);


--
-- Name: openapi_specs openapi_specs_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.openapi_specs
    ADD CONSTRAINT openapi_specs_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.tool_credentials(id) ON DELETE SET NULL;


--
-- Name: platform_integrations pi_bot_actor_id_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_integrations
    ADD CONSTRAINT pi_bot_actor_id_fk FOREIGN KEY (bot_actor_id) REFERENCES public.actors(id) NOT VALID;


--
-- Name: plan_versions plan_versions_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.plan_versions
    ADD CONSTRAINT plan_versions_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: platform_channel_bindings platform_channel_bindings_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_channel_bindings
    ADD CONSTRAINT platform_channel_bindings_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: platform_channel_bindings platform_channel_bindings_integration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_channel_bindings
    ADD CONSTRAINT platform_channel_bindings_integration_id_fkey FOREIGN KEY (integration_id) REFERENCES public.platform_integrations(id);


--
-- Name: platform_user_map pum_actor_id_fk; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_user_map
    ADD CONSTRAINT pum_actor_id_fk FOREIGN KEY (actor_id) REFERENCES public.actors(id) NOT VALID;


--
-- Name: resource_access resource_access_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.agents(id);


--
-- Name: resource_access resource_access_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: resource_access resource_access_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_access
    ADD CONSTRAINT resource_access_resource_id_fkey FOREIGN KEY (resource_id) REFERENCES public.resources(id);


--
-- Name: resource_activity_signal_notifications resource_activity_signal_notifications_signal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_signal_notifications
    ADD CONSTRAINT resource_activity_signal_notifications_signal_id_fkey FOREIGN KEY (signal_id) REFERENCES public.resource_activity_signals(id);


--
-- Name: resource_activity_signals resource_activity_signals_repo_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_activity_signals
    ADD CONSTRAINT resource_activity_signals_repo_goal_resource_id_fkey FOREIGN KEY (repo_goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: resource_attachments resource_attachments_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_attachments
    ADD CONSTRAINT resource_attachments_resource_id_fkey FOREIGN KEY (resource_id) REFERENCES public.goal_resources(id);


--
-- Name: resource_links resource_links_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_links
    ADD CONSTRAINT resource_links_goal_resource_id_fkey FOREIGN KEY (goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: resource_renders resource_renders_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_renders
    ADD CONSTRAINT resource_renders_resource_id_fkey FOREIGN KEY (resource_id) REFERENCES public.goal_resources(id) ON DELETE CASCADE;


--
-- Name: resource_summaries resource_summaries_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resource_summaries
    ADD CONSTRAINT resource_summaries_resource_id_fkey FOREIGN KEY (resource_id) REFERENCES public.goal_resources(id);


--
-- Name: resources resources_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.resources
    ADD CONSTRAINT resources_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: security_findings security_findings_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_findings
    ADD CONSTRAINT security_findings_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.security_review_runs(id) ON DELETE CASCADE;


--
-- Name: security_review_stages security_review_stages_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_review_stages
    ADD CONSTRAINT security_review_stages_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.security_review_runs(id) ON DELETE CASCADE;


--
-- Name: shca_template_files shca_template_files_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shca_template_files
    ADD CONSTRAINT shca_template_files_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.shca_templates(id);


--
-- Name: slack_config slack_config_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_config
    ADD CONSTRAINT slack_config_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: slack_config slack_config_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_config
    ADD CONSTRAINT slack_config_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.slack_workspaces(workspace_id);


--
-- Name: slack_story_channels slack_story_channels_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_story_channels
    ADD CONSTRAINT slack_story_channels_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: slack_story_channels slack_story_channels_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_story_channels
    ADD CONSTRAINT slack_story_channels_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id) ON DELETE CASCADE;


--
-- Name: slack_thread_map slack_thread_map_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_thread_map
    ADD CONSTRAINT slack_thread_map_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: slack_user_goal_channels slack_user_goal_channels_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_user_goal_channels
    ADD CONSTRAINT slack_user_goal_channels_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id) ON DELETE CASCADE;


--
-- Name: slack_user_map slack_user_map_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.slack_user_map
    ADD CONSTRAINT slack_user_map_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.slack_workspaces(workspace_id);


--
-- Name: stories stories_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.stories
    ADD CONSTRAINT stories_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: story_branches story_branches_repo_goal_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_branches
    ADD CONSTRAINT story_branches_repo_goal_resource_id_fkey FOREIGN KEY (repo_goal_resource_id) REFERENCES public.goal_resources(id);


--
-- Name: story_branches story_branches_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_branches
    ADD CONSTRAINT story_branches_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: story_claude_execution_contexts story_claude_execution_contexts_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_claude_execution_contexts
    ADD CONSTRAINT story_claude_execution_contexts_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: story_resources story_resources_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_resources
    ADD CONSTRAINT story_resources_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: story_workspaces story_workspaces_story_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.story_workspaces
    ADD CONSTRAINT story_workspaces_story_id_fkey FOREIGN KEY (story_id) REFERENCES public.stories(id);


--
-- Name: task_activity task_activity_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_activity
    ADD CONSTRAINT task_activity_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: task_events task_events_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_events
    ADD CONSTRAINT task_events_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: task_resource_deps task_resource_deps_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resource_deps
    ADD CONSTRAINT task_resource_deps_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: task_resources task_resources_resource_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resources
    ADD CONSTRAINT task_resources_resource_id_fkey FOREIGN KEY (resource_id) REFERENCES public.resources(id);


--
-- Name: task_resources task_resources_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_resources
    ADD CONSTRAINT task_resources_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: task_reviews task_reviews_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reviews
    ADD CONSTRAINT task_reviews_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id);


--
-- Name: tasks tasks_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: tasks tasks_parent_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id);


--
-- Name: tasks tasks_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id);


--
-- Name: template_files template_files_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_files
    ADD CONSTRAINT template_files_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.templates(id);


--
-- Name: template_tool_profiles template_tool_profiles_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_tool_profiles
    ADD CONSTRAINT template_tool_profiles_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.tool_profiles(id) ON DELETE CASCADE;


--
-- Name: template_tool_profiles template_tool_profiles_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.template_tool_profiles
    ADD CONSTRAINT template_tool_profiles_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.templates(id) ON DELETE CASCADE;


--
-- Name: templates templates_parent_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.templates
    ADD CONSTRAINT templates_parent_template_id_fkey FOREIGN KEY (parent_template_id) REFERENCES public.templates(id);


--
-- Name: thread_participants thread_participants_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.thread_participants
    ADD CONSTRAINT thread_participants_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.threads(id);


--
-- Name: threads threads_goal_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.threads
    ADD CONSTRAINT threads_goal_id_fkey FOREIGN KEY (goal_id) REFERENCES public.goals(id);


--
-- Name: tool_profile_tools tool_profile_tools_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tool_profile_tools
    ADD CONSTRAINT tool_profile_tools_profile_id_fkey FOREIGN KEY (profile_id) REFERENCES public.tool_profiles(id) ON DELETE CASCADE;


--
-- Name: tools tools_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tools
    ADD CONSTRAINT tools_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.tool_credentials(id) ON DELETE RESTRICT;


--
-- Name: user_preferences user_preferences_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT user_preferences_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(uid);


--
-- Name: user_shca_configs user_shca_configs_template_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_shca_configs
    ADD CONSTRAINT user_shca_configs_template_id_fkey FOREIGN KEY (template_id) REFERENCES public.shca_templates(id);


--
-- Name: user_tour_progress user_tour_progress_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tour_progress
    ADD CONSTRAINT user_tour_progress_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(uid);


--
-- PostgreSQL database dump complete
--


