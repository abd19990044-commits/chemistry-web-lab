import { Env } from "./types";

// ===========================================================================
// Standardized Error & Response Helpers with Configurable CORS
// ===========================================================================

export function getCorsHeaders(request?: Request, env?: Env): Record<string, string> {
  const headers: Record<string, string> = {
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Project-Id, X-Namespace",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  };

  if (!request) {
    return headers;
  }

  const origin = request.headers.get("Origin");
  if (!origin) {
    return headers;
  }

  const configured = (env?.ALLOWED_ORIGINS || env?.CHEMISTRY_LAB_ALLOWED_ORIGINS || "").trim();
  if (configured) {
    const allowlist = configured.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (allowlist.includes(origin.toLowerCase()) || allowlist.includes("*")) {
      headers["Access-Control-Allow-Origin"] = origin;
      headers["Vary"] = "Origin";
    }
  } else {
    // Default fallback: echo request origin with Vary: Origin
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Vary"] = "Origin";
  }

  return headers;
}

export function jsonResponse(
  data: any,
  status = 200,
  request?: Request,
  env?: Env
): Response {
  const cors = getCorsHeaders(request, env);
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...cors,
    },
  });
}

export function errorResponse(
  message: string,
  status = 400,
  code = "BAD_REQUEST",
  request?: Request,
  env?: Env
): Response {
  return jsonResponse(
    {
      ok: false,
      error: {
        code,
        message,
      },
    },
    status,
    request,
    env
  );
}
