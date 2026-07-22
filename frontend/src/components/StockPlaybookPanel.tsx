import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import { BookOpen, ChevronDown, ChevronRight, FileUp, RefreshCw } from 'lucide-react'
import {
  playbooksApi,
  type StockPlaybook,
  type StockPlaybookPayload,
  type StockPlaybookVersion,
} from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@panwatch/base-ui/components/ui/select'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

export interface StockPlaybookPanelProps {
  /** stocks 表 id；null 表示该股票未在关注列表（无法挂方案档案） */
  stockId: number | null
  symbol: string
  market: string
  stockName?: string
}

const BATCH_STATUS_META: Record<string, { label: string; className: string }> = {
  executed: { label: '已执行', className: 'bg-emerald-500/10 text-emerald-600' },
  frozen: { label: '冻结', className: 'bg-slate-500/10 text-slate-500' },
  pending: { label: '待定', className: 'bg-amber-500/10 text-amber-600' },
}

const fmtDateTime = (iso?: string | null) => {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

const fmtRange = (r?: [number, number]) => (r && r.length === 2 ? `${r[0]} ~ ${r[1]}` : null)

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card p-3">
      <div className="text-[12px] font-semibold text-foreground mb-2">{title}</div>
      {children}
    </div>
  )
}

function KeyValue({ k, v }: { k: string; v?: string | null }) {
  if (!v) return null
  return (
    <div className="flex items-start gap-2 text-[12px]">
      <span className="text-muted-foreground shrink-0">{k}</span>
      <span className="text-foreground">{v}</span>
    </div>
  )
}

