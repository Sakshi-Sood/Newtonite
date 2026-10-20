"""Workflow and permission rules. Pure functions, no database.

The API decides every request with these, and returns `allowed_actions`
so the UI can show only the controls that will actually work.
"""

STATUSES = ("awaiting_approval", "open", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("urgent", "high", "medium", "low")
ITEM_TYPES = ("customer_issue", "engineering", "payment", "incident", "compliance", "task")
ROLES = ("member", "lead")
TERMINAL = {"done", "cancelled"}

ACTIONS = ("approve", "reject", "start", "block", "unblock", "complete", "reopen", "cancel")
REASON_REQUIRED = {"reject", "block", "complete", "cancel"}

# status -> action -> new status
TRANSITIONS: dict[str, dict[str, str]] = {
    "awaiting_approval": {"approve": "open", "reject": "cancelled"},
    "open": {"start": "in_progress", "cancel": "cancelled"},
    "in_progress": {"block": "blocked", "complete": "done", "cancel": "cancelled"},
    "blocked": {"unblock": "in_progress", "cancel": "cancelled"},
    "done": {"reopen": "in_progress"},
    "cancelled": {},
}

# Who may perform each transition (beyond being in the item's team).
_LEAD_ONLY = {"cancel"}
_APPROVAL = {"approve", "reject"}  # lead, and never the item's creator
_ASSIGNEE_OR_LEAD = {"start", "block", "unblock", "complete", "reopen"}


def next_status(status: str, action: str) -> str | None:
    return TRANSITIONS.get(status, {}).get(action)


def _same(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def allowed_actions(item: dict, user_id, role: str | None) -> list[str]:
    """Everything `user_id` (with `role` in the item's team) may do to `item`."""
    if role is None:
        return []
    is_lead = role == "lead"
    terminal = item["status"] in TERMINAL
    is_assignee = _same(item["assignee_id"], user_id)
    is_creator = _same(item["created_by"], user_id)

    acts = ["view"]
    if not terminal:
        acts.append("edit")
    acts.append("comment")
    if not terminal and item["assignee_id"] is None:
        acts.append("claim")
    if not terminal and is_lead:
        acts.append("assign")

    for action in ACTIONS:
        if next_status(item["status"], action) is None:
            continue
        if action in _APPROVAL:
            ok = is_lead and not is_creator
        elif action in _LEAD_ONLY:
            ok = is_lead
        else:
            ok = is_lead or is_assignee
        if ok:
            acts.append(action)
    return acts


def can(item: dict, user_id, role: str | None, permission: str) -> bool:
    return permission in allowed_actions(item, user_id, role)
