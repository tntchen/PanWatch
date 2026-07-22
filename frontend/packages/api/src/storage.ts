/**
 * 用户作用域 localStorage 封装（MT-P4 · docs/17 §R1 localStorage 串味）。
 *
 * 键分三类：
 * - 会话键（token / token_expires / panwatch_auth_identity）：全局，不走本封装
 *   （同浏览器同时只登录一个账号）。
 * - 设备级键（如 panwatch_upgrade_dismissed_version）：全局，不走本封装。
 * - 偏好/业务缓存键（主题、各页筛选条件、trace_id、onboarding 等）：经本封装按
 *   `panwatch:u:{username|anon}:{key}` 隔离；未登录时落 anon 作用域，
 *   登录后由 migrateAnonKeysToUser 迁移到用户作用域。
 *
 * 读路径带两级兜底：用户作用域 → anon 作用域 → 旧版未隔离键（升级兼容）；
 * 写入只写当前作用域并顺手清除旧版未隔离键，保证单用户场景行为等价。
 */

const IDENTITY_KEY = 'panwatch_auth_identity'
const USER_KEY_PREFIX = 'panwatch:u:'
const ANON_SCOPE = 'anon'

interface StoredIdentity {
  user_id?: number
  tenant_id?: number
  username?: string
}

/**
 * 当前存储作用域：已登录为用户名，未登录为 anon。
 * 同步可读：身份标记在登录/刷新 /auth/me 时已持久化到 localStorage。
 */
export function getStorageScope(): string {
  try {
    const raw = localStorage.getItem(IDENTITY_KEY)
    if (!raw) return ANON_SCOPE
    const parsed = JSON.parse(raw) as StoredIdentity | null
    const name = typeof parsed?.username === 'string' ? parsed.username.trim() : ''
    return name || ANON_SCOPE
  } catch {
    return ANON_SCOPE
  }
}

/** 用户作用域完整键名。 */
export function scopedStorageKey(key: string, scope?: string): string {
  return `${USER_KEY_PREFIX}${scope ?? getStorageScope()}:${key}`
}

/** 读：用户作用域 → anon 作用域 → 旧版未隔离键。 */
export function scopedGet(key: string): string | null {
  const scope = getStorageScope()
  if (scope !== ANON_SCOPE) {
    const scoped = localStorage.getItem(scopedStorageKey(key, scope))
    if (scoped !== null) return scoped
  }
  const anon = localStorage.getItem(scopedStorageKey(key, ANON_SCOPE))
  if (anon !== null) return anon
  return localStorage.getItem(key)
}

/** 写：只写当前作用域，并清除旧版未隔离键完成收敛。 */
export function scopedSet(key: string, value: string): void {
  localStorage.setItem(scopedStorageKey(key), value)
  if (localStorage.getItem(key) !== null) {
    localStorage.removeItem(key)
  }
}

/** 删：当前作用域 + 旧版未隔离键（anon 残留由登录迁移/账号切换清理）。 */
export function scopedRemove(key: string): void {
  localStorage.removeItem(scopedStorageKey(key))
  localStorage.removeItem(key)
}

/** 登录后调用：把 anon 作用域的全部键迁移到指定用户作用域（目标已存在则不覆盖）。 */
export function migrateAnonKeysToUser(username: string): void {
  const scope = username.trim()
  if (!scope || scope === ANON_SCOPE) return
  const anonPrefix = `${USER_KEY_PREFIX}${ANON_SCOPE}:`
  for (const key of Object.keys(localStorage)) {
    if (!key.startsWith(anonPrefix)) continue
    const target = `${USER_KEY_PREFIX}${scope}:${key.slice(anonPrefix.length)}`
    if (localStorage.getItem(target) === null) {
      const value = localStorage.getItem(key)
      if (value !== null) localStorage.setItem(target, value)
    }
    localStorage.removeItem(key)
  }
}

/** 是否用户作用域键（含 anon），供 client.ts 账号切换清理判断。 */
export function isUserScopedKey(key: string): boolean {
  return key.startsWith(USER_KEY_PREFIX)
}

/** 是否 anon 作用域键。 */
export function isAnonScopedKey(key: string): boolean {
  return key.startsWith(`${USER_KEY_PREFIX}${ANON_SCOPE}:`)
}
