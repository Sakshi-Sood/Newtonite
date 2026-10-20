// Where to go after login: only a path on this same site, never another host.
export function safeNext(next, origin) {
  if (!next) return "/";
  try {
    const url = new URL(next, origin); // resolves "//host" and "/\host" the way browsers do
    return url.origin === origin ? url.pathname + url.search + url.hash : "/";
  } catch {
    return "/";
  }
}
