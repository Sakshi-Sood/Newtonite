// Keeping the newest server copy of an item: node tests/test_state.mjs
import assert from "node:assert/strict";
import { latest } from "../static/state.js";

const v1 = { id: "a", version: 1, title: "old" };
const v2 = { id: "a", version: 2, title: "new" };

// A slow poll that started before my save must not overwrite the saved item.
assert.equal(latest(v2, v1), v2);
// Newer server state always wins.
assert.equal(latest(v1, v2), v2);
// Same version (e.g. a new comment changed last_activity_at) takes the fresh copy.
const v2b = { ...v2, last_activity_at: "later" };
assert.equal(latest(v2, v2b), v2b);
// Nothing held yet.
assert.equal(latest(null, v1), v1);

console.log("state: all assertions passed");
