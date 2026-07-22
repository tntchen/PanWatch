import type { AuthMe } from '@panwatch/api'

/**
 * Settings 页两态视图决策（MT-P4 前端配置属主化）。
 *
 * 三种视角：
 * - admin / 单租户模式（身份未明，me 为 null）：现状全量管理 UI。
 * - 配额共享租户（quota_shared_with_admin=true）：管理员托管项只读，无自建入口。
 * - 非共享普通租户：完整自建表单，可增删改自己的服务/渠道。
 */
export interface TenantViewCtx {
  isAdmin: boolean
  quotaShared: boolean
}

export function tenantViewCtx(me: AuthMe | null): TenantViewCtx {
  // 身份未明（加载中或单租户直通）：按管理员处理，保持现状 UI 不变
  if (!me) return { isAdmin: true, quotaShared: false }
  const isAdmin = me.role === 'admin'
  return { isAdmin, quotaShared: !isAdmin && !!me.quota_shared_with_admin }
}

/** 该项对当前视角是否只读（管理员托管）。admin 视角永不只读。 */
export function isManagedItem(
  item: { is_managed?: boolean } | null | undefined,
  ctx: TenantViewCtx,
): boolean {
  return !ctx.isAdmin && !!item?.is_managed
}

/** 是否有「添加 AI 服务商」入口：admin 与非共享租户有，配额共享租户无。 */
export function canCreateAiService(ctx: TenantViewCtx): boolean {
  return ctx.isAdmin || !ctx.quotaShared
}
