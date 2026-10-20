// Responses can arrive out of order (a poll that started before a save can
// finish after it). Versions only ever go up, so keep whichever copy is newer.
export function latest(held, incoming) {
  if (!held) return incoming;
  return incoming.version >= held.version ? incoming : held;
}
