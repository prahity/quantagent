export type Position = {
  symbol: string;
  quantity: number;
  avg_cost: number;
  mark_price: number | null;
  market_value: number | null;
};

export type DecisionRequest = {
  schema_version: string;
  run_id: string;
  request_id: string;
  mode: "V1" | "V2";
  as_of: string;
  window_start: string;
  window_end: string;
  initial_cash: number;
  account: { currency: string; cash: number; equity: number; positions: Position[] };
  clock: {
    cadence_clock: string;
    decision_interval_seconds: number;
    time_scale: number;
    execution_resolution_ms: number;
    interval: "hourly" | "daily";
  };
  data: { feed_schema: string; workspace_schema: string; news_event_ids: string[]; symbols: string[] };
  limits: {
    max_orders_per_request: number;
    max_position_pct_equity: number;
    allow_shorting: boolean;
    order_validity_bars: number;
    slippage_bps: number;
    commission_per_share: number;
  };
  step: number;
  steps_total: number;
};

export type RunState = {
  run_id: string;
  label: string | null;
  status: "created" | "awaiting_action" | "finished";
  step: number;
  steps_total: number;
  as_of: string | null;
  config: Record<string, unknown> & { interval: string; mode: string; initial_cash: number; start: string; end: string; symbols: string[] };
  data_version: string;
  symbols: string[];
  cash: number;
  equity: number;
  positions: Position[];
  marks: Record<string, number>;
  pending_orders: { order_id: string; symbol: string; side: string; quantity: number; execute_at: string; request_id: string; placed_step: number }[];
  fills: number;
  rejections: number;
  invalid_responses: number;
  held_requests: number;
  news_total_in_window: number;
  news_released: number;
  current_request: DecisionRequest | null;
};

export type RunSummary = {
  run_id: string;
  label: string | null;
  status: string;
  step: number;
  steps_total: number;
  as_of: string | null;
  cash: number;
  equity: number;
  fills: number;
  rejections: number;
  symbols: string[];
  interval: string;
  mode: string;
  initial_cash: number;
  start: string;
  end: string;
  net_profit: number | null;
};

export type Bar = { ts: string; open: number; high: number; low: number; close: number; volume: number; bar_end: string };

export type NewsRecord = {
  event_id: string;
  symbol: string;
  provider: string;
  form: string;
  title: string;
  summary: string;
  source_url: string;
  published_at: string;
  available_at: string;
  released_at: string;
  availability_basis: string;
  availability_verified: boolean;
};

export type Fill = {
  order_id: string;
  request_id: string;
  symbol: string;
  side: string;
  quantity: number;
  execute_at: string;
  filled_at: string;
  bar_open: number;
  fill_price: number;
  commission: number;
  slippage_cost: number;
  notional: number;
  cash_after: number;
};

export type Rejection = {
  order_id: string;
  request_id: string;
  symbol: string;
  side: string;
  quantity: number;
  execute_at: string;
  at: string;
  reason: string;
};

export type EquityPoint = { ts: string; cash: number; equity: number; drawdown: number };

export type StepRecord = {
  step: number;
  request_id: string;
  as_of: string;
  response: Record<string, unknown> | null;
  accepted: boolean;
  errors: { code: string; message: string; order_id: string | null }[];
  order_outcomes: { order_id: string; status: string; reason: string | null }[];
  released_news: string[];
};

export type Report = {
  run_id: string;
  label: string | null;
  environment_version: string;
  data_version: string;
  interval: string;
  mode: string;
  symbols: string[];
  start: string;
  end: string;
  steps: number;
  initial_cash: number;
  final_cash: number;
  final_equity: number;
  net_profit: number;
  return_pct: number;
  max_drawdown_pct: number;
  sharpe: number | null;
  sharpe_periods_per_year: number;
  turnover: number;
  traded_notional: number;
  commissions: number;
  slippage_cost: number;
  fills: number;
  rejections: number;
  invalid_responses: number;
  held_requests: number;
  final_positions: Position[];
  valuation_convention: string;
  costs: Record<string, unknown>;
};

export type ActionResult = {
  ack: { accepted: boolean; errors: { code: string; message: string }[]; orders: { order_id: string; status: string; reason: string | null }[] };
  next_request: DecisionRequest | null;
  report: Report | null;
};

export type Config = {
  environment: Record<string, unknown> & { initial_cash: number; interval: string; mode: string; execution: Record<string, number | boolean> };
  universe: { data_start: string; data_end: string; symbols: { ticker: string; name: string; cik: number; sector: string }[] };
  splits: {
    development: { id: string; start: string; end: string }[];
    validation: { id: string; start: string; end: string }[];
    sealed: { id: string; start: string; end: string };
  };
  snapshot: {
    data_version: string;
    manifest: { fetched_at?: string; sources?: Record<string, unknown> };
    coverage: Record<string, Record<string, { rows: number; first: string; last: string }>>;
    news: { rows: number; by_symbol: Record<string, number>; by_provider: Record<string, number> };
  };
  agents: string[];
  scripts: { id: string; name: string; description: string; commands: number }[];
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: { "content-type": "application/json", ...(init?.headers ?? {}) }, cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () => request<{ ok: boolean; detail?: string; data_version?: string; runs?: number }>("/api/health"),
  config: () => request<Config>("/api/config"),
  runs: () => request<RunSummary[]>("/api/runs"),
  createRun: (body: Record<string, unknown>) => request<DecisionRequest>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  run: (id: string) => request<RunState>(`/api/runs/${id}`),
  equity: (id: string) => request<EquityPoint[]>(`/api/runs/${id}/equity`),
  fills: (id: string) => request<Fill[]>(`/api/runs/${id}/fills`),
  rejections: (id: string) => request<Rejection[]>(`/api/runs/${id}/rejections`),
  records: (id: string) => request<StepRecord[]>(`/api/runs/${id}/records`),
  report: (id: string) => request<Report>(`/api/runs/${id}/report`),
  replay: (id: string) => request<{ match: boolean; actions_replayed: number; original: Record<string, number> | null; replayed: Record<string, number> | null }>(`/api/runs/${id}/replay`),
  prices: (id: string, symbol: string, limit?: number) =>
    request<{ symbol: string; as_of: string; bars: Bar[] }>(`/api/runs/${id}/feed/prices?symbol=${encodeURIComponent(symbol)}${limit ? `&limit=${limit}` : ""}`),
  pricesAsOf: (id: string, symbol: string, asOf: string) =>
    request<{ symbol: string; as_of: string; bars: Bar[] }>(`/api/runs/${id}/feed/prices?symbol=${encodeURIComponent(symbol)}&as_of=${encodeURIComponent(asOf)}`),
  news: (id: string) => request<{ as_of: string; records: NewsRecord[] }>(`/api/runs/${id}/feed/news`),
  submit: (id: string, payload: unknown) => request<ActionResult>(`/api/runs/${id}/actions`, { method: "POST", body: JSON.stringify(payload) }),
  autoplay: (id: string, body: { agent: string; script?: string; steps: number }) =>
    request<{ steps_done: number; agent: string; state: RunState; last: ActionResult | null }>(`/api/runs/${id}/autoplay`, { method: "POST", body: JSON.stringify(body) }),
};
