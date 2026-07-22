import { migrateAnonKeysToUser, isUserScopedKey, isAnonScopedKey } from './storage'

const API_BASE = '/api'
const DEFAULT_TIMEOUT_MS = 20000

interface ApiResponse<T> {
  code: number
  success?: boolean
  data: T
  message: string
}

export function getToken(): string | null {
  return localStorage.getItem('token')
}

/** 当前登录身份标记（user_id/tenant_id/username，用于账号切换检测与存储作用域，不落完整用户对象） */
const IDENTITY_KEY = 'panwatch_auth_identity'
/** 设备级偏好键：换账号时豁免清除（升级提示等）；主题已改为用户作用域键（见 storage.ts） */
const DEVICE_LEVEL_KEYS = new Set(['panwatch_upgrade_dismissed_version'])

export interface AuthIdentity {
  user_id: number
  tenant_id: number
  /** 用户名：作为 localStorage 用户作用域（MT-P4 键隔离） */
  username?: string
}

/**
 * 清空账号相关业务键（panwatch_* / stock_insight_* / token），设备级键豁免。
 * MT-P4 起：用户作用域键 panwatch:u:{username}:* 自隔离，切换账号时保留
 * （切回可恢复偏好）；仅清 anon 残留与旧版未隔离键。
 */
function purgeAccountScopedKeys(): void {
  for (const key of Object.keys(localStorage)) {
    if (key === IDENTITY_KEY || DEVICE_LEVEL_KEYS.has(key)) continue
    if (isUserScopedKey(key)) {
      if (isAnonScopedKey(key)) localStorage.removeItem(key)
      continue
    }
    if (
      key === 'token' ||
      key === 'token_expires' ||
      key.startsWith('panwatch') ||
      key.startsWith('stock_insight')
    ) {
      localStorage.removeItem(key)
    }
  }
}

/**
 * 账号切换检测（docs/25 §5.2）：登录成功 / App 启动拉取 /auth/me 后调用。
 * 与上次身份不一致 → 清空上个账号的共享业务缓存键；随后把 anon 作用域键
 * 迁移到当前用户作用域（MT-P4），再写入新身份。
 * 注意：会清掉旧 token，调用方须在此之后再写入新 token。
 */
export function reconcileAuthIdentity(next: AuthIdentity | null): void {
  const raw = localStorage.getItem(IDENTITY_KEY)
  let prev: AuthIdentity | null = null
  try {
    prev = raw ? (JSON.parse(raw) as AuthIdentity) : null
  } catch {
    prev = null
  }
  if (!next) {
    localStorage.removeItem(IDENTITY_KEY)
    return
  }
  if (prev && (prev.user_id !== next.user_id || prev.tenant_id !== next.tenant_id)) {
    purgeAccountScopedKeys()
  }
  if (next.username) {
    migrateAnonKeysToUser(next.username)
  }
  localStorage.setItem(IDENTITY_KEY, JSON.stringify(next))
}

export function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('token_expires')
  localStorage.removeItem(IDENTITY_KEY)
  window.location.href = '/login'
}

export function isAuthenticated(): boolean {
  const token = getToken()
  if (!token) return false

  const expires = localStorage.getItem('token_expires')
  if (expires && new Date(expires) < new Date()) {
    logout()
    return false
  }
  return true
}

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number
}

export async function fetchAPI<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  const headers: Record<string, string> = {}

  const token = getToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  if (options?.body) {
    headers['Content-Type'] = 'application/json'
  }

  const timeoutController = options?.signal ? null : new AbortController()
  const timeoutMs = typeof options?.timeoutMs === 'number' && options.timeoutMs > 0
    ? options.timeoutMs
    : DEFAULT_TIMEOUT_MS
  const timeoutId = timeoutController
    ? window.setTimeout(() => timeoutController.abort(), timeoutMs)
    : null

  let res: Response
  try {
    const { timeoutMs: _timeoutMs, ...requestOptions } = options || {}
    res = await fetch(`${API_BASE}${path}`, {
      ...requestOptions,
      headers: {
        ...headers,
        ...(requestOptions.headers as Record<string, string> | undefined),
      },
      signal: requestOptions.signal || timeoutController?.signal,
    })
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      throw new Error('请求超时，请稍后重试')
    }
    throw error
  } finally {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId)
    }
  }

  if (res.status === 401) {
    logout()
    throw new Error('登录已过期')
  }

  const body: ApiResponse<T> = await res.json().catch(() => ({
    code: res.status,
    data: null as T,
    message: `HTTP ${res.status}`,
  }))
  if (body.code !== 0 || body.success === false) {
    throw new Error(body.message || `HTTP ${res.status}`)
  }
  return body.data
}

export const apiClient = {
  request: fetchAPI,
}