export function StockPlaybookPanel({ stockId, symbol, market, stockName }: StockPlaybookPanelProps) {
  const { toast } = useToast()
  const [active, setActive] = useState<StockPlaybook | null>(null)
  const [versions, setVersions] = useState<StockPlaybookVersion[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showRaw, setShowRaw] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [importText, setImportText] = useState('')
  const [importNote, setImportNote] = useState('')
  const [importing, setImporting] = useState(false)
  const [activating, setActivating] = useState(false)

  const load = useCallback(async () => {
    if (stockId == null) return
    setLoading(true)
    setError(null)
    try {
      const [activeData, versionList] = await Promise.all([
        playbooksApi.getActive(stockId),
        playbooksApi.list(stockId),
      ])
      setActive(activeData || null)
      setVersions(versionList || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载方案档案失败')
    } finally {
      setLoading(false)
    }
  }, [stockId])

  useEffect(() => {
    setShowRaw(false)
    setShowImport(false)
    setImportText('')
    setImportNote('')
    load()
  }, [load])

  const payload: StockPlaybookPayload | null = active?.payload ?? null

  const versionOptions = useMemo(
    () =>
      [...versions]
        .sort((a, b) => b.version - a.version)
        .map(v => ({
          id: v.id,
          label: `v${v.version}${v.is_active ? '（当前）' : ''} · ${fmtDateTime(v.created_at)}`,
        })),
    [versions]
  )

  const handleActivate = async (value: string) => {
    const id = Number(value)
    if (!id || active?.id === id) return
    setActivating(true)
    try {
      await playbooksApi.activate(id)
      toast('已切换激活版本', 'success')
      await load()
    } catch (e) {
      toast(`切换失败：${e instanceof Error ? e.message : '请稍后重试'}`, 'error')
    } finally {
      setActivating(false)
    }
  }

  const handleImport = async () => {
    if (stockId == null) return
    const text = importText.trim()
    if (!text) {
      toast('请粘贴方案 JSON', 'error')
      return
    }
    let parsed: StockPlaybookPayload
    try {
      const obj = JSON.parse(text)
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new Error('not an object')
      parsed = obj as StockPlaybookPayload
    } catch {
      toast('JSON 解析失败：请粘贴符合契约 A 的方案 payload 对象', 'error')
      return
    }
    setImporting(true)
    try {
      await playbooksApi.create(stockId, {
        payload: parsed,
        note: importNote.trim() || undefined,
      })
      toast('已导入并激活新版本', 'success')
      setShowImport(false)
      setImportText('')
      setImportNote('')
      await load()
    } catch (e) {
      toast(`导入失败：${e instanceof Error ? e.message : '请稍后重试'}`, 'error')
    } finally {
      setImporting(false)
    }
  }

  if (stockId == null) {
    return (
      <div className="card p-6 text-center text-[12px] text-muted-foreground">
        {stockName || symbol}（{market}）尚未加入关注列表，关注后才能维护方案档案。
      </div>
    )
  }

  const meta = payload?.meta
  const importPanel = showImport && (
    <div className="card p-3 space-y-2">
      <div className="text-[12px] font-semibold text-foreground">导入方案（粘贴契约 A payload JSON，提交后生成新版本并激活）</div>
      <textarea
        className="w-full h-48 rounded-md border border-input bg-background px-3 py-2 text-[12px] font-mono text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        placeholder='{"schema_version": 1, "meta": {"name": "...", ...}, ...}'
        value={importText}
        onChange={e => setImportText(e.target.value)}
      />
      <input
        className="w-full h-8 rounded-md border border-input bg-background px-3 text-[12px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        placeholder="版本备注（可选）"
        value={importNote}
        onChange={e => setImportNote(e.target.value)}
      />
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => setShowImport(false)}>取消</Button>
        <Button size="sm" onClick={handleImport} disabled={importing}>
          {importing ? '导入中...' : '提交新版本'}
        </Button>
      </div>
    </div>
  )

  return (
    <div className="space-y-3">
      {/* 头部：元信息 + 版本切换 + 操作 */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          <BookOpen className="w-4 h-4 text-primary shrink-0" />
          <span className="text-[13px] font-medium text-foreground truncate">
            {meta?.name || `${stockName || symbol} 方案`}
          </span>
          {meta?.version_label && (
            <span className="text-[11px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">{meta.version_label}</span>
          )}
          {meta?.strategy_mode && (
            <span className="text-[11px] px-1.5 py-0.5 rounded bg-accent/60 text-muted-foreground">{meta.strategy_mode}</span>
          )}
          {meta?.base_date && (
            <span className="text-[11px] text-muted-foreground">
              基准 {meta.base_date}{meta.base_price != null ? ` @ ${meta.base_price}` : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {versionOptions.length > 1 && (
            <Select value={String(active?.id ?? '')} onValueChange={handleActivate} disabled={activating}>
              <SelectTrigger className="h-7 w-[180px] text-[11px]">
                <SelectValue placeholder="选择版本" />
              </SelectTrigger>
              <SelectContent>
                {versionOptions.map(v => (
                  <SelectItem key={v.id} value={String(v.id)}>{v.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button variant="outline" size="sm" className="h-7 px-2 text-[11px]" onClick={() => setShowImport(v => !v)}>
            <FileUp className="w-3.5 h-3.5 mr-1" /> 导入
          </Button>
          <Button variant="ghost" size="icon" className="h-7 w-7" onClick={load} disabled={loading} title="刷新">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </div>

      {importPanel}

      {error && <div className="card p-4 text-[12px] text-rose-500">{error}</div>}

      {!error && !loading && !active && (
        <div className="card p-6 text-center space-y-3">
          <div className="text-[12px] text-muted-foreground">暂无方案档案</div>
          <Button variant="outline" size="sm" onClick={() => setShowImport(true)}>
            <FileUp className="w-3.5 h-3.5 mr-1" /> 从 markdown / JSON 导入
          </Button>
        </div>
      )}

      {active && payload && (
        <>
          {active.summary && (
            <div className="card p-3">
              <div className="text-[12px] font-semibold text-foreground mb-1">摘要</div>
              <div className="text-[12px] text-muted-foreground whitespace-pre-wrap">{active.summary}</div>
              {active.note && <div className="mt-1 text-[11px] text-muted-foreground/70">备注：{active.note}</div>}
            </div>
          )}

          {!!payload.price_levels?.length && (
            <Section title="价位表">
              <div className="space-y-1">
                {payload.price_levels.map((lv, i) => (
                  <div key={`${lv.label}-${i}`} className="flex items-baseline gap-2 text-[12px]">
                    <span className="text-muted-foreground w-16 shrink-0">{lv.label}</span>
                    <span className="font-mono font-medium text-foreground">{lv.value}</span>
                    {lv.note && <span className="text-muted-foreground text-[11px]">{lv.note}</span>}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {!!payload.batches?.length && (
            <Section title="批次">
              <div className="space-y-2">
                {payload.batches.map((b, i) => {
                  const st = BATCH_STATUS_META[String(b.status || '')] || { label: b.status || '待定', className: 'bg-accent/60 text-muted-foreground' }
                  return (
                    <div key={`${b.name}-${i}`} className="flex items-start gap-2 text-[12px]">
                      <span className="font-medium text-foreground shrink-0">{b.name}</span>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${st.className}`}>{st.label}</span>
                      <div className="min-w-0">
                        {b.trigger && <div className="font-mono text-foreground">{b.trigger}</div>}
                        {b.logic && <div className="text-muted-foreground">{b.logic}</div>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </Section>
          )}

          {payload.t_zone && (
            <Section title="做 T 区">
              <div className="space-y-1">
                <KeyValue k="卖出区" v={fmtRange(payload.t_zone.sell_range)} />
                <KeyValue k="接回区" v={fmtRange(payload.t_zone.buyback_range)} />
                <KeyValue k="数量" v={payload.t_zone.size} />
                <KeyValue k="模式" v={payload.t_zone.mode} />
              </div>
            </Section>
          )}

          {payload.defense && (
            <Section title="防线">
              <div className="space-y-1">
                <KeyValue k="规则" v={payload.defense.rule} />
                <KeyValue k="动作" v={payload.defense.action} />
              </div>
            </Section>
          )}

          {!!payload.stop_loss_tracks?.length && (
            <Section title="止损轨">
              <div className="space-y-2">
                {payload.stop_loss_tracks.map((t, i) => (
                  <div key={`${t.track}-${i}`} className="text-[12px]">
                    <span className="font-medium text-foreground">{t.track}</span>
                    {t.trigger && <span className="text-muted-foreground"> · {t.trigger}</span>}
                    {t.action && <div className="text-muted-foreground mt-0.5">{t.action}</div>}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {!!payload.calendar?.length && (
            <Section title="日历">
              <div className="space-y-2">
                {payload.calendar.map((c, i) => (
                  <div key={`${c.date}-${i}`} className="text-[12px]">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-foreground shrink-0">{c.date}</span>
                      {c.event && <span className="text-foreground">{c.event}</span>}
                      {c.bias && <span className="text-[11px] text-muted-foreground">{c.bias}</span>}
                    </div>
                    {c.plan && <div className="text-muted-foreground mt-0.5">{c.plan}</div>}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {!!payload.scenarios?.length && (
            <Section title="情景">
              <div className="space-y-2">
                {payload.scenarios.map((s, i) => (
                  <div key={`${s.name}-${i}`} className="text-[12px]">
                    <span className="font-medium text-foreground">{s.name}</span>
                    {s.trigger && <span className="text-muted-foreground"> · {s.trigger}</span>}
                    {s.action && <div className="text-muted-foreground mt-0.5">{s.action}</div>}
                  </div>
                ))}
              </div>
            </Section>
          )}

          {payload.raw_markdown && (
            <div className="card p-3">
              <button
                className="flex items-center gap-1 text-[12px] font-semibold text-foreground"
                onClick={() => setShowRaw(v => !v)}
              >
                {showRaw ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                原始方案全文
              </button>
              {showRaw && (
                <div className="prose prose-sm dark:prose-invert max-w-none mt-2 text-[12px]">
                  <ReactMarkdown>{payload.raw_markdown}</ReactMarkdown>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

export default StockPlaybookPanel
