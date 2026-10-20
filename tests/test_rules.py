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


if __name__ == "__main__":
    unittest.main()
