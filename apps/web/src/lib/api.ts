import type {
  AuditListResponse,
  CancelOrderRequest,
  ExecuteRunResponse,
  HealthResponse,
  OrderListResponse,
  OrderStatus,
  OrderView,
  Plan,
  ReadyResponse,
  RejectTicketRequest,
  RetryStepResponse,
  RunDetail,
  RunListResponse,
  RunStatus,
  TicketListResponse,
  TicketStatus,
  TicketSummary,
  ToolDescription,
  ToolInvocationResponse,
} from "@guardrail/contracts";

/** 浏览器侧地址:容器里必须能从宿主机访问,所以是 localhost:端口 */
export const PUBLIC_API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** 服务端侧地址:容器内直连 compose 网络,免去绕宿主机一跳 */
function resolveBaseUrl(): string {
  if (typeof window === "undefined" && process.env.INTERNAL_API_BASE_URL) {
    return process.env.INTERNAL_API_BASE_URL;
  }
  return PUBLIC_API_BASE_URL;
}

type ErrorPayload = {
  error?: { code?: string; message?: string; context?: Record<string, unknown> };
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly context: Record<string, unknown>;

  constructor(status: number, code: string, message: string, context: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.context = context;
  }

  /** 工具参数校验失败时的字段级错误,直接给表单标红用。 */
  get fieldErrors(): { field: string; message: string }[] {
    const errors = this.context.errors;
    return Array.isArray(errors) ? (errors as { field: string; message: string }[]) : [];
  }
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  actor?: string;
};

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.actor) headers["X-Actor"] = options.actor;

  const response = await fetch(`${resolveBaseUrl()}${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as ErrorPayload | null;
    throw new ApiError(
      response.status,
      payload?.error?.code ?? "unknown_error",
      payload?.error?.message ?? `请求失败(${response.status})`,
      payload?.error?.context ?? {},
    );
  }
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

// --- 平台 ---

export const getHealth = () => request<HealthResponse>("/health");
export const getReady = () => request<ReadyResponse>("/ready");

// --- 订单 ---

export const listOrders = (params: { status?: OrderStatus; limit?: number; offset?: number } = {}) =>
  request<OrderListResponse>(`/api/orders${query(params)}`);

export const getOrder = (orderId: number) => request<OrderView>(`/api/orders/${orderId}`);

export const orderAction = (orderId: number, action: "pay" | "ship" | "complete", actor?: string) =>
  request<OrderView>(`/api/orders/${orderId}/${action}`, { method: "POST", actor });

export const cancelOrder = (orderId: number, body: CancelOrderRequest, actor?: string) =>
  request<OrderView>(`/api/orders/${orderId}/cancel`, { method: "POST", body, actor });

// --- 售后 ---

export const listTickets = (params: { status?: TicketStatus; limit?: number; offset?: number } = {}) =>
  request<TicketListResponse>(`/api/tickets${query(params)}`);

export const approveTicket = (ticketId: number, actor?: string) =>
  request<TicketSummary>(`/api/tickets/${ticketId}/approve`, { method: "POST", actor });

export const refundTicket = (ticketId: number, actor?: string) =>
  request<TicketSummary>(`/api/tickets/${ticketId}/refund`, { method: "POST", actor });

export const rejectTicket = (ticketId: number, body: RejectTicketRequest, actor?: string) =>
  request<TicketSummary>(`/api/tickets/${ticketId}/reject`, { method: "POST", body, actor });

export const closeTicket = (ticketId: number, actor?: string) =>
  request<TicketSummary>(`/api/tickets/${ticketId}/close`, { method: "POST", actor });

// --- 工具与提议 ---

export const listTools = () => request<ToolDescription[]>("/api/tools");

export const invokeTool = (toolName: string, args: Record<string, unknown>, actor?: string) =>
  request<ToolInvocationResponse>(`/api/tools/${toolName}/invoke`, {
    method: "POST",
    body: { arguments: args },
    actor,
  });

export const draftProposal = (intent: string) =>
  request<Plan>("/api/planner/draft", { method: "POST", body: { intent } });

// --- 治理层:执行记录与审计 ---

export const listRuns = (params: { status?: RunStatus; limit?: number; offset?: number } = {}) =>
  request<RunListResponse>(`/api/runs${query(params)}`);

export const getRun = (runUid: string) => request<RunDetail>(`/api/runs/${runUid}`);

/**
 * 登记一次执行(此时无副作用)。计划里的每一步都带上 seq / 依赖 / 参数,
 * 参数里的 `{"$ref": "1.x"}` 由后端在执行前解析 —— 前端原样提交,不做二次解释。
 */
export const createRun = (body: {
  goal: string;
  steps: { seq: number; tool: string; args: Record<string, unknown>; requires_approval?: boolean; depends_on?: number[] }[];
  actor?: string;
}) =>
  request<RunDetail>("/api/runs", {
    method: "POST",
    body: { goal: body.goal, steps: body.steps },
    actor: body.actor,
  });

export const executeRun = (runUid: string) =>
  request<ExecuteRunResponse>(`/api/runs/${runUid}/execute`, { method: "POST" });

export const retryStep = (runUid: string, seq: number) =>
  request<RetryStepResponse>(`/api/runs/${runUid}/steps/${seq}/retry`, { method: "POST" });

/**
 * 批准一个挂起的步骤。**必须带审批人身份** —— 审计要记下"谁签的字"。
 * 而且这个身份不能等于发起执行的执行体:系统会拒绝自己批自己(职责分离)。
 */
export const approveStep = (runUid: string, seq: number, actor: string) =>
  request<RunDetail>(`/api/runs/${runUid}/steps/${seq}/approve`, { method: "POST", actor });

export const listAudit = (
  params: { run_uid?: string; trace_id?: string; tool_name?: string; limit?: number } = {},
) => request<AuditListResponse>(`/api/audit${query(params)}`);
