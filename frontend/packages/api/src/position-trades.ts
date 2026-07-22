import { fetchAPI } from './client'

/**
 * 持仓交易流水（Phase 1 持仓交易化）。
 * API 契约（两端共用，不得更改字段名）：
 * - POST /api/positions/{position_id}/trades
 * - GET  /api/positions/{position_id}/trades（按 traded_at 升序）
 */

export type PositionTradeDirection = 'buy' | 'sell' | 'adjustment'

export interface PositionTrade {
  id: number
  position_id: number
  direction: PositionTradeDirection
  price: number
  quantity: number
  fee: number
  /** ISO 时间（YYYY-MM-DDTHH:MM:SS） */
  traded_at: string
  /** 卖出时为本次已实现盈亏；买入/调整为 null */
  realized_pnl: number | null
  note: string | null
  /** ISO 时间 */
  created_at: string
}

export interface PositionTradeCreatePayload {
  direction: 'buy' | 'sell'
  price: number
  /** 整数，>0 */
  quantity: number
  fee?: number
  /** YYYY-MM-DDTHH:MM:SS，可选（默认当前） */
  traded_at?: string
  note?: string
}

/** Position 对象在交易化后新增的字段（其余字段由页面侧定义） */
export interface PositionTradeStats {
  realized_pnl_total: number
  trade_count: number
}

export interface PositionTradeCreateResponse {
  position: PositionTradeStats & Record<string, unknown>
  trade: PositionTrade
}

export const positionTradesApi = {
  /** 流水列表（按 traded_at 升序）。 */
  list: (positionId: number) =>
    fetchAPI<PositionTrade[]>(`/positions/${positionId}/trades`),

  /**
   * 录入一笔买入/卖出流水。
   * 成本口径：买入移动加权平均重算成本；卖出成本不变、结转已实现盈亏。
   * 错误：卖出数量 > 持仓 → 400。
   */
  create: (positionId: number, payload: PositionTradeCreatePayload) =>
    fetchAPI<PositionTradeCreateResponse>(`/positions/${positionId}/trades`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
}
