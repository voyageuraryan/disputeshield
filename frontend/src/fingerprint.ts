// Phase 15: a simple, privacy-preserving device fingerprint for the checkout. Only a SHA-256 of
// user agent + screen + timezone leaves the browser; the server keys and hashes it again and never sees raw values.

export function fingerprintSource(nav: { userAgent: string }, scr: { width: number; height: number; colorDepth: number }, timeZone: string): string {
  return `${nav.userAgent}|${scr.width}x${scr.height}x${scr.colorDepth}|${timeZone}`;
}

export async function sha256Hex(text: string): Promise<string | null> {
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null; // no WebCrypto (e.g. plain http on a non-localhost host): checkout continues without it
  }
}

export async function deviceFingerprint(): Promise<string | null> {
  try {
    return await sha256Hex(fingerprintSource(navigator, screen, Intl.DateTimeFormat().resolvedOptions().timeZone ?? ""));
  } catch {
    return null;
  }
}
