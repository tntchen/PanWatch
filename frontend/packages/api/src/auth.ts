import { fetchAPI } from './client'

export interface AuthStatus {
  initialized: boolean
}

/** 用户角色：admin=管理员(实例所有者)，user=普通租户成员（T5 两级） */
export type AuthRole = 'admin' | 'user'

/** 认证用户（users 表行）。注意命名规避：不复用持仓域的 Account。 */
export interface AuthUser {
  id: number
  username: string
  role: AuthRole
  tenant_id: number
  tenant_name: string
}

/** /auth/me 返回：AuthUser + 配额共享标记（T13 两态） */
export interface AuthMe extends AuthUser {
  quota_shared_with_admin: boolean
}

/** 管理员用户列表行（GET /auth/users） */
export interface AdminUserRow extends AuthMe {
  is_active: boolean
  last_login_at?: string | null
  created_at?: string | null
}

export interface AuthTokenPayload {
  token: string
  expires_at?: string
  user?: AuthUser
}

export interface LoginPayload {
  username: string
  password: string
}

export interface ChangePasswordPayload {
  old_password: string
  new_password: string
}

/** 邀请制建用户（T12，admin-only） */
export interface AdminCreateUserPayload {
  username: string
  password: string
  role: AuthRole
  tenant_name: string
  quota_shared_with_admin: boolean
}

/** 管理员 PATCH /auth/users/{id} */
export interface AdminUpdateUserPayload {
  is_active?: boolean
  role?: AuthRole
  quota_shared_with_admin?: boolean
  reset_password?: string
}

export const authApi = {
  status: () => fetchAPI<AuthStatus>('/auth/status'),
  login: (payload: LoginPayload) =>
    fetchAPI<AuthTokenPayload>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  setup: (payload: LoginPayload) =>
    fetchAPI<AuthTokenPayload>('/auth/setup', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  me: () => fetchAPI<AuthMe>('/auth/me'),
  changePassword: (payload: ChangePasswordPayload) =>
    fetchAPI<null>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // ---- 管理员：用户管理（T12 邀请制） ----
  listUsers: () => fetchAPI<AdminUserRow[]>('/auth/users'),
  createUser: (payload: AdminCreateUserPayload) =>
    fetchAPI<AdminUserRow>('/auth/users', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateUser: (id: number, payload: AdminUpdateUserPayload) =>
    fetchAPI<AdminUserRow>(`/auth/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
}
