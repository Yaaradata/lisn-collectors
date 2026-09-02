import { GoogleAuth } from "google-auth-library";

const LOCAL_BACKEND = "http://127.0.0.1:8090";

function resolveBackendUrl(): string {
  return (
    process.env.AGENT_BACKEND_URL?.replace(/\/$/, "") ??
    process.env.COLLECTOR_AGENT_URL?.replace(/\/$/, "") ??
    LOCAL_BACKEND
  );
}

function isLocalBackend(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host === "127.0.0.1";
  } catch {
    return /localhost|127\.0\.0\.1/.test(url);
  }
}

async function backendAuthHeaders(
  audience: string,
): Promise<Record<string, string>> {
  if (isLocalBackend(audience)) {
    return {};
  }
  const auth = new GoogleAuth();
  const client = await auth.getIdTokenClient(audience);
  const raw = await client.getRequestHeaders();
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (value != null) out[key] = String(value);
  }
  return out;
}

/**
 * Forward a browser request to the agent backend from the Next.js server.
 * Cloud Run → Cloud Run uses the UI service account identity token; local dev
 * skips auth when AGENT_BACKEND_URL points at localhost.
 */
export async function proxyToAgentBackend(
  pathSegments: string[],
  request: Request,
): Promise<Response> {
  const backendBase = resolveBackendUrl();
  const path = pathSegments.map((s) => encodeURIComponent(s)).join("/");
  const incoming = new URL(request.url);
  const target = `${backendBase}/${path}${incoming.search}`;

  const headers = new Headers(await backendAuthHeaders(backendBase));
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);

  const init: RequestInit = {
    method: request.method,
    headers,
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(target, init);
  const responseHeaders = new Headers();
  const upstreamType = upstream.headers.get("content-type");
  if (upstreamType) responseHeaders.set("content-type", upstreamType);

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
