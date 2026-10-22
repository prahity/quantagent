"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, type ActionResult, type Bar, type Config, type EquityPoint, type Fill, type NewsRecord, type Rejection, type Report, type RunState, type StepRecord } from "@/lib/api";
import { money, num, pct, signed, utc, utcShort } from "@/lib/format";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EquityChart, PriceChart } from "@/components/charts";
import { OrderForm } from "@/components/order-form";

type Bundle = {
  state: RunState;
  equity: EquityPoint[];
  fills: Fill[];
  rejections: Rejection[];
  records: StepRecord[];
  news: NewsRecord[];
  report: Report | null;
};

export function RunView({ runId }: { runId: string }) {
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [symbol, setSymbol] = useState<string>("");
  const [bars, setBars] = useState<Bar[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<ActionResult | null>(null);
  const [agent, setAgent] = useState("hold");
  const [script, setScript] = useState("aapl_round_trip");
  const [steps, setSteps] = useState("10");
  const [replay, setReplay] = useState<{ match: boolean; actions_replayed: number } | null>(null);
  const [probe, setProbe] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const state = await api.run(runId);
      const [equity, fills, rejections, records, news] = await Promise.all([api.equity(runId), api.fills(runId), api.rejections(runId), api.records(runId), api.news(runId)]);
      const report = state.status === "finished" ? await api.report(runId) : null;
      setBundle({ state, equity, fills, rejections, records, news: news.records, report });
      setSymbol((cur) => cur || state.symbols[0] || "");
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }, [runId]);

  useEffect(() => {
    void Promise.resolve().then(load);
    api.config().then(setConfig).catch(() => undefined);
  }, [load]);

  useEffect(() => {
    if (!bundle || bundle.state.status === "finished") return;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [bundle, load]);

  useEffect(() => {
    if (!symbol) return;
    api
      .prices(runId, symbol)
      .then((r) => setBars(r.bars))
      .catch(() => setBars([]));
  }, [runId, symbol, bundle?.state.as_of]);

  async function submit(payload: unknown) {
    setBusy(true);
    try {
      const result = await api.submit(runId, payload);
      setLast(result);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function autoplay(n: number) {
    setBusy(true);
    try {
      const result = await api.autoplay(runId, { agent, script: agent === "script" ? script : undefined, steps: n });
      if (result.last) setLast(result.last);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function runReplay() {
    try {
      setReplay(await api.replay(runId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function probeFuture() {
    if (!bundle?.state.as_of) return;
    const future = new Date(new Date(bundle.state.as_of).getTime() + 7 * 86400000).toISOString();
    try {
      await api.pricesAsOf(runId, symbol, future);
      setProbe("Unexpected: the feed returned future data.");
    } catch (e) {
      setProbe(e instanceof ApiError ? `${e.status} ${e.message}` : String(e));
    }
  }

  const state = bundle?.state;
  const request = state?.current_request ?? null;
  const pnl = state ? state.equity - state.config.initial_cash : 0;
  const newsById = useMemo(() => new Map((bundle?.news ?? []).map((n) => [n.event_id, n])), [bundle?.news]);
  const digest = (request?.data.news_event_ids ?? []).map((id) => newsById.get(id)).filter(Boolean) as NewsRecord[];

  if (error && !bundle) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load run {runId}</AlertTitle>
        <AlertDescription>{error}. Runs live in the API process memory; restarting the server clears them (recorded logs stay under runs/).</AlertDescription>
      </Alert>
    );
  }
  if (!bundle || !state) return <Skeleton className="h-96 w-full" />;

  const progress = state.status === "finished" ? state.steps_total : state.step;

  return (
    <div className="space-y-6">
      <section className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="font-mono text-lg font-semibold tracking-tight">{state.run_id}</h1>
            <Badge variant={state.status === "finished" ? "secondary" : "default"}>{state.status === "finished" ? "finished" : "awaiting action"}</Badge>
            <Badge variant="outline">{state.config.interval}</Badge>
            <Badge variant="outline">{state.config.mode}</Badge>
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {state.label ? <>{state.label} · </> : null}
            {state.config.start} → {state.config.end} · {state.symbols.join(", ")} · snapshot <span className="font-mono">{state.data_version}</span>
          </p>
        </div>
        <div className="text-right">
          <div className="text-xs uppercase tracking-wide text-muted-foreground">Simulated clock (as_of)</div>
          <div className="font-mono text-lg">{utc(state.as_of)}</div>
          <div className="font-mono text-xs text-muted-foreground">
            decision {progress}/{state.steps_total}
            {request ? ` · window ${utcShort(request.window_start)} → ${utcShort(request.window_end)}` : ""}
          </div>
        </div>
      </section>

      <div className="h-1 w-full overflow-hidden rounded bg-muted">
        <div className="h-full bg-foreground transition-all" style={{ width: `${(100 * progress) / Math.max(state.steps_total, 1)}%` }} />
      </div>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
        <Kpi label="Cash" value={money(state.cash)} />
        <Kpi label="Equity" value={money(state.equity)} />
        <Kpi label="Net P&L" value={signed(pnl)} tone={pnl >= 0 ? "pos" : "neg"} sub={pct((pnl / state.config.initial_cash) * 100)} />
        <Kpi label="Positions" value={String(state.positions.length)} sub={state.positions.map((p) => `${p.quantity} ${p.symbol}`).join(", ") || "flat"} />
        <Kpi label="Fills / rejections" value={`${state.fills} / ${state.rejections}`} sub={`${state.invalid_responses} invalid responses`} />
        <Kpi label="News released" value={`${state.news_released} / ${state.news_total_in_window}`} sub="eligible records so far" />
      </section>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="space-y-6">
          <Card>
            <CardHeader className="flex flex-row items-center justify-between gap-4">
              <div>
                <CardTitle className="text-sm">Released market feed</CardTitle>
                <CardDescription>Only bars that completed by as_of. Dots are fills at the next bar open.</CardDescription>
              </div>
              <Select value={symbol} onValueChange={(v) => setSymbol(String(v))}>
                <SelectTrigger size="sm" className="w-28 font-mono">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {state.symbols.map((s) => (
                    <SelectItem key={s} value={s} className="font-mono">
                      {s}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </CardHeader>
            <CardContent>
              <PriceChart bars={bars} fills={bundle.fills} symbol={symbol} asOf={state.as_of ?? ""} />
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>
                  {bars.length} completed bars visible · last close {bars.length ? num(bars[bars.length - 1].close) : "—"} at {bars.length ? utcShort(bars[bars.length - 1].bar_end) : "—"}
                </span>
                <span className="flex items-center gap-2">
                  <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={probeFuture}>
                    Try reading one week ahead
                  </Button>
                  {probe && <span className="font-mono">{probe}</span>}
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Equity and drawdown</CardTitle>
              <CardDescription>Marked at the last completed close. Dashed line is cash. Shaded area is drawdown from peak.</CardDescription>
            </CardHeader>
            <CardContent>
              <EquityChart points={bundle.equity} initialCash={state.config.initial_cash} />
            </CardContent>
          </Card>

          <Tabs defaultValue="news">
            <TabsList>
              <TabsTrigger value="news">News feed ({bundle.news.length})</TabsTrigger>
              <TabsTrigger value="fills">Fills ({bundle.fills.length})</TabsTrigger>
              <TabsTrigger value="rejections">Rejections ({bundle.rejections.length})</TabsTrigger>
              <TabsTrigger value="log">Decision log ({bundle.records.length})</TabsTrigger>
              {bundle.report && <TabsTrigger value="report">Report</TabsTrigger>}
            </TabsList>

            <TabsContent value="news">
              <Card>
                <CardContent className="p-0">
                  {bundle.news.length === 0 ? (
                    <p className="p-4 text-sm text-muted-foreground">No eligible records yet. Filings appear once published, available and released by the simulated clock.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Symbol</TableHead>
                          <TableHead>Record</TableHead>
                          <TableHead>Published (UTC)</TableHead>
                          <TableHead>Available</TableHead>
                          <TableHead>Released</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {bundle.news.map((n) => (
                          <TableRow key={n.event_id}>
                            <TableCell className="font-mono">{n.symbol}</TableCell>
                            <TableCell>
                              <a href={n.source_url} target="_blank" rel="noreferrer" className="hover:underline">
                                {n.title}
                              </a>
                              <div className="text-xs text-muted-foreground">
                                {n.provider} · {n.availability_verified ? "availability verified" : "provider timestamp"}
                              </div>
                            </TableCell>
                            <TableCell className="font-mono text-xs">{utc(n.published_at)}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(n.available_at)}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(n.released_at)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="fills">
              <Card>
                <CardContent className="p-0">
                  {bundle.fills.length === 0 ? (
                    <p className="p-4 text-sm text-muted-foreground">No fills yet.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Order</TableHead>
                          <TableHead>Side</TableHead>
                          <TableHead className="text-right">Qty</TableHead>
                          <TableHead>Activated</TableHead>
                          <TableHead>Filled (bar start)</TableHead>
                          <TableHead className="text-right">Bar open</TableHead>
                          <TableHead className="text-right">Fill</TableHead>
                          <TableHead className="text-right">Costs</TableHead>
                          <TableHead className="text-right">Cash after</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {bundle.fills.map((f) => (
                          <TableRow key={f.order_id + f.filled_at}>
                            <TableCell className="font-mono text-xs">
                              {f.order_id}
                              <div className="text-muted-foreground">{f.request_id}</div>
                            </TableCell>
                            <TableCell className={`font-mono ${f.side === "BUY" ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}`}>
                              {f.side} {f.symbol}
                            </TableCell>
                            <TableCell className="text-right font-mono">{f.quantity}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(f.execute_at)}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(f.filled_at)}</TableCell>
                            <TableCell className="text-right font-mono">{num(f.bar_open, 4)}</TableCell>
                            <TableCell className="text-right font-mono">{num(f.fill_price, 4)}</TableCell>
                            <TableCell className="text-right font-mono text-xs">{money(f.commission + f.slippage_cost, 4)}</TableCell>
                            <TableCell className="text-right font-mono">{money(f.cash_after)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="rejections">
              <Card>
                <CardContent className="p-0">
                  {bundle.rejections.length === 0 ? (
                    <p className="p-4 text-sm text-muted-foreground">No execution rejections. Whole-response validation failures are in the decision log.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Order</TableHead>
                          <TableHead>Side</TableHead>
                          <TableHead className="text-right">Qty</TableHead>
                          <TableHead>Activated</TableHead>
                          <TableHead>Rejected at</TableHead>
                          <TableHead>Reason</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {bundle.rejections.map((r, i) => (
                          <TableRow key={r.order_id + i}>
                            <TableCell className="font-mono text-xs">{r.order_id}</TableCell>
                            <TableCell className="font-mono">
                              {r.side} {r.symbol}
                            </TableCell>
                            <TableCell className="text-right font-mono">{r.quantity}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(r.execute_at)}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(r.at)}</TableCell>
                            <TableCell>
                              <Badge variant="outline" className="font-mono">
                                {r.reason}
                              </Badge>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="log">
              <Card>
                <CardContent className="p-0">
                  {bundle.records.length === 0 ? (
                    <p className="p-4 text-sm text-muted-foreground">No decisions submitted yet.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="w-14">Step</TableHead>
                          <TableHead>as_of</TableHead>
                          <TableHead>Outcome</TableHead>
                          <TableHead>Reason given</TableHead>
                          <TableHead className="text-right">News released</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {[...bundle.records].reverse().map((rec) => (
                          <TableRow key={rec.request_id}>
                            <TableCell className="font-mono text-xs">{rec.step}</TableCell>
                            <TableCell className="font-mono text-xs">{utc(rec.as_of)}</TableCell>
                            <TableCell className="text-xs">
                              {!rec.accepted ? (
                                <span className="text-rose-700 dark:text-rose-400">response rejected: {rec.errors.map((e) => e.code).join(", ")}</span>
                              ) : rec.order_outcomes.length === 0 ? (
                                <span className="text-muted-foreground">hold</span>
                              ) : (
                                <span className="font-mono">{rec.order_outcomes.map((o) => `${o.order_id} ${o.status}${o.reason ? ` (${o.reason})` : ""}`).join("; ")}</span>
                              )}
                              {!rec.accepted && rec.errors.length > 0 && <div className="text-muted-foreground">{rec.errors.map((e) => e.message).join(" · ")}</div>}
                            </TableCell>
                            <TableCell className="max-w-xs truncate text-xs text-muted-foreground">{typeof rec.response?.reason === "string" ? rec.response.reason : "—"}</TableCell>
                            <TableCell className="text-right font-mono text-xs">{rec.released_news.length}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </TabsContent>

            {bundle.report && (
              <TabsContent value="report">
                <ReportCard report={bundle.report} onReplay={runReplay} replay={replay} />
              </TabsContent>
            )}
          </Tabs>
        </div>

        <aside className="space-y-6 xl:sticky xl:top-6 xl:self-start">
          {request ? (
            <>
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm">Decision request {request.request_id}</CardTitle>
                  <CardDescription>
                    {request.mode === "V1" ? "Orders activate at the boundary and fill at this bar's open." : `Schedule activations inside the window; with ${request.clock.interval} bars they fill at the next bar open.`}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <OrderForm key={request.request_id} request={request} busy={busy} onSubmit={submit} />
                  <details className="text-xs">
                    <summary className="cursor-pointer text-muted-foreground">Request JSON</summary>
                    <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-muted p-2 font-mono text-[11px] leading-snug">{JSON.stringify(request, null, 2)}</pre>
                  </details>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-sm">Test agent</CardTitle>
                  <CardDescription>Deterministic agents read only the released feed and return the fixed JSON envelope.</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="grid grid-cols-2 gap-2">
                    <div className="space-y-1">
                      <Label className="text-xs">Agent</Label>
                      <Select value={agent} onValueChange={(v) => setAgent(String(v))}>
                        <SelectTrigger size="sm" className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="hold">hold</SelectItem>
                          <SelectItem value="buy_and_hold">buy_and_hold</SelectItem>
                          <SelectItem value="script">script</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs">Steps</Label>
                      <Input className="h-8 font-mono" type="number" min={1} max={10000} value={steps} onChange={(e) => setSteps(e.target.value)} />
                    </div>
                  </div>
                  {agent === "script" && config && (
                    <div className="space-y-1">
                      <Label className="text-xs">Script</Label>
                      <Select value={script} onValueChange={(v) => setScript(String(v))}>
                        <SelectTrigger size="sm" className="w-full font-mono">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {config.scripts.map((s) => (
                            <SelectItem key={s.id} value={s.id} className="font-mono">
                              {s.id}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{config.scripts.find((s) => s.id === script)?.description}</p>
                    </div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" size="sm" onClick={() => autoplay(1)} disabled={busy}>
                      Step once
                    </Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => autoplay(Math.max(1, Number(steps) || 1))} disabled={busy}>
                      Advance {steps}
                    </Button>
                    <Button type="button" size="sm" variant="secondary" onClick={() => autoplay(10000)} disabled={busy}>
                      Run to end
                    </Button>
                  </div>
                </CardContent>
              </Card>
            </>
          ) : (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Run finished</CardTitle>
                <CardDescription>The official report is produced from the ledger. Open the Report tab or replay the recorded action log.</CardDescription>
              </CardHeader>
            </Card>
          )}

          {last && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Last acknowledgement</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1 text-xs">
                <div>
                  {last.ack.accepted ? <span className="text-emerald-700 dark:text-emerald-400">response accepted</span> : <span className="text-rose-700 dark:text-rose-400">response rejected</span>} ·{" "}
                  <span className="font-mono">{last.ack.orders.length}</span> orders
                </div>
                {last.ack.errors.map((e, i) => (
                  <div key={i} className="font-mono text-rose-700 dark:text-rose-400">
                    {e.code}: {e.message}
                  </div>
                ))}
                {last.ack.orders.map((o) => (
                  <div key={o.order_id} className="font-mono">
                    {o.order_id} → {o.status}
                    {o.reason ? ` (${o.reason})` : ""}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {request && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">News digest in this request</CardTitle>
                <CardDescription>
                  {digest.length} of {request.data.news_event_ids.length} ids resolved · held symbols first
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-xs">
                {digest.length === 0 ? (
                  <p className="text-muted-foreground">No eligible records for these symbols yet.</p>
                ) : (
                  digest.slice(0, 8).map((n) => (
                    <div key={n.event_id} className="border-l-2 pl-2">
                      <div className="font-mono text-muted-foreground">
                        {n.symbol} · {utcShort(n.available_at)}
                      </div>
                      <a href={n.source_url} target="_blank" rel="noreferrer" className="hover:underline">
                        {n.title}
                      </a>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          )}

          {state.pending_orders.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Pending orders</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1 font-mono text-xs">
                {state.pending_orders.map((p) => (
                  <div key={p.order_id}>
                    {p.side} {p.quantity} {p.symbol} @ {utcShort(p.execute_at)}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </aside>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "pos" | "neg" }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`mt-1 font-mono text-lg ${tone === "pos" ? "text-emerald-700 dark:text-emerald-400" : tone === "neg" ? "text-rose-700 dark:text-rose-400" : ""}`}>{value}</div>
      {sub && <div className="truncate text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function ReportCard({ report, onReplay, replay }: { report: Report; onReplay: () => void; replay: { match: boolean; actions_replayed: number } | null }) {
  const rows: [string, string][] = [
    ["Initial cash K", money(report.initial_cash)],
    ["Final equity", money(report.final_equity)],
    ["Net profit", signed(report.net_profit)],
    ["Return", pct(report.return_pct)],
    ["Max drawdown", `${num(report.max_drawdown_pct)}%`],
    ["Sharpe", report.sharpe === null ? "n/a (too few periods)" : `${num(report.sharpe)} (${report.sharpe_periods_per_year} periods/yr)`],
    ["Turnover", num(report.turnover, 3)],
    ["Traded notional", money(report.traded_notional)],
    ["Commissions", money(report.commissions, 4)],
    ["Slippage cost", money(report.slippage_cost, 4)],
    ["Fills / rejections", `${report.fills} / ${report.rejections}`],
    ["Invalid responses / holds", `${report.invalid_responses} / ${report.held_requests}`],
    ["Decisions", String(report.steps)],
    ["Environment / data", `${report.environment_version} · ${report.data_version}`],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">Official report</CardTitle>
        <CardDescription>{report.valuation_convention}.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-4 border-b py-1">
              <dt className="text-muted-foreground">{k}</dt>
              <dd className="font-mono">{v}</dd>
            </div>
          ))}
        </dl>
        {report.final_positions.length > 0 && (
          <div className="text-sm">
            <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">Final positions</div>
            <div className="font-mono text-xs">{report.final_positions.map((p) => `${p.quantity} ${p.symbol} @ ${num(p.mark_price ?? 0)}`).join(" · ")}</div>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" size="sm" variant="outline" onClick={onReplay}>
            Replay recorded actions
          </Button>
          {replay && (
            <span className={`font-mono text-xs ${replay.match ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}`}>
              {replay.actions_replayed} actions replayed · {replay.match ? "identical ledger result" : "MISMATCH"}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
