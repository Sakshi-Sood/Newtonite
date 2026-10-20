"""Pure workflow/permission rules: python -m unittest -v"""
import unittest

from app.rules import allowed_actions, can, next_status

ITEM = {"status": "in_progress", "assignee_id": "u2", "created_by": "u3"}


class RulesTest(unittest.TestCase):
    def test_transitions(self):
        self.assertEqual(next_status("open", "start"), "in_progress")
        self.assertIsNone(next_status("done", "cancel"))
        self.assertIsNone(next_status("awaiting_approval", "start"))

    def test_non_member_gets_nothing(self):
        self.assertEqual(allowed_actions(ITEM, "u1", None), [])

    def test_member_only_completes_own_item(self):
        self.assertFalse(can(ITEM, "u1", "member", "complete"))
        self.assertTrue(can(ITEM, "u2", "member", "complete"))

    def test_lead_can_complete_any_and_assign(self):
        self.assertTrue(can(ITEM, "u1", "lead", "complete") and can(ITEM, "u1", "lead", "assign"))

    def test_member_cannot_assign_or_cancel(self):
        self.assertFalse(can(ITEM, "u2", "member", "assign") or can(ITEM, "u2", "member", "cancel"))

    def test_creator_cannot_approve_own_item(self):
        item = {"status": "awaiting_approval", "assignee_id": None, "created_by": "u1"}
        self.assertFalse(can(item, "u1", "lead", "approve"))
        self.assertTrue(can(item, "u9", "lead", "approve"))

    def test_terminal_allows_comment_only(self):
        acts = allowed_actions({"status": "cancelled", "assignee_id": None, "created_by": "u3"}, "u1", "lead")
        self.assertIn("comment", acts)
        self.assertNotIn("edit", acts)
        self.assertNotIn("claim", acts)

    def test_claim_only_when_unassigned(self):
        self.assertFalse(can(ITEM, "u1", "member", "claim"))

    def test_restricted_item_details_only_for_leads_and_creator(self):
        item = {**ITEM, "restricted": True}  # assignee u2, creator u3
        self.assertFalse(can(item, "u2", "member", "edit"))  # assignee member: no
        self.assertTrue(can(item, "u3", "member", "edit"))   # creator: yes
        self.assertTrue(can(item, "u9", "lead", "edit"))     # lead: yes
        self.assertTrue(can({**ITEM, "restricted": False}, "u2", "member", "edit"))

    def test_restriction_does_not_lock_status(self):
        item = {**ITEM, "restricted": True}
        self.assertTrue(can(item, "u2", "member", "complete"))

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
        unassigned = {"status": "open", "assignee_id": None, "created_by": "r1"}
        self.assertNotIn("claim", allowed_actions(unassigned, "r1", "requester"))


if __name__ == "__main__":
    unittest.main()
