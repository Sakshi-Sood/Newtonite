// Edit-merge rules used by the item page: node tests/test_edits.mjs
import assert from "node:assert/strict";
import { changedFields, conflictRows } from "../static/edits.js";

const snapshot = { title: "Old", description: "d", priority: "medium", due_at: "2026-11-01T10:00:00.000Z" };

// Only fields the user actually changed are sent, so "Keep mine" can't revert
// someone else's change to a field this user never touched.
assert.deepEqual(changedFields(snapshot, { ...snapshot, title: "New" }), { title: "New" });
assert.deepEqual(changedFields(snapshot, { ...snapshot }), {});

// The same instant written differently is not a change.
assert.deepEqual(changedFields(snapshot, { ...snapshot, due_at: "2026-11-01T10:00:00Z" }), {});
assert.deepEqual(changedFields(snapshot, { ...snapshot, due_at: null }), { due_at: null });

// Conflict view: only my changed fields, shown against the server's current value.
const current = { title: "Theirs", description: "d", priority: "urgent", due_at: snapshot.due_at };
assert.deepEqual(conflictRows({ title: "New" }, current), [{ field: "title", mine: "New", current: "Theirs" }]);
// I changed priority to what they also set: no conflict to show.
assert.deepEqual(conflictRows({ priority: "urgent" }, current), []);

console.log("edits: all assertions passed");
