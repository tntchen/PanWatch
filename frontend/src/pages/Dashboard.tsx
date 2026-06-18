import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw, AlertTriangle, Sparkles, Activity, ShieldAlert } from 'lucide-react'
import {
  dashboardApi,
  portfolioApi,
  recommendationsApi,
  type DashboardMarketIndex,
  type DashboardMonitorStock,
  type DashboardOverviewResponse,
  type PortfolioDiagnostics,
  type PortfolioBenchmark,
  type StrategySignalItem,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Onboarding } from '@panwatch/biz-ui/components/onboarding'
import StockInsightModal from '@panwatch/biz-ui/components/stock-insight-modal'

function pct(v?: number | null, digits = 2): string {
  if (v == null || !isFinite(v)) return '--'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}
function moveColor(v?: number | null): string {
  if (v == null) return 'text-muted-foreground'
  return v > 0 ? 'text-rose-500' : v < 0 ? 'text-emerald-500' : 'text-muted-foreground'
}
const ALERT_LABEL: Record<string, string> = {
  surge: '快速拉升',
  plunge: '快速跳水',
  high_volume: '放量异动',
  breakout: '突破',
  breakdown: '破位',
  limit_up: '涨停',
  limit_down: '跌停',
}

export default function DashboardPage() {
  const [loading, setLoading] = useState(true)
  const [indices, setIndices] = useState<DashboardMarketIndex[]>([])
  const [scan, setScan] = useState<DashboardMonitorStock[]>([])
  const [overview, setOverview] = useState<DashboardOverviewResponse | null>(null)
  const [diag, setDiag] = useState<PortfolioDiagnostics | null>(null)
  const [bench, setBench] = useState<PortfolioBenchmark | null>(null)
  const [oppFallback, setOppFallback] = useState<StrategySignalItem[]>([])
  const [showOnboarding, setShowOnboarding] = useState(false)
  const [modal, setModal] = useState<{ open: boolean; symbol: string; market: string; name: string; hasPosition: boolean }>({
    open: false,
    symbol: '',
    market: 'CN',
    name: '',
    hasPosition: false,
  })

  const load = useCallback(async () => {
    setLoading(true)
    const [idx, sc, ov, dg, bn] = await Promise.allSettled([
      dashboardApi.indices(),
      dashboardApi.intradayScan(),
      dashboardApi.overview({ market: 'ALL', action_limit: 6, risk_limit: 6 }),
      portfolioApi.diagnostics(),
      portfolioApi.benchmark({ days: 60 }),
    ])
    if (idx.status === 'fulfilled') setIndices(idx.value)
    if (sc.status === 'fulfilled') setScan(sc.value.stocks || [])
    if (ov.status === 'fulfilled') setOverview(ov.value)
    if (dg.status === 'fulfilled') setDiag(dg.value)
    if (bn.status === 'fulfilled') setBench(bn.value)
    if (ov.status !== 'fulfilled' || !ov.value.action_center?.opportunities?.length) {
      try {
        const r = await recommendationsApi.listStrategySignals({ status: 'active', limit: 5 })
        setOppFallback(r.items || [])
      } catch {
        /* ignore */
      }
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
    if (!localStorage.getItem('panwatch_onboarding_completed')) setShowOnboarding(true)
  }, [load])

  const handleOnboardingComplete = () => {
    localStorage.setItem('panwatch_onboarding_completed', 'true')
    setShowOnboarding(false)
  }

  const openStock = (symbol: string, market: string, name: string, hasPosition = false) =>
    setModal({ open: true, symbol, market: market || 'CN', name, hasPosition })

  // 今日要紧事:持仓异动 + 触发的盯盘信号(有 AI 建议/告警优先)
  const urgent = useMemo(() => {
    const items = (scan || []).filter((s) => s.has_position || s.alert_type || s.suggestion?.should_alert)
    const weight = (s: DashboardMonitorStock) =>
      (s.suggestion?.should_alert ? 1000 : 0) + (s.has_position ? 500 : 0) + Math.abs(s.change_pct || 0)
    return items.sort((a, b) => weight(b) - weight(a)).slice(0, 8)
  }, [scan])

  const opportunities = useMemo(() => {
    const list = overview?.action_center?.opportunities?.length ? overview.action_center.opportunities : oppFallback
    return list.slice(0, 5)
  }, [overview, oppFallback])

  const hasHoldings = (diag?.position_count ?? 0) > 0
  const benchReady = bench && !bench.empty && bench.excess_return != null
  const hasWatchlist = (overview?.kpis?.watchlist_count ?? 0) > 0

  return (
    <div className="page-container pb-10">
      {/* 顶部:标题 + 大盘指数细条(降级,不再占主位) */}
      <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-[20px] font-bold tracking-tight text-foreground md:text-[22px]">今日该看什么</h1>
          <Button onClick={load} disabled={loading} size="sm" variant="ghost" className="h-7 px-2">
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          {indices.slice(0, 5).map((ix) => (
            <span key={`${ix.market}:${ix.symbol}`} className="flex items-center gap-1">
              <span className="text-muted-foreground">{ix.name}</span>
              <span className={`font-mono ${moveColor(ix.change_pct)}`}>{pct(ix.change_pct)}</span>
            </span>
          ))}
        </div>
      </div>

      {/* 今日要紧事(主角) */}
      <div className="card mb-3 p-4">
        <div className="mb-2 flex items-center gap-2">
          <Activity className="h-4 w-4 text-primary" />
          <h2 className="text-sm font-semibold">今日要紧事</h2>
          <span className="text-[11px] text-muted-foreground">你的持仓/自选里今天该关注的</span>
        </div>
        {loading && urgent.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-muted-foreground">扫描中…</div>
        ) : urgent.length === 0 ? (
          <div className="py-6 text-center text-[12px] text-muted-foreground">今日暂无明显异动或触发信号 ✓</div>
        ) : (
          <div className="divide-y divide-border/40">
            {urgent.map((s) => (
              <div
                key={`${s.market}:${s.symbol}`}
                className="flex cursor-pointer items-center gap-3 py-2 hover:bg-accent/30"
                onClick={() => openStock(s.symbol, s.market, s.name, s.has_position)}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium">{s.name}</span>
                    {s.has_position && <span className="rounded bg-emerald-500/15 px-1 text-[9px] text-emerald-500">持仓</span>}
                    {s.alert_type && ALERT_LABEL[s.alert_type] && (
                      <span className="rounded bg-amber-500/15 px-1 text-[9px] text-amber-600">{ALERT_LABEL[s.alert_type]}</span>
                    )}
                  </div>
                  {s.suggestion?.signal && <div className="truncate text-[11px] text-muted-foreground">{s.suggestion.signal}</div>}
                </div>
                {s.suggestion?.action_label && (
                  <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">{s.suggestion.action_label}</span>
                )}
                <div className="shrink-0 text-right">
                  <div className={`font-mono text-[13px] ${moveColor(s.change_pct)}`}>{pct(s.change_pct)}</div>
                  {s.has_position && s.pnl_pct != null && (
                    <div className={`font-mono text-[10px] ${moveColor(s.pnl_pct)}`}>持仓 {pct(s.pnl_pct)}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {/* 组合体检(并入首页) */}
        <div className="card p-4">
          <div className="mb-2 flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold">组合体检</h2>
          </div>
          {!hasHoldings ? (
            <div className="py-6 text-center text-[12px] text-muted-foreground">
              {loading ? '加载中…' : '暂无持仓,添加持仓后这里给风险与相对大盘表现'}
            </div>
          ) : (
            <div className="space-y-2 text-[12px]">
              <div className="flex items-center justify-between rounded bg-accent/15 px-2 py-1.5">
                <span className="text-muted-foreground">近 60 日相对大盘</span>
                <span className={`font-mono font-semibold ${moveColor(benchReady ? bench!.excess_return : null)}`}>
                  {benchReady ? `超额 ${pct(bench!.excess_return)}` : '数据不足'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">持仓 {diag!.position_count} 只 · 最大单仓</span>
                <span className={`font-mono ${diag!.max_weight >= 0.4 ? 'text-amber-600' : ''}`}>
                  {(diag!.max_weight * 100).toFixed(0)}%
                </span>
              </div>
              {Object.entries(diag!.by_market).map(([m, v]) => {
                const w = diag!.total_market_value > 0 ? (v / diag!.total_market_value) * 100 : 0
                return (
                  <div key={m}>
                    <div className="flex justify-between text-[11px]">
                      <span>{m}</span>
                      <span className="font-mono">{w.toFixed(0)}%</span>
                    </div>
                    <div className="h-1.5 rounded bg-accent/40">
                      <div className="h-1.5 rounded bg-primary/60" style={{ width: `${Math.min(100, w)}%` }} />
                    </div>
                  </div>
                )
              })}
              {diag!.alerts.length > 0 ? (
                <div className="space-y-1 pt-1">
                  {diag!.alerts.map((a, i) => (
                    <div key={i} className="flex items-start gap-1 text-[11px] text-amber-600">
                      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                      <span>{a}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="pt-1 text-[11px] text-emerald-500">✓ 集中度/分布未见明显风险</div>
              )}
            </div>
          )}
        </div>

        {/* 机会精选 */}
        <div className="card p-4">
          <div className="mb-2 flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold">机会精选</h2>
          </div>
          {opportunities.length === 0 ? (
            <div className="py-6 text-center text-[12px] text-muted-foreground">{loading ? '加载中…' : '暂无活跃机会信号'}</div>
          ) : (
            <div className="divide-y divide-border/40">
              {opportunities.map((o) => (
                <div
                  key={`${o.stock_market}:${o.stock_symbol}`}
                  className="flex cursor-pointer items-center gap-2 py-2 hover:bg-accent/30"
                  onClick={() => openStock(o.stock_symbol, o.stock_market, o.stock_name || o.stock_symbol)}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-[13px] font-medium">{o.stock_name || o.stock_symbol}</span>
                      {o.action_label && <span className="rounded bg-primary/10 px-1 text-[9px] text-primary">{o.action_label}</span>}
                    </div>
                    {(o.signal || o.reason) && <div className="truncate text-[11px] text-muted-foreground">{o.signal || o.reason}</div>}
                  </div>
                  <div className="shrink-0 text-right">
                    <div className="font-mono text-[13px] text-foreground">{(o.rank_score ?? o.score ?? 0).toFixed(0)}</div>
                    <div className="text-[9px] text-muted-foreground">评分</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <StockInsightModal
        open={modal.open}
        onOpenChange={(o) => setModal((m) => ({ ...m, open: o }))}
        symbol={modal.symbol}
        market={modal.market}
        stockName={modal.name}
        hasPosition={modal.hasPosition}
      />
      <Onboarding open={showOnboarding} onComplete={handleOnboardingComplete} hasStocks={hasWatchlist} />
    </div>
  )
}
