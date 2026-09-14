"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, login } from "@/lib/api";
import { forgetIdentity } from "@/lib/identity";

/** 演示账号。真实部署里当然不会把这个印在登录页上 —— 这里是为了能直接点开试用。 */
const DEMO_ACCOUNTS = [
  { username: "supervisor-01", role: "主管", hint: "签批退款申请" },
  { username: "finance-01", role: "财务", hint: "执行退款(与主管不同角色)" },
  { username: "operator-01", role: "客服", hint: "发起提议" },
];

export default function LoginPage() {
  const [username, setUsername] = useState("supervisor-01");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      // 身份可能变了,缓存必须作废,否则页面上还挂着上一个人的名字
      forgetIdentity();
      router.push("/");
      router.refresh();
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.message : "登录失败,请稍后再试",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-md space-y-6 py-10">
      <header className="space-y-2">
        <h1 className="text-xl font-semibold text-ink">登录后签署</h1>
        <p className="text-sm leading-relaxed text-muted">
          审批是记名动作。系统需要知道签字的人是谁 —— 执行体身份不由调用方自称,
          更不由模型提供。
        </p>
      </header>

      <form onSubmit={submit} className="space-y-4 rounded-xl border border-line bg-surface p-5">
        <div className="space-y-1.5">
          <label htmlFor="username" className="block text-sm text-muted">
            用户名
          </label>
          <input
            id="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            className="w-full rounded-md border border-line-strong bg-canvas px-3 py-2 font-mono text-sm text-ink outline-none focus:border-brand"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="password" className="block text-sm text-muted">
            口令
          </label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            className="w-full rounded-md border border-line-strong bg-canvas px-3 py-2 text-sm text-ink outline-none focus:border-brand"
          />
        </div>

        {error ? (
          <p
            role="alert"
            className="rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-300"
          >
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="w-full rounded-md bg-brand px-4 py-2 text-sm font-medium text-canvas hover:opacity-90 disabled:opacity-40"
        >
          {busy ? "登录中…" : "登录"}
        </button>
      </form>

      <section className="space-y-2 rounded-xl border border-line bg-surface/60 p-4">
        <h2 className="text-xs tracking-wide text-subtle uppercase">演示账号</h2>
        <ul className="space-y-1 text-xs text-muted">
          {DEMO_ACCOUNTS.map((account) => (
            <li key={account.username} className="flex flex-wrap items-baseline gap-2">
              <button
                type="button"
                onClick={() => setUsername(account.username)}
                className="font-mono text-brand hover:underline"
              >
                {account.username}
              </button>
              <span className="text-subtle">{account.role}</span>
              <span className="text-subtle">·</span>
              <span className="text-subtle">{account.hint}</span>
            </li>
          ))}
        </ul>
        <p className="text-xs text-subtle">
          口令由 <span className="font-mono">DEMO_PASSWORD</span> 决定,默认{" "}
          <span className="font-mono">guardrail-demo</span>。
        </p>
      </section>
    </div>
  );
}
