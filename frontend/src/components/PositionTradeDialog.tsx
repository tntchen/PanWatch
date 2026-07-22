import { useEffect, useState } from 'react'
import { TrendingUp, TrendingDown } from 'lucide-react'
import { positionTradesApi } from '@panwatch/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

/** 弹窗所需的最小持仓信息（与 Stocks.tsx 的 Position 结构兼容） */
export interface PositionTradeTarget {
  id: number
  symbol: string
  name: string
  market: string
  cost_price: number
  quantity: number
}

interface PositionTradeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  position: PositionTradeTarget | null
  /** 初始方向：buy=加仓 / sell=减仓（弹窗内可切换） */
  direction: 'buy' | 'sell'
  onSuccess: () => void
}

/** Date -> 'YYYY-MM-DDTHH:MM:SS'（本地时间，契约格式） */
function toContractTimestamp(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** Date -> datetime-local 输入框值 'YYYY-MM-DDTHH:MM' */
function toLocalInputValue(d: Date): string {
  return toContractTimestamp(d).slice(0, 16)
}

export function PositionTradeDialog({ open, onOpenChange, position, direction: initialDirection, onSuccess }: PositionTradeDialogProps) {
  const [direction, setDirection] = useState<'buy' | 'sell'>(initialDirection)
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [fee, setFee] = useState('0')
  const [tradedAt, setTradedAt] = useState('')
  const [note, setNote] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const { toast } = useToast()

  // 每次打开时重置表单
  useEffect(() => {
    if (open) {
      setDirection(initialDirection)
      setPrice('')
      setQuantity('')
      setFee('0')
      setTradedAt(toLocalInputValue(new Date()))
      setNote('')
    }
  }, [open, initialDirection, position?.id])

  if (!position) return null

  const isBuy = direction === 'buy'
  const priceNum = parseFloat(price)
  const quantityNum = parseInt(quantity, 10)
  const feeNum = fee.trim() === '' ? 0 : parseFloat(fee)
  const quantityValid = Number.isInteger(quantityNum) && quantityNum > 0
  const oversell = !isBuy && quantityValid && quantityNum > position.quantity
  const canSubmit =
    !submitting &&
    Number.isFinite(priceNum) && priceNum > 0 &&
    quantityValid &&
    Number.isFinite(feeNum) && feeNum >= 0 &&
    !oversell

  const handleSubmit = async () => {
    if (!canSubmit) return
    setSubmitting(true)
    try {
      const payload: Parameters<typeof positionTradesApi.create>[1] = {
        direction,
        price: priceNum,
        quantity: quantityNum,
        fee: feeNum,
      }
      // datetime-local 值为 'YYYY-MM-DDTHH:MM'，补秒到契约格式；空则由后端默认当前时间
      if (tradedAt) payload.traded_at = tradedAt.length === 16 ? `${tradedAt}:00` : tradedAt
      if (note.trim()) payload.note = note.trim()
      await positionTradesApi.create(position.id, payload)
      toast(isBuy ? '加仓已记录，成本已重算' : '减仓已记录，盈亏已结转', 'success')
      onSuccess()
    } catch (e) {
      toast(e instanceof Error ? e.message : '流水录入失败', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            {isBuy ? (
              <TrendingUp className="w-4 h-4 text-rose-500" />
            ) : (
              <TrendingDown className="w-4 h-4 text-emerald-500" />
            )}
            {isBuy ? '加仓' : '减仓'} · {position.name}
            <span className="font-mono text-[12px] text-muted-foreground">{position.symbol}</span>
          </DialogTitle>
          <DialogDescription>
            当前持仓 {position.quantity} 股 @ 成本 {position.cost_price}
            {isBuy ? '；买入后按移动加权平均重算成本' : '；卖出成本不变，结转已实现盈亏'}
          </DialogDescription>
        </DialogHeader>

        {/* 方向切换（红涨绿跌） */}
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => setDirection('buy')}
            className={`h-9 rounded-md text-[13px] font-medium border transition-colors ${
              isBuy
                ? 'bg-rose-500/10 border-rose-500/40 text-rose-600'
                : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-rose-500/30'
            }`}
          >
            买入（加仓）
          </button>
          <button
            type="button"
            onClick={() => setDirection('sell')}
            className={`h-9 rounded-md text-[13px] font-medium border transition-colors ${
              !isBuy
                ? 'bg-emerald-500/10 border-emerald-500/40 text-emerald-600'
                : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-emerald-500/30'
            }`}
          >
            卖出（减仓）
          </button>
        </div>

        <div className="grid grid-cols-2 gap-3 mt-3">
          <div>
            <Label htmlFor="trade-price">成交价格</Label>
            <Input
              id="trade-price"
              type="number"
              step="any"
              min="0"
              value={price}
              onChange={e => setPrice(e.target.value)}
              placeholder="如 112.57"
            />
          </div>
          <div>
            <Label htmlFor="trade-quantity">数量（股）</Label>
            <Input
              id="trade-quantity"
              type="number"
              step="1"
              min="1"
              value={quantity}
              onChange={e => setQuantity(e.target.value)}
              placeholder={isBuy ? '如 100' : `≤ ${position.quantity}`}
            />
            {oversell && (
              <p className="text-[11px] text-rose-500 mt-1">卖出数量不能超过当前持仓 {position.quantity} 股</p>
            )}
          </div>
          <div>
            <Label htmlFor="trade-fee">手续费</Label>
            <Input
              id="trade-fee"
              type="number"
              step="any"
              min="0"
              value={fee}
              onChange={e => setFee(e.target.value)}
              placeholder="0"
            />
          </div>
          <div>
            <Label htmlFor="trade-time">成交时间</Label>
            <Input
              id="trade-time"
              type="datetime-local"
              value={tradedAt}
              onChange={e => setTradedAt(e.target.value)}
            />
          </div>
        </div>
        <div className="mt-3">
          <Label htmlFor="trade-note">备注（可选）</Label>
          <Input
            id="trade-note"
            value={note}
            onChange={e => setNote(e.target.value)}
            placeholder="如：按计划第一批建仓"
          />
        </div>

        <div className="flex items-center gap-3 justify-end mt-5">
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={isBuy ? 'bg-rose-500 hover:bg-rose-600 text-white' : 'bg-emerald-500 hover:bg-emerald-600 text-white'}
          >
            {submitting ? '提交中...' : isBuy ? '确认买入' : '确认卖出'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default PositionTradeDialog
