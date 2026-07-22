import { useState, useEffect, useCallback } from 'react'
import { Users, UserPlus, KeyRound, ShieldCheck, User as UserIcon, Eye, EyeOff } from 'lucide-react'
import { authApi, type AdminUserRow, type AuthRole } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { useAuthUser } from '@/hooks/use-auth-user'

interface InviteForm {
  username: string
  password: string
  tenant_name: string
  role: AuthRole
  quota_shared_with_admin: boolean
}

const emptyInviteForm: InviteForm = {
  username: '',
  password: '',
  tenant_name: '',
  role: 'user',
  quota_shared_with_admin: true,
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 用户管理页（管理员专属，T12 邀请制）：
 * 用户列表 / 邀请创建（含 T13 配额共享开关）/ 停用启用 / 重置密码。
 * 命名规避：用户域用 AuthUser/AdminUserRow，不复用持仓域 Account。
 */
export default function UsersPage() {
  const { toast } = useToast()
  const me = useAuthUser()
  const [users, setUsers] = useState<AdminUserRow[]>([])
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)

  const [inviteOpen, setInviteOpen] = useState(false)
  const [inviteForm, setInviteForm] = useState<InviteForm>(emptyInviteForm)
  const [inviteSaving, setInviteSaving] = useState(false)
  const [showInvitePwd, setShowInvitePwd] = useState(false)

  const [resetTarget, setResetTarget] = useState<AdminUserRow | null>(null)
  const [resetPassword, setResetPassword] = useState('')
  const [resetSaving, setResetSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await authApi.listUsers()
      setUsers(Array.isArray(data) ? data : [])
      setForbidden(false)
    } catch (e) {
      const msg = e instanceof Error ? e.message : ''
      if (msg.includes('403') || msg.includes('权限') || msg.includes(' forb')) {
        setForbidden(true)
      } else {
        toast(msg || '加载用户列表失败', 'error')
      }
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!inviteForm.username || !inviteForm.password) return
    if (inviteForm.password.length < 6) {
      toast('初始密码长度至少 6 位', 'error')
      return
    }
    setInviteSaving(true)
    try {
      await authApi.createUser({
        username: inviteForm.username.trim(),
        password: inviteForm.password,
        role: inviteForm.role,
        tenant_name: inviteForm.tenant_name.trim() || `${inviteForm.username.trim()} 的租户`,
        quota_shared_with_admin: inviteForm.quota_shared_with_admin,
      })
      toast(`已创建用户 ${inviteForm.username}`, 'success')
      setInviteOpen(false)
      setInviteForm(emptyInviteForm)
      await load()
    } catch (err) {
      toast(err instanceof Error ? err.message : '创建失败', 'error')
    } finally {
      setInviteSaving(false)
    }
  }

  const patchUser = async (row: AdminUserRow, payload: Parameters<typeof authApi.updateUser>[1], okMsg: string) => {
    try {
      await authApi.updateUser(row.id, payload)
      toast(okMsg, 'success')
      await load()
    } catch (err) {
      toast(err instanceof Error ? err.message : '操作失败', 'error')
      await load()
    }
  }

  const handleResetPassword = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!resetTarget) return
    if (resetPassword.length < 6) {
      toast('新密码长度至少 6 位', 'error')
      return
    }
    setResetSaving(true)
    try {
      await authApi.updateUser(resetTarget.id, { reset_password: resetPassword })
      toast(`已重置 ${resetTarget.username} 的密码`, 'success')
      setResetTarget(null)
      setResetPassword('')
    } catch (err) {
      toast(err instanceof Error ? err.message : '重置失败', 'error')
    } finally {
      setResetSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="w-6 h-6 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
      </div>
    )
  }

  if (forbidden) {
    return (
      <div className="card p-8 text-center">
        <ShieldCheck className="w-8 h-8 text-muted-foreground mx-auto mb-3" />
        <div className="text-[15px] font-medium text-foreground">仅管理员可访问</div>
        <div className="text-[13px] text-muted-foreground mt-1">用户管理为管理员专属功能（T12 邀请制）</div>
      </div>
    )
  }

  return (
    <div className="space-y-4 max-w-4xl">
      {/* 页头 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-foreground flex items-center gap-2">
            <Users className="w-5 h-5 text-primary" />
            用户管理
          </h1>
          <p className="text-[13px] text-muted-foreground mt-1">
            邀请制开户（T12）：仅管理员可创建用户；「与管理员共享配额」决定该租户使用托管 AI 服务还是自建密钥（T13）。
          </p>
        </div>
        <Button onClick={() => setInviteOpen(true)} className="gap-1.5">
          <UserPlus className="w-4 h-4" />
          邀请用户
        </Button>
      </div>

      {/* 用户列表 */}
      <div className="card divide-y divide-border/60">
        {users.length === 0 && (
          <div className="p-8 text-center text-[13px] text-muted-foreground">暂无用户</div>
        )}
        {users.map(u => {
          const isSelf = me?.id === u.id
          return (
            <div key={u.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="w-9 h-9 rounded-full bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center flex-shrink-0">
                <UserIcon className="w-4 h-4 text-white" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-[14px] font-medium text-foreground">{u.username}</span>
                  {isSelf && <span className="text-[11px] text-muted-foreground">（我）</span>}
                  <Badge variant={u.role === 'admin' ? 'default' : 'secondary'}>
                    {u.role === 'admin' ? '管理员' : '用户'}
                  </Badge>
                  <Badge variant={u.is_active ? 'success' : 'outline'}>
                    {u.is_active ? '启用中' : '已停用'}
                  </Badge>
                </div>
                <div className="text-[12px] text-muted-foreground mt-0.5">
                  租户：{u.tenant_name} · 最近登录：{formatTime(u.last_login_at)}
                </div>
              </div>

              {/* 配额共享开关（T13） */}
              <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
                共享配额
                <Switch
                  checked={u.quota_shared_with_admin}
                  disabled={u.role === 'admin'}
                  onCheckedChange={v =>
                    patchUser(u, { quota_shared_with_admin: v }, v ? '已开启配额共享' : '已关闭配额共享')
                  }
                />
              </label>

              <div className="flex items-center gap-1.5">
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1 text-[12px]"
                  onClick={() => {
                    setResetTarget(u)
                    setResetPassword('')
                  }}
                >
                  <KeyRound className="w-3.5 h-3.5" />
                  重置密码
                </Button>
                {!isSelf && (
                  <Button
                    variant={u.is_active ? 'secondary' : 'default'}
                    size="sm"
                    className="text-[12px]"
                    onClick={() =>
                      patchUser(u, { is_active: !u.is_active }, u.is_active ? `已停用 ${u.username}` : `已启用 ${u.username}`)
                    }
                  >
                    {u.is_active ? '停用' : '启用'}
                  </Button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* 邀请用户弹窗 */}
      <Dialog open={inviteOpen} onOpenChange={v => { setInviteOpen(v); if (!v) setInviteForm(emptyInviteForm) }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <UserPlus className="w-4 h-4 text-primary" />
              邀请用户
            </DialogTitle>
            <DialogDescription>创建后把用户名与初始密码告知对方，首次登录后建议修改密码。</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleInvite} className="space-y-4">
            <div>
              <Label>用户名</Label>
              <Input
                value={inviteForm.username}
                onChange={e => setInviteForm(f => ({ ...f, username: e.target.value }))}
                placeholder="登录用户名"
                autoFocus
              />
            </div>
            <div>
              <Label>初始密码</Label>
              <div className="relative">
                <Input
                  type={showInvitePwd ? 'text' : 'password'}
                  value={inviteForm.password}
                  onChange={e => setInviteForm(f => ({ ...f, password: e.target.value }))}
                  placeholder="至少 6 位"
                  className="pr-10"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
                  onClick={() => setShowInvitePwd(v => !v)}
                >
                  {showInvitePwd ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </Button>
              </div>
            </div>
            <div>
              <Label>租户名称</Label>
              <Input
                value={inviteForm.tenant_name}
                onChange={e => setInviteForm(f => ({ ...f, tenant_name: e.target.value }))}
                placeholder="留空则默认为「用户名 的租户」"
              />
            </div>
            <div>
              <Label>角色</Label>
              <div className="flex gap-2 mt-1">
                {([['user', '用户'], ['admin', '管理员']] as [AuthRole, string][]).map(([role, label]) => (
                  <button
                    key={role}
                    type="button"
                    onClick={() => setInviteForm(f => ({ ...f, role }))}
                    className={`flex-1 px-3 py-2 rounded-xl text-[13px] font-medium border transition-colors ${
                      inviteForm.role === role
                        ? 'border-primary/40 bg-primary/10 text-primary'
                        : 'border-border text-muted-foreground hover:bg-accent/60'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <label className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2.5">
              <span className="text-[13px]">
                <span className="text-foreground font-medium">与管理员共享配额</span>
                <span className="block text-[12px] text-muted-foreground mt-0.5">
                  开启：使用管理员托管的 AI 服务（不见密钥）；关闭：该租户自建 AI 服务密钥
                </span>
              </span>
              <Switch
                checked={inviteForm.quota_shared_with_admin}
                onCheckedChange={v => setInviteForm(f => ({ ...f, quota_shared_with_admin: v }))}
              />
            </label>
            <Button type="submit" className="w-full" disabled={inviteSaving || !inviteForm.username || !inviteForm.password}>
              {inviteSaving ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                '创建用户'
              )}
            </Button>
          </form>
        </DialogContent>
      </Dialog>

      {/* 重置密码弹窗 */}
      <Dialog open={!!resetTarget} onOpenChange={v => { if (!v) { setResetTarget(null); setResetPassword('') } }}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <KeyRound className="w-4 h-4 text-primary" />
              重置密码
            </DialogTitle>
            <DialogDescription>
              为 {resetTarget?.username} 设置新密码，重置后其旧登录态全部失效。
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleResetPassword} className="space-y-4">
            <div>
              <Label>新密码</Label>
              <Input
                type="password"
                value={resetPassword}
                onChange={e => setResetPassword(e.target.value)}
                placeholder="至少 6 位"
                autoFocus
              />
            </div>
            <Button type="submit" className="w-full" disabled={resetSaving || resetPassword.length < 6}>
              {resetSaving ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                '确认重置'
              )}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
