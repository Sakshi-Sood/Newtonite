// Pure helpers for editing a work item without overwriting other people's changes.

const sameValue = (field, a, b) => {
  if (field === "due_at") {
    const t = (v) => (v ? new Date(v).getTime() : null);
    return t(a) === t(b);
  }
  return (a ?? null) === (b ?? null);
};

// The fields the user changed since opening the editor. Only these are sent,
// so saving can never revert a field someone else changed in the meantime.
export function changedFields(snapshot, draft) {
  const out = {};
  for (const [field, value] of Object.entries(draft)) {
    if (!sameValue(field, snapshot[field], value)) out[field] = value;
  }
  return out;
}

// For the conflict view: my changed fields whose value differs from the server's now.
export function conflictRows(changes, current) {
  return Object.entries(changes)
    .filter(([field, mine]) => !sameValue(field, mine, current[field]))
    .map(([field, mine]) => ({ field, mine, current: current[field] }));
}
