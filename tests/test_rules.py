"""Pure permission rules: python -m unittest -v"""
import unittest

from app.rules import allowed_actions, can

# assignee u2, creator u3, in a status that isn't finished
ITEM = {"closed": False, "approval": None, "assignee_id": "u2", "created_by": "u3"}


class RulesTest(unittest.TestCase):
    def test_non_member_gets_nothing(self):
        self.assertEqual(allowed_actions(ITEM, "u1", None), [])

    def test_member_only_moves_own_item(self):
        self.assertFalse(can(ITEM, "u1", "member", "move"))
        self.assertTrue(can(ITEM, "u2", "member", "move"))

    def test_lead_can_move_and_assign_any(self):
        self.assertTrue(can(ITEM, "u1", "lead", "move") and can(ITEM, "u1", "lead", "assign"))

    def test_member_cannot_assign(self):
        self.assertFalse(can(ITEM, "u2", "member", "assign"))

    def test_closed_item_allows_comment_and_reopening_only(self):
        closed = {**ITEM, "closed": True}
        acts = allowed_actions(closed, "u2", "member")
        self.assertIn("comment", acts)
        self.assertIn("move", acts)  # the assignee can move it back out of a finished status
        self.assertNotIn("edit", acts)
        self.assertNotIn("claim", acts)
        self.assertNotIn("assign", allowed_actions({**closed, "assignee_id": None}, "u1", "lead"))

    def test_claim_only_when_unassigned(self):
        self.assertFalse(can(ITEM, "u1", "member", "claim"))
        self.assertTrue(can({**ITEM, "assignee_id": None}, "u1", "member", "claim"))

    def test_pending_approval_blocks_status_changes_for_everyone(self):
        pending = {**ITEM, "approval": "pending"}
        self.assertFalse(can(pending, "u2", "member", "move"))
        self.assertFalse(can(pending, "u9", "lead", "move"))
        self.assertTrue(can({**ITEM, "approval": "approved"}, "u2", "member", "move"))

    def test_approval_is_a_leads_call_and_never_the_creators(self):
        pending = {**ITEM, "approval": "pending"}
        self.assertTrue(can(pending, "u9", "lead", "approve") and can(pending, "u9", "lead", "reject"))
        self.assertFalse(can(pending, "u3", "lead", "approve"))  # creator, even as lead
        self.assertFalse(can(pending, "u2", "member", "approve"))
        self.assertFalse(can(ITEM, "u9", "lead", "approve"))  # nothing to approve

    def test_a_rejected_task_is_closed_but_a_lead_can_still_approve_it(self):
        rejected = {**ITEM, "approval": "rejected", "closed": True}
        self.assertTrue(can(rejected, "u9", "lead", "approve"))
        self.assertFalse(can(rejected, "u9", "lead", "reject"))
        self.assertFalse(can(rejected, "u2", "member", "move"))

    def test_restricted_item_details_only_for_leads_and_creator(self):
        item = {**ITEM, "restricted": True}
        self.assertFalse(can(item, "u2", "member", "edit"))  # assignee member: no
        self.assertTrue(can(item, "u3", "member", "edit"))   # creator: yes
        self.assertTrue(can(item, "u9", "lead", "edit"))     # lead: yes
        self.assertTrue(can({**ITEM, "restricted": False}, "u2", "member", "edit"))

    def test_restriction_does_not_lock_status(self):
        self.assertTrue(can({**ITEM, "restricted": True}, "u2", "member", "move"))

    def test_only_a_non_lead_creator_toggles_restriction(self):
        self.assertFalse(can(ITEM, "u2", "member", "restrict"))
        self.assertTrue(can(ITEM, "u3", "member", "restrict"))
        self.assertFalse(can(ITEM, "u9", "lead", "restrict"))  # leads always edit; no checkbox
        self.assertFalse(can(ITEM, "u3", "lead", "restrict"))  # even a lead who created it

    def test_assigner_can_reassign_but_assignee_cannot_pass_it_on(self):
        item = {**ITEM, "assignee_id": "u2", "assigned_by": "u5"}
        self.assertTrue(can(item, "u5", "member", "assign"))   # made the assignment
        self.assertFalse(can(item, "u2", "member", "assign"))  # was handed it
        self.assertTrue(can(item, "u9", "lead", "assign"))     # any lead

    def test_requester_views_comments_edits_but_does_no_work(self):
        acts = allowed_actions({**ITEM, "restricted": True, "created_by": "r1"}, "r1", "requester")
        self.assertEqual(acts, ["view", "edit", "comment", "restrict"])
        unassigned = {**ITEM, "assignee_id": None, "created_by": "r1"}
        self.assertNotIn("claim", allowed_actions(unassigned, "r1", "requester"))
        self.assertNotIn("move", allowed_actions(unassigned, "r1", "requester"))


if __name__ == "__main__":
    unittest.main()
