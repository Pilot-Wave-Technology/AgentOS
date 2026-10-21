-- Retire the legacy agent templates.
--
-- `go` and `default-shca` were the GO and assistant templates before
-- `go-v2` and `kernel-assistant`; the five IA templates and
-- `task-reviewer-ia` belonged to the retired task-controller IA flow.
-- Nothing creates agents from any of them now, and every agent ever made
-- from them is already `deleted` (their goals are gone). They only show
-- up in the template list, looking like choices.
--
-- SAFE BY CONSTRUCTION: a template is deleted only when no agent that is
-- not `deleted` still points at it, so a database where one IS in use
-- keeps it. The same condition is spelled in each statement below (no
-- scratch table: the expand/contract guard cannot tell a scratch table's
-- DROP from a real one). Idempotent: a second run finds nothing to do.

-- The legacy rows named after the templates (template_id NULL, no goal).
UPDATE agents SET status = 'deleted', updated_at = now()
 WHERE id IN ('go', 'default-shca', 'planner-ia', 'backend-dev-ia',
              'frontend-dev-ia', 'code-test-ia', 'code-review-ia',
              'task-reviewer-ia')
   AND status <> 'deleted';

-- Deleted agents keep their history; they just stop naming the template.
UPDATE agents SET template_id = NULL
 WHERE status = 'deleted'
   AND template_id IN (
       SELECT t.id FROM templates t
        WHERE (t.id IN ('go', 'default-shca', 'planner-ia', 'backend-dev-ia',
                        'frontend-dev-ia', 'code-test-ia', 'code-review-ia',
                        'task-reviewer-ia')
               OR t.parent_template_id IN ('go', 'default-shca'))
          AND NOT EXISTS (SELECT 1 FROM agents a
                           WHERE a.template_id = t.id AND a.status <> 'deleted'));

DELETE FROM template_files
 WHERE template_id IN (
       SELECT t.id FROM templates t
        WHERE (t.id IN ('go', 'default-shca', 'planner-ia', 'backend-dev-ia',
                        'frontend-dev-ia', 'code-test-ia', 'code-review-ia',
                        'task-reviewer-ia')
               OR t.parent_template_id IN ('go', 'default-shca'))
          AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.template_id = t.id));

-- Children first: a clone's parent_template_id references its parent.
DELETE FROM templates t
 WHERE t.parent_template_id IN ('go', 'default-shca')
   AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.template_id = t.id);

DELETE FROM templates t
 WHERE t.id IN ('go', 'default-shca', 'planner-ia', 'backend-dev-ia',
                'frontend-dev-ia', 'code-test-ia', 'code-review-ia',
                'task-reviewer-ia')
   AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.template_id = t.id)
   AND NOT EXISTS (SELECT 1 FROM templates c WHERE c.parent_template_id = t.id);

-- Prompt and tool text that told agents to use the retired ids.
UPDATE template_files
   SET content = replace(content, '(template_id: `go` or `default-shca`).',
                                  '(template_id: `go-v2` or `kernel-assistant`).'),
       updated_at = now()
 WHERE template_id = 'platform-shca' AND filename = 'AGENTS.md'
   AND content LIKE '%(template_id: `go` or `default-shca`).%';

UPDATE template_files
   SET content = replace(replace(content,
           'op_params={"template_id":"<go|default-shca>","model":"<exact supported model id>"}',
           'op_params={"template_id":"<go-v2|kernel-assistant>","model":"<exact supported model id>"}'),
           '(target_role GO -> template_id "go"; SHCA -> "default-shca".)',
           '(target_role GO -> template_id "go-v2"; SHCA -> "kernel-assistant".)'),
       updated_at = now()
 WHERE template_id = 'review-integration' AND filename = 'AGENTS.md'
   AND content LIKE '%<go|default-shca>%';

UPDATE tools
   SET description = replace(description,
           'template_id is ''go'' (GO) or ''default-shca'' (SHCA).',
           'template_id is ''go-v2'' (GO) or ''kernel-assistant'' (assistant).')
 WHERE name = 'apply_model_change'
   AND description LIKE '%''default-shca'' (SHCA)%';
