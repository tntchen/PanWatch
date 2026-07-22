import { ShieldCheck } from 'lucide-react'

/** 「管理员托管」只读标记：用于配额共享租户看到的托管服务/渠道。 */
export function ManagedBadge({ className = '' }: { className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border border-border/50 bg-accent/40 px-2 py-0.5 text-[10px] leading-4 text-muted-foreground ${className}`}
      title="由管理员统一配置与托管，当前为只读"
    >
      <ShieldCheck className="h-3 w-3" />
      管理员托管
    </span>
  )
}
