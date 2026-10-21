"""Permission rules. Pure functions, no database.

The API decides every request with these, and returns `allowed_actions`
so the UI can show only the controls that will actually work.

Statuses themselves are data (see app.statuses); the rules here only care about
whether a task is *closed* (in a finished status, or rejected by a lead) and
about its approval step.
"""

PRIORITIES = ("urgent", "high", "medium", "low")
ROLES = ("member", "lead")  # team membership roles; "requester" is derived (created it, not in the team)
APPROVAL_STATES = ("pending", "approved", "rejected")


def _same(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def allowed_actions(item: dict, user_id, role: str | None) -> list[str]:
    """Everything `user_id` may do to `item`.

    `role` is the user's role in the item's team ("member" / "lead"), or
    "requester" when they created the item but aren't in its team, or None.
    `item` needs: closed, approval, assignee_id, assigned_by, created_by, restricted.
    """
    if role is None:
        return []
    is_lead = role == "lead"
    closed = bool(item.get("closed"))
    approval = item.get("approval")
    is_assignee = _same(item["assignee_id"], user_id)
    is_creator = _same(item["created_by"], user_id)
    restricted = bool(item.get("restricted"))

    acts = ["view"]
    # Editing details (and moving to another team). A restricted item can only
    # be edited by leads and its creator; status changes are not affected.
    if not closed and (is_lead or is_creator or not restricted):
        acts.append("edit")
    acts.append("comment")
    if not closed and is_creator and not is_lead:
        # Tick/untick "Restrict editing". Leads can always edit, so it's only
        # offered to a creator who isn't a lead.
        acts.append("restrict")
    if role == "requester":
        return acts  # requesters follow and edit; the owning team does the work
    if not closed and item["assignee_id"] is None:
        acts.append("claim")
    # (Re)assign: any lead, or whoever made the current assignment. Someone who
    # was handed a task can't pass it on.
    if not closed and (is_lead or _same(item.get("assigned_by"), user_id)):
        acts.append("assign")
    # Change status: the assignee or a lead, to any status, unless a lead still
    # has to approve the task (or has rejected it).
    if approval not in ("pending", "rejected") and (is_lead or is_assignee):
        acts.append("move")
    # Approval is a lead's call, never the task creator's.
    if is_lead and not is_creator:
        if approval in ("pending", "rejected"):
            acts.append("approve")
        if approval == "pending":
            acts.append("reject")
    return acts


def can(item: dict, user_id, role: str | None, permission: str) -> bool:
    return permission in allowed_actions(item, user_id, role)
