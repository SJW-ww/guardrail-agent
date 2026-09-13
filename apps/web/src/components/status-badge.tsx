import type { OrderStatus, RunStatus, StepStatus, TicketStatus } from "@guardrail/contracts";
import {
  ORDER_STATUS_LABELS,
  RUN_STATUS_LABELS,
  STEP_STATUS_LABELS,
  TICKET_STATUS_LABELS,
} from "@guardrail/contracts";

type Tone = "neutral" | "info" | "success" | "warn" | "danger";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "bg-neutral-800 text-neutral-300",
  info: "bg-sky-500/10 text-sky-300",
  success: "bg-emerald-500/10 text-emerald-300",
  warn: "bg-amber-500/10 text-amber-300",
  danger: "bg-rose-500/10 text-rose-300",
};

const ORDER_TONE: Record<OrderStatus, Tone> = {
  CREATED: "neutral",
  PAID: "info",
  SHIPPED: "info",
  COMPLETED: "success",
  CANCELLED: "danger",
};

const TICKET_TONE: Record<TicketStatus, Tone> = {
  PENDING: "warn",
  APPROVED: "info",
  REFUNDED: "success",
  REJECTED: "danger",
  CLOSED: "neutral",
};

export function Badge({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}>{label}</span>
  );
}

export function OrderBadge({ status }: { status: OrderStatus }) {
  return <Badge label={ORDER_STATUS_LABELS[status]} tone={ORDER_TONE[status]} />;
}

export function TicketBadge({ status }: { status: TicketStatus }) {
  return <Badge label={TICKET_STATUS_LABELS[status]} tone={TICKET_TONE[status]} />;
}

const RUN_TONE: Record<RunStatus, Tone> = {
  PENDING: "neutral",
  RUNNING: "info",
  WAITING_APPROVAL: "warn",
  SUCCEEDED: "success",
  FAILED: "danger",
  COMPENSATED: "neutral",
};

const STEP_TONE: Record<StepStatus, Tone> = {
  RUNNING: "info",
  SUCCEEDED: "success",
  FAILED: "danger",
};

export function RunBadge({ status }: { status: RunStatus }) {
  return <Badge label={RUN_STATUS_LABELS[status]} tone={RUN_TONE[status]} />;
}

export function StepBadge({ status }: { status: StepStatus }) {
  return <Badge label={STEP_STATUS_LABELS[status]} tone={STEP_TONE[status]} />;
}
