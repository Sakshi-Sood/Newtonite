// Post-login redirect must stay on this site: node tests/test_redirect.mjs
import assert from "node:assert/strict";
import { safeNext } from "../static/redirect.js";

const origin = "http://localhost:8000";
assert.equal(safeNext("/item.html?id=1", origin), "/item.html?id=1");
assert.equal(safeNext(null, origin), "/");
assert.equal(safeNext("//evil.com", origin), "/");
assert.equal(safeNext("/\\evil.com", origin), "/"); // browsers treat "\" as "/"
assert.equal(safeNext("https://evil.com/x", origin), "/");
assert.equal(safeNext("javascript:alert(1)", origin), "/");
console.log("redirect: all assertions passed");
