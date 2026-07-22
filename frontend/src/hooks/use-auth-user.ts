import { useState, useEffect } from 'react'
import { authApi, isAuthenticated, reconcileAuthIdentity, type AuthMe } from '@panwatch/api'

const EVENT = 'panwatch:auth-user-changed'

// 会话内内存缓存（不落 localStorage，避免身份过期残留，见 docs/25 §5.1）。
let cache: AuthMe | null = null
let inflight: Promise<AuthMe | null> | null = null

function emit(): void {
  window.dispatchEvent(new CustomEvent(EVENT))
}

/**
 * 拉取/刷新当前登录身份（/auth/me），并做账号切换检测：
 * 与上次身份不一致时清空上个账号的业务 localStorage 键。
 */
export function refreshAuthUser(): Promise<AuthMe | null> {
  if (!isAuthenticated()) {
    cache = null
    emit()
    return Promise.resolve(null)
  }
  if (!inflight) {
    inflight = authApi.me()
      .then(me => {
        cache = me
        reconcileAuthIdentity({ user_id: me.id, tenant_id: me.tenant_id, username: me.username })
        emit()
        return me
      })
      .catch(() => cache)
      .finally(() => {
        inflight = null
      })
  }
  return inflight
}

/** 当前登录用户（含角色/租户/配额共享标记）。未登录或加载中返回 null。 */
export function useAuthUser(): AuthMe | null {
  const [user, setUser] = useState<AuthMe | null>(cache)
  useEffect(() => {
    let alive = true
    refreshAuthUser().then(u => {
      if (alive) setUser(u)
    })
    const onChange = () => setUser(cache)
    window.addEventListener(EVENT, onChange)
    return () => {
      alive = false
      window.removeEventListener(EVENT, onChange)
    }
  }, [])
  return user
}
