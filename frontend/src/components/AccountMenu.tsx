import { useState, useEffect, useRef } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { Moon, Sun, Monitor, Check, LogOut, User, Stethoscope, KeyRound, Users, type LucideIcon } from 'lucide-react'
import { isAuthenticated, logout } from '@panwatch/api'
import type { ThemeMode } from '@/hooks/use-theme'
import { useAvatar } from '@/hooks/use-avatar'
import { useAuthUser } from '@/hooks/use-auth-user'
import ChangePasswordDialog from '@/components/ChangePasswordDialog'

export interface AccountNavItem {
  to: string
  icon: LucideIcon
  label: string
}

const THEME_OPTIONS: { value: ThemeMode; icon: LucideIcon; label: string }[] = [
  { value: 'light', icon: Sun, label: '亮色' },
  { value: 'dark', icon: Moon, label: '暗色' },
  { value: 'system', icon: Monitor, label: '跟随系统' },
]

interface AccountMenuProps {
  /** 原“更多”里折叠的导航项(Agent / 历史 / 数据源 / 设置)。 */
  navItems: AccountNavItem[]
  mode: ThemeMode
  onSetMode: (m: ThemeMode) => void
  /** 打开「系统自检」弹窗(状态由上层 App 托管,避免桌面/移动两个实例重复)。 */
  onOpenSelfCheck: () => void
  /** 头像尺寸:桌面 md,移动端 sm。 */
  size?: 'sm' | 'md'
}

/**
 * 右上角头像区域 + 下拉菜单(参考 beecount-cloud):
 * 把原“更多”导航、主题色(亮/暗/跟随系统)、退出登录收进头像下拉
 * (查看日志 / GitHub 仍在外侧)。
 */
export default function AccountMenu({
  navItems,
  mode,
  onSetMode,
  onOpenSelfCheck,
  size = 'md',
}: AccountMenuProps) {
  const [open, setOpen] = useState(false)
  const [pwdOpen, setPwdOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)
  const location = useLocation()
  const avatar = useAvatar()
  const authUser = useAuthUser()
  // 仅在支持 hover 的设备(PC)启用悬停展开;触屏维持点击
  const [canHover] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(hover: hover)').matches,
  )

  // 点击外部关闭
  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      if (open && ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  // 路由变化时关闭
  useEffect(() => {
    setOpen(false)
  }, [location.pathname])

  const avatarSize = size === 'sm' ? 'w-6 h-6' : 'w-7 h-7'
  const iconSize = size === 'sm' ? 'w-3.5 h-3.5' : 'w-4 h-4'

  return (
    <div
      className="relative"
      ref={ref}
      onMouseEnter={canHover ? () => setOpen(true) : undefined}
      onMouseLeave={canHover ? () => setOpen(false) : undefined}
    >
      <button
        onClick={() => setOpen(v => !v)}
        className={`${avatarSize} rounded-full overflow-hidden bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center shadow-sm ring-1 transition-all ${
          open ? 'ring-primary/50' : 'ring-border/40 hover:ring-primary/40'
        }`}
        title="账户与设置"
        aria-label="账户与设置"
      >
        {avatar ? (
          <img src={avatar} alt="头像" className="w-full h-full object-cover" />
        ) : (
          <User className={`${iconSize} text-white`} />
        )}
      </button>

      {open && (
        // top-full + pt-2:用透明内边距桥接头像与菜单,hover 移入不断开
        <div className="absolute right-0 top-full pt-2 z-50">
          <div className="w-52 rounded-xl border border-border/60 bg-card/95 backdrop-blur p-1.5 shadow-xl">
          {/* 当前用户身份行:用户名 + 角色徽标 + 租户 */}
          {authUser && (
            <>
              <div className="flex items-center gap-2.5 px-2.5 py-2">
                <div className="w-8 h-8 rounded-full overflow-hidden bg-gradient-to-br from-primary to-primary/70 flex items-center justify-center flex-shrink-0">
                  {avatar ? (
                    <img src={avatar} alt="头像" className="w-full h-full object-cover" />
                  ) : (
                    <User className="w-4 h-4 text-white" />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px] font-medium text-foreground truncate">{authUser.username}</span>
                    <span
                      className={`flex-shrink-0 px-1.5 py-0.5 rounded-md text-[10px] font-medium ${
                        authUser.role === 'admin'
                          ? 'bg-primary/10 text-primary'
                          : 'bg-secondary text-secondary-foreground'
                      }`}
                    >
                      {authUser.role === 'admin' ? '管理员' : '用户'}
                    </span>
                  </div>
                  <div className="text-[11px] text-muted-foreground truncate">{authUser.tenant_name}</div>
                </div>
              </div>
              <div className="my-1 h-px bg-border/50" />
            </>
          )}

          {/* 原“更多”导航 */}
          {navItems.map(({ to, icon: Icon, label }) => {
            const isActive = location.pathname.startsWith(to)
            return (
              <NavLink
                key={to}
                to={to}
                onClick={() => setOpen(false)}
                className={`flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                  isActive
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
              </NavLink>
            )
          })}

          {/* 管理员专属:用户管理(T12 邀请制,仅 role==admin 可见) */}
          {authUser?.role === 'admin' && (
            <NavLink
              to="/users"
              onClick={() => setOpen(false)}
              className={`flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                location.pathname.startsWith('/users')
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
              }`}
            >
              <Users className="w-3.5 h-3.5" />
              用户管理
            </NavLink>
          )}

          <div className="my-1 h-px bg-border/50" />

          {/* 主题色:亮 / 暗 / 跟随系统 */}
          <div className="px-2.5 pt-0.5 pb-1 text-[11px] text-muted-foreground">主题</div>
          {THEME_OPTIONS.map(({ value, icon: Icon, label }) => {
            const active = mode === value
            return (
              <button
                key={value}
                onClick={() => onSetMode(value)}
                className={`flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] transition-colors ${
                  active
                    ? 'text-foreground bg-accent/40'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
                {active && <Check className="w-3.5 h-3.5 ml-auto text-primary" />}
              </button>
            )
          })}

          <div className="my-1 h-px bg-border/50" />
          {/* 系统自检:打开弹窗(逐项检查数据源/AI/通知连通性) */}
          <button
            onClick={() => {
              setOpen(false)
              onOpenSelfCheck()
            }}
            className="flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent/60 transition-colors"
          >
            <Stethoscope className="w-3.5 h-3.5" />
            系统自检
          </button>

          {isAuthenticated() && (
            <>
              <div className="my-1 h-px bg-border/50" />
              {/* 修改密码:校验旧密码(J10),改密后旧 token 吊销 */}
              <button
                onClick={() => {
                  setOpen(false)
                  setPwdOpen(true)
                }}
                className="flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] text-muted-foreground hover:text-foreground hover:bg-accent/60 transition-colors"
              >
                <KeyRound className="w-3.5 h-3.5" />
                修改密码
              </button>
              <button
                onClick={logout}
                className="flex w-full items-center gap-2.5 px-2.5 py-2 rounded-lg text-[12px] text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
              >
                <LogOut className="w-3.5 h-3.5" />
                退出登录
              </button>
            </>
          )}
          </div>
        </div>
      )}
      <ChangePasswordDialog open={pwdOpen} onOpenChange={setPwdOpen} />
    </div>
  )
}
