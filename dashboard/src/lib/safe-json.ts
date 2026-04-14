/**
 * Safe JSON stringification for rendering untrusted config payloads in
 * the UI (plugin manifests, tool parameter schemas, log extra fields).
 *
 * Two guards:
 *   1. Size clamp — truncates the rendered string at a max length and
 *      appends a "… (clamped, N more chars)" tail so reviewers can tell
 *      the view is incomplete.
 *   2. Secret redaction — any object key whose name matches a
 *      known-sensitive pattern (token, api_key, password, secret,
 *      authorization, cookie, private_key, session) has its value
 *      replaced with "[REDACTED]" before serialization. Prevents a
 *      config with a leaked credential from being rendered verbatim
 *      into the DOM.
 */

const DEFAULT_MAX_LEN = 4_000;

const SECRET_KEY_PATTERNS: RegExp[] = [
  /token/i,
  /api[_-]?key/i,
  /apikey/i,
  /password/i,
  /passwd/i,
  /secret/i,
  /authorization/i,
  /auth/i,
  /cookie/i,
  /private[_-]?key/i,
  /session/i,
  /credential/i,
];

function isSecretKey(key: string): boolean {
  return SECRET_KEY_PATTERNS.some((pat) => pat.test(key));
}

/**
 * JSON.stringify replacer that masks values under secret-looking keys.
 * Applies one level deep — nested objects recursively inherit the mask
 * via the same replacer callback.
 */
function redactReplacer(key: string, value: unknown): unknown {
  if (key && isSecretKey(key) && value !== null && value !== undefined) {
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return "[REDACTED]";
    }
  }
  return value;
}

export interface SafeStringifyOptions {
  /** Indent level passed through to JSON.stringify. Default 2. */
  indent?: number;
  /** Max rendered characters. Default 4000. */
  maxLength?: number;
}

/**
 * Stringify `value` for render with redaction + clamp applied. Never
 * throws — circular / unserializable values fall back to "[unserializable]".
 */
export function safeStringify(
  value: unknown,
  options: SafeStringifyOptions = {},
): string {
  const indent = options.indent ?? 2;
  const maxLength = options.maxLength ?? DEFAULT_MAX_LEN;
  let rendered: string;
  try {
    rendered = JSON.stringify(value, redactReplacer, indent);
  } catch {
    return "[unserializable]";
  }
  if (rendered === undefined) return "";
  if (rendered.length <= maxLength) return rendered;
  const truncated = rendered.slice(0, maxLength);
  const tail = rendered.length - maxLength;
  return `${truncated}\n… (clamped, ${tail} more chars)`;
}
