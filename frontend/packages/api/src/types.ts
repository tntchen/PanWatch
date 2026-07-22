export interface AIModel {
  id: number
  name: string
  service_id: number
  model: string
  is_default: boolean
}

export interface AIService {
  id: number
  name: string
  base_url: string
  api_key: string
  models: AIModel[]
  /** 属主租户（多租户；单租户/旧后端可能缺省） */
  tenant_id?: number
  /** 管理员托管服务：密钥不出网（api_key 恒为空串），非管理员只读 */
  is_managed?: boolean
}

export interface NotifyChannel {
  id: number
  name: string
  type: string
  config: Record<string, string>
  enabled: boolean
  is_default: boolean
  /** 属主租户（多租户；单租户/旧后端可能缺省） */
  tenant_id?: number
  /** 管理员共享给配额共享租户的渠道 */
  is_shared?: boolean
  /** 管理员托管渠道：config 恒为 {}，非管理员只读（可测试） */
  is_managed?: boolean
}

export interface SourceHealth {
  count: number
  success_rate: number | null
  p50_latency_ms: number | null
  last_error?: string
  last_success_at?: number
}

export interface DataSource {
  id: number
  name: string
  type: string
  provider: string
  config: Record<string, unknown>
  enabled: boolean
  priority: number
  supports_batch: boolean
  test_symbols: string[]
  engine_attached?: boolean
  health?: SourceHealth | null
  /** 孤儿源:该 (type, provider) 在包内无对应 vendor 且不在种子里,抓取/测试必失败。 */
  is_orphan?: boolean
}
