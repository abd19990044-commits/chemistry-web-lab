// ===========================================================================
// Security, Authentication, Owner Sanitization, and Credential Scrubbing
// ===========================================================================

import { Env } from "./types";
import { errorResponse } from "./errors";

const FORBIDDEN_SECRET_KEYS = [
  "kaggle_key",
  "api_token",
  "password",
  "raw_secret",
  "private_key",
  "kaggle_api_key",
  "plaintext_key",
  "encryption_key",
];

export function validateAuth(request: Request, env: Env): Response | null {
  const expectedToken = env.CONTROL_PLANE_API_TOKEN;

  // CW-2: Fail Closed - If token is not configured, NEVER allow unauthenticated access.
  if (!expectedToken || expectedToken.trim() === "") {
    return errorResponse(
      "Control plane authentication token is not configured on the Worker (FAIL CLOSED).",
      503,
      "CONFIG_ERROR"
    );
  }

  const authHeader = request.headers.get("Authorization");
  if (!authHeader) {
    return errorResponse("Missing Authorization header.", 401, "UNAUTHORIZED");
  }

  const match = authHeader.match(/^Bearer\s+(.+)$/i);
  if (!match) {
    return errorResponse("Malformed Authorization header.", 401, "UNAUTHORIZED");
  }

  const token = match[1].trim();
  if (token !== expectedToken) {
    return errorResponse("Invalid API token.", 401, "UNAUTHORIZED");
  }

  // CW-6: Strict Project ID and Namespace validation against Worker environment
  if (env.CONTROL_PLANE_PROJECT_ID) {
    const projHeader = request.headers.get("X-Project-Id");
    if (!projHeader || projHeader.trim() !== env.CONTROL_PLANE_PROJECT_ID) {
      return errorResponse("Missing or mismatched X-Project-Id header.", 403, "FORBIDDEN");
    }
  }

  if (env.CONTROL_PLANE_NAMESPACE) {
    const nsHeader = request.headers.get("X-Namespace");
    if (!nsHeader || nsHeader.trim() !== env.CONTROL_PLANE_NAMESPACE) {
      return errorResponse("Missing or mismatched X-Namespace header.", 403, "FORBIDDEN");
    }
  }

  return null;
}

export function sanitizeOwner(rawOwner: string): string | null {
  if (!rawOwner) return null;
  const cleaned = rawOwner.trim().toLowerCase();

  // Validate character set and length: 1-128 chars, alphanumeric, '.', '_', '-'
  if (cleaned.length < 1 || cleaned.length > 128) {
    return null;
  }

  // Reject path traversal tokens
  if (
    cleaned.includes("..") ||
    cleaned.includes("/") ||
    cleaned.includes("\\") ||
    cleaned.includes("%2e") ||
    cleaned.includes("%2f")
  ) {
    return null;
  }

  if (!/^[a-z0-9_.-]+$/.test(cleaned)) {
    return null;
  }

  return cleaned;
}

export function assertNoSecrets(obj: any): string | null {
  if (!obj || typeof obj !== "object") return null;

  if (Array.isArray(obj)) {
    for (const item of obj) {
      const err = assertNoSecrets(item);
      if (err) return err;
    }
    return null;
  }

  for (const [key, value] of Object.entries(obj)) {
    const kLower = key.toLowerCase();
    if (FORBIDDEN_SECRET_KEYS.some((f) => kLower.includes(f))) {
      return `Payload contains forbidden credential field '${key}'. Secrets must NEVER be sent to Cloudflare.`;
    }
    const err = assertNoSecrets(value);
    if (err) return err;
  }

  return null;
}
