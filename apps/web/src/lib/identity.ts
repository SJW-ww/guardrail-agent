"use client";

import { useEffect, useState } from "react";

import { ApiError, getMe, type Principal } from "@/lib/api";

/**
 * 当前登录身份。
 *
 * 页面顶部和审批按钮都要用同一个身份,所以只问一次后端 —— 结果缓存在模块级别。
 * 「未登录」是一个**正常状态**(本地演示就是这样),不当作错误往上抛。
 */
let cached: Principal | null | undefined;
let inflight: Promise<Principal | null> | null = null;

export function currentIdentity(): Promise<Principal | null> {
  if (cached !== undefined) return Promise.resolve(cached);
  inflight ??= getMe()
    .then((session) => {
      cached = session.principal;
      return cached;
    })
    .catch((cause: unknown) => {
      if (cause instanceof ApiError && cause.status === 401) {
        cached = null;
        return null;
      }
      throw cause;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

export function forgetIdentity(): void {
  cached = undefined;
  inflight = null;
}

export function useIdentity(): { loaded: boolean; principal: Principal | null } {
  const [state, setState] = useState<{ loaded: boolean; principal: Principal | null }>({
    loaded: false,
    principal: null,
  });

  useEffect(() => {
    let alive = true;
    currentIdentity()
      .then((principal) => {
        if (alive) setState({ loaded: true, principal });
      })
      .catch(() => {
        if (alive) setState({ loaded: true, principal: null });
      });
    return () => {
      alive = false;
    };
  }, []);

  return state;
}
