import { ApiError } from "@/lib/api";

export function ApiErrorNotice({ error }: { error: unknown }) {
  const isApi = error instanceof ApiError;
  const message = error instanceof Error ? error.message : "未知错误";

  return (
    <section className="rounded-lg border border-rose-900/60 bg-rose-950/30 p-4 text-sm">
      <p className="font-medium text-rose-300">{message}</p>
      {isApi && error.fieldErrors.length > 0 && (
        <ul className="mt-2 space-y-1 font-mono text-xs text-rose-400">
          {error.fieldErrors.map((item) => (
            <li key={item.field}>
              {item.field}: {item.message}
            </li>
          ))}
        </ul>
      )}
      {!isApi && <p className="mt-1 text-xs text-rose-400/80">后端可能没起来,试试 make up</p>}
    </section>
  );
}

