"""Role constants and display-label helpers.

DB values are unchanged — these constants centralise what each role string
means so callers never compare against a bare string. Any future DB rename
becomes a one-file change here.
"""


class SystemRole:
    """Platform-wide (system) roles stored in users.system_role."""

    PLATFORM_ADMIN  = "admin"
    PLATFORM_EDITOR = "editor"
    PLATFORM_MEMBER = "member"

    # Convenience tuples for membership checks
    ALL       = (PLATFORM_ADMIN, PLATFORM_EDITOR, PLATFORM_MEMBER)
    PRIVILEGED = (PLATFORM_ADMIN, PLATFORM_EDITOR)   # admin OR editor


class GoalRole:
    """Per-goal roles stored in goal_members.role."""

    GOAL_ADMIN  = "admin"
    GOAL_MEMBER = "member"

    ALL = (GOAL_ADMIN, GOAL_MEMBER)


# Human-readable labels for display in templates and API responses.
# DB value → display label.

SYSTEM_ROLE_LABELS: dict[str, str] = {
    SystemRole.PLATFORM_ADMIN:  "platform.admin",
    SystemRole.PLATFORM_EDITOR: "platform.editor",
    SystemRole.PLATFORM_MEMBER: "platform.member",
}

GOAL_ROLE_LABELS: dict[str, str] = {
    GoalRole.GOAL_ADMIN:  "goal.manager",
    GoalRole.GOAL_MEMBER: "goal.member",
}
