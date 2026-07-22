import { fetchAPI } from './client'

/**
 * 个股方案档案（Phase 2a stock_playbooks）。
 * API 契约（两端共用，不得更改字段名）：
 * - GET  /api/stocks/{stock_id}/playbook      → 激活版本（无档案时 data = null）
 * - GET  /api/stocks/{stock_id}/playbooks     → 版本列表（不含 payload 全文，含 summary）
 * - POST /api/stocks/{stock_id}/playbooks     → 创建新版本并置为 active（version 自增）
 * - POST /api/playbooks/{playbook_id}/activate → 切换激活版本
 */

/** 契约 A：方案档案 payload 结构（schema_version 信封，演进只增不改） */
export interface PlaybookMeta {
  name?: string
  version_label?: string
  strategy_mode?: string
  /** YYYY-MM-DD */
  base_date?: string
  base_price?: number
}

export interface PlaybookPriceLevel {
  label: string
  value: number
  note?: string
}

export type PlaybookBatchStatus = 'executed' | 'frozen' | 'pending'

export interface PlaybookBatch {
  name: string
  trigger?: string
  logic?: string
  status?: PlaybookBatchStatus | string
}

export interface PlaybookTZone {
  sell_range?: [number, number]
  buyback_range?: [number, number]
  size?: string
  mode?: string
}

export interface PlaybookDefense {
  rule?: string
  action?: string
}

export interface PlaybookStopLossTrack {
  track: string
  trigger?: string
  action?: string
}

export interface PlaybookCalendarItem {
  /** YYYY-MM-DD */
  date: string
  event?: string
  bias?: string
  plan?: string
}

export interface PlaybookScenario {
  /** 上行 | 基准 | 下行 等 */
  name: string
  trigger?: string
  action?: string
}

export interface StockPlaybookPayload {
  schema_version?: number
  meta?: PlaybookMeta
  price_levels?: PlaybookPriceLevel[]
  batches?: PlaybookBatch[]
  t_zone?: PlaybookTZone
  defense?: PlaybookDefense
  stop_loss_tracks?: PlaybookStopLossTrack[]
  calendar?: PlaybookCalendarItem[]
  scenarios?: PlaybookScenario[]
  /** 价格提醒规则名 → 命中时的方案提示文案 */
  trigger_hints?: Record<string, string>
  raw_markdown?: string
}

export interface StockPlaybook {
  id: number
  stock_id: number
  version: number
  is_active: boolean
  payload: StockPlaybookPayload
  /** 后端生成的紧凑中文摘要（≤500 token） */
  summary: string | null
  note: string | null
  /** ISO 时间 */
  created_at: string
}

/** 版本列表项：不含 payload 全文 */
export type StockPlaybookVersion = Omit<StockPlaybook, 'payload'>

export interface StockPlaybookCreatePayload {
  payload: StockPlaybookPayload
  note?: string
}

export const playbooksApi = {
  /** 激活版本；无档案时返回 null。 */
  getActive: (stockId: number) =>
    fetchAPI<StockPlaybook | null>(`/stocks/${stockId}/playbook`),

  /** 版本列表（不含 payload 全文，含 summary）。 */
  list: (stockId: number) =>
    fetchAPI<StockPlaybookVersion[]>(`/stocks/${stockId}/playbooks`),

  /** 创建新版本（version 自增并置为 active，其余版本 is_active=false）。 */
  create: (stockId: number, body: StockPlaybookCreatePayload) =>
    fetchAPI<StockPlaybook>(`/stocks/${stockId}/playbooks`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 切换激活版本。 */
  activate: (playbookId: number) =>
    fetchAPI<StockPlaybook>(`/playbooks/${playbookId}/activate`, { method: 'POST' }),
}
