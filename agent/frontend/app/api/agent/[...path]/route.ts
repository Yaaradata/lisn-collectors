import { proxyToAgentBackend } from "@/lib/server/agent-proxy";

export const runtime = "nodejs";

/** Chat + multi-system diagnose can exceed default route limits. */
export const maxDuration = 300;

type RouteContext = { params: Promise<{ path: string[] }> };

async function handle(request: Request, context: RouteContext): Promise<Response> {
  const { path } = await context.params;
  return proxyToAgentBackend(path, request);
}

export function GET(request: Request, context: RouteContext) {
  return handle(request, context);
}

export function POST(request: Request, context: RouteContext) {
  return handle(request, context);
}

export function DELETE(request: Request, context: RouteContext) {
  return handle(request, context);
}
