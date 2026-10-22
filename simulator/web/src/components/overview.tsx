"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Config, type RunSummary } from "@/lib/api";
import { money, signed, utcShort } from "@/lib/format";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { NewRunForm } from "@/components/new-run-form";

export function Overview() {
  const [config, setConfig] = useState<Config | null>(null);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [cfg, list] = await Promise.all([api.config(), api.runs()]);
      setConfig(cfg);
      setRuns(list);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Simulator API unreachable</AlertTitle>
        <AlertDescription>
          <p>{error}</p>
          <p className="mt-1">
            Start it with <code className="font-mono">uvicorn server.app:app --port 8711</code>. If the message mentions a missing snapshot, run{" "}
            <code className="font-mono">python scripts/fetch_data.py</code> first.
          </p>
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
      <div className="space-y-6">
        <section>
          <h1 className="text-xl font-semibold tracking-tight">Runs</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Each run is an isolated account starting at <span className="font-mono">K</span>. The simulator owns the clock, releases real prices and SEC
            filings only once they were historically available, fills orders at the next bar open, and scores from its own ledger.
          </p>
        </section>

        {runs === null ? (
          <Skeleton className="h-40 w-full" />
        ) : runs.length === 0 ? (
          <Card>
            <CardHeader>
              <CardTitle>No runs yet</CardTitle>
              <CardDescription>Create one on the right. A short hourly window over two or three symbols finishes in a few seconds with the bundled scripts.</CardDescription>
            </CardHeader>
          </Card>
        ) : (
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Window</TableHead>
                    <TableHead>Clock</TableHead>
                    <TableHead>Progress</TableHead>
                    <TableHead className="text-right">Equity</TableHead>
                    <TableHead className="text-right">Net P&amp;L</TableHead>
                    <TableHead className="text-right">Fills / Rej.</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((r) => (
                    <TableRow key={r.run_id}>
                      <TableCell>
                        <Link href={`/runs/${r.run_id}`} className="font-medium hover:underline">
                          {r.run_id}
                        </Link>
                        <div className="text-xs text-muted-foreground">{r.label ?? "—"}</div>
                      </TableCell>
                      <TableCell className="text-xs">
                        <div>
                          {r.start} → {r.end}
                        </div>
                        <div className="text-muted-foreground">
                          {r.interval} · {r.mode} · {r.symbols.length} symbols
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{utcShort(r.as_of)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          <Badge variant={r.status === "finished" ? "secondary" : "default"}>{r.status === "finished" ? "finished" : "live"}</Badge>
                          <span className="font-mono text-xs text-muted-foreground">
                            {Math.min(r.step + (r.status === "finished" ? 1 : 0), r.steps_total)}/{r.steps_total}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right font-mono text-sm">{money(r.equity)}</TableCell>
                      <TableCell className={`text-right font-mono text-sm ${(r.equity - r.initial_cash) >= 0 ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}`}>
                        {signed(r.equity - r.initial_cash)}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs">
                        {r.fills} / {r.rejections}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}

        {config && (
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Frozen snapshot {config.snapshot.data_version}</CardTitle>
              <CardDescription>
                {config.universe.symbols.length} symbols · {config.universe.data_start} → {config.universe.data_end} · hourly and daily Yahoo Finance bars ·{" "}
                {config.snapshot.news.rows} SEC EDGAR filings. <Link href="/data" className="underline">Coverage details</Link>.
              </CardDescription>
            </CardHeader>
          </Card>
        )}
      </div>

      <aside className="lg:sticky lg:top-6 lg:self-start">
        {config ? <NewRunForm config={config} onCreated={load} /> : <Skeleton className="h-96 w-full" />}
      </aside>
    </div>
  );
}
