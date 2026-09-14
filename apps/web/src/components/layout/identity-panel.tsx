"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { logout as logoutRequest } from "@/lib/api";
import { forgetIdentity, useIdentity } from "@/lib/identity";

/**
 * 侧边栏底部的「我是谁」。
 *
 * 它存在的意义不是装饰:审批是**记名**动作,签之前必须一眼看到自己是谁,
 * 否则「换个人签字」这件事在界面上根本无法核对。
 */
export function IdentityPanel() {
  const { loaded, principal } = useIdentity();
  const [busy, setBusy] = useState(false);
  const router = useRouter();

  async function signOut() {
    setBusy(true);
    try {
      await logoutRequest();
    } finally {
      forgetIdentity();
      setBusy(false);
      router.push("/login");
      router.refresh();
    }
  }

  if (!loaded) {
    return <p className="px-3 py-2 text-xs text-subtle">身份确认中…</p>;
  }

  if (!principal) {
    return (
      <div className="space-y-2 px-3 py-2">
        <p className="text-xs text-subtle">未登录</p>
        <p className="text-[11px] leading-relaxed text-subtle">
          本地演示模式下以 <span className="font-mono">operator-01</span> 运行;
          生产环境未登录会被拒绝。
        </p>
        <Link
          href="/login"
          className="inline-block rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:border-line-strong hover:text-ink"
        >
          登录
        </Link>
      </div>
    );
  }

  const initial = principal.display_name.slice(0, 1);

  return (
    <div className="space-y-2 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-brand-dim text-xs font-medium text-brand">
          {initial}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-sm text-ink">{principal.display_name}</span>
          <span className="block truncate font-mono text-[11px] text-subtle">
            {principal.actor}
          </span>
        </span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="rounded bg-raised px-2 py-0.5 text-[11px] text-muted">
          {principal.role}
        </span>
        <button
          type="button"
          onClick={signOut}
          disabled={busy}
          className="text-xs text-subtle hover:text-ink disabled:opacity-50"
        >
          退出登录
        </button>
      </div>
    </div>
  );
}
