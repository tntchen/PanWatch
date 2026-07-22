import { useEffect, useState } from 'react'
import { Receipt, RefreshCw, TrendingUp, TrendingDown, Settings2 } from 'lucide-react'
import { positionTradesApi, type PositionTrade } from '@panwatch/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'

/** 列表所需的最小持仓信息（与 Stocks.tsx 的 Position 结构兼容） */
export interface PositionTradesTarget {
  id: number
  symbol: string
  name: string
  market: string
  cost_price: number
  quantity: number
  realized_pnl_total?: number
  trade_count?: number
}

interface PositionTradesDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  position: PositionTradesTarget | null
}

const DIRECTION_META: Record<PositionTrade['direction'], { label: string; className: string; Icon: typeof TrendingUp }> = {
  buy: { label: '买入', className: 'bg-rose-500/10 text-rose-600', Icon: TrendingUp },
  sell: { label: '卖出', className: 'bg-emerald-500/10 text-emerald-600', Icon: TrendingDown },
  adjustment: { label: '调整', className: 'bg-slate-500/10 text-slate-500', Icon: Settings2 },
}

const formatMoney = (value: number) => {
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(2)}万`
  return value.toFixed(2)
}

const formatTime = (iso: string) => {
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    })
  } catch {
    return iso
  }
}

export function PositionTradesDrawer({ open, onOpenChange, position }: PositionTradesDrawerProps) {
  const [trades, setTrades] = useState<PositionTrade[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    if (!position) return
    setLoading(true)
    setError(null)
    try {
      const data = await positionTradesApi.list(position.id)
      setTrades(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载流水失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open && position) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, position?.id])

  if (!position) return null

  // API 返回按 traded_at 升序；展示时最新在前
  const ordered = [...trades].reverse()

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <Receipt className="w-4 h-4 text-primary" />
            交易流水 · {position.name}
            <span className="font-mono text-[12px] text-muted-foreground">{position.symbol}</span>
          </DialogTitle>
          <DialogDescription className="flex items-center gap-3 flex-wrap">
            <span>持仓 {position.quantity} 股 @ 成本 {position.cost_price}</span>
            {position.realized_pnl_total != null && (
              <span className={`font-mono ${position.realized_pnl_total >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                已实现盈亏 {position.realized_pnl_total >= 0 ? '+' : ''}{formatMoney(position.realized_pnl_total)}
              </span>
            )}
            <span>共 {trades.length} 笔</span>
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto min-h-0 py-2">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <span className="w-5 h-5 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
              <span className="ml-2 text-[13px] text-muted-foreground">加载中...</span>
            </div>
          ) : error ? (
            <div className="text-center py-12 text-[13px] text-rose-500">{error}</div>
          ) : ordered.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-[13px]">
              暂无交易流水，点击持仓行的「加仓 / 减仓」记录第一笔
            </div>
          ) : (
            <div className="space-y-2">
              {ordered.map(t => {
                const meta = DIRECTION_META[t.direction] || DIRECTION_META.adjustment
                return (
                  <div key={t.id} className="p-3 rounded-lg bg-accent/30">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className={`inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded shrink-0 ${meta.className}`}>
                          <meta.Icon className="w-3 h-3" />
                          {meta.label}
                        </span>
                        <span className="font-mono text-[13px] text-foreground whitespace-nowrap">
                          {t.price} × {t.quantity}
                        </span>
                        {t.fee > 0 && (
                          <span className="text-[11px] text-muted-foreground whitespace-nowrap">费 {t.fee}</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {t.direction === 'sell' && t.realized_pnl != null && (
                          <span className={`font-mono text-[12px] whitespace-nowrap ${t.realized_pnl >= 0 ? 'text-rose-500' : 'text-emerald-500'}`}>
                            {t.realized_pnl >= 0 ? '+' : ''}{formatMoney(t.realized_pnl)}
                          </span>
                        )}
                        <span className="text-[11px] text-muted-foreground whitespace-nowrap">{formatTime(t.traded_at)}</span>
                      </div>
                    </div>
                    {t.note && (
                      <p className="mt-1.5 text-[12px] text-muted-foreground leading-relaxed">{t.note}</p>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between pt-2 border-t">
          <span className="text-[11px] text-muted-foreground">
            {position.trade_count != null ? `流水总数 ${position.trade_count} 笔` : ''}
          </span>
          <Button variant="secondary" size="sm" onClick={load} disabled={loading}>
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default PositionTradesDrawer
