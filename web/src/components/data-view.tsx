"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Config } from "@/lib/api";
import { utc } from "@/lib/format";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function DataView() {
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .config()
      .then(setConfig)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Simulator API unreachable</AlertTitle>
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (!config) return <Skeleton className="h-64 w-full" />;

  const intervals = Object.keys(config.snapshot.coverage);
  const sources = (config.snapshot.manifest.sources ?? {}) as Record<string, Record<string, unknown>>;

  return (
    <div className="space-y-6">
      <section>
        <h1 className="text-xl font-semibold tracking-tight">Frozen snapshot</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          <span className="font-mono">{config.snapshot.data_version}</span>
          {config.snapshot.manifest.fetched_at ? <> · fetched {utc(config.snapshot.manifest.fetched_at)}</> : null}. The archive includes future rows; runs only ever see
          bars that have completed and records that were published, available and released by the current <span className="font-mono">as_of</span>.
        </p>
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Universe</CardTitle>
            <CardDescription>Declared in config/universe.yaml. Expansion instructions for 20–30 symbols are in that file.</CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Ticker</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Sector</TableHead>
                  <TableHead className="text-right">Filings</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {config.universe.symbols.map((s) => (
                  <TableRow key={s.ticker}>
                    <TableCell className="font-mono">{s.ticker}</TableCell>
                    <TableCell>{s.name}</TableCell>
                    <TableCell className="text-muted-foreground">{s.sector}</TableCell>
                    <TableCell className="text-right font-mono">{config.snapshot.news.by_symbol[s.ticker] ?? 0}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Chronological splits</CardTitle>
            <CardDescription>From config/splits.yaml. The sealed interval is reserved before any strategy development.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">Development</div>
              <div className="flex flex-wrap gap-1.5">
                {config.splits.development.map((w) => (
                  <Badge key={w.id} variant="outline" className="font-mono">
                    {w.start} → {w.end}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">Validation</div>
              <div className="flex flex-wrap gap-1.5">
                {config.splits.validation.map((w) => (
                  <Badge key={w.id} variant="outline" className="font-mono">
                    {w.start} → {w.end}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">Sealed test</div>
              <Badge className="font-mono">
                {config.splits.sealed.start} → {config.splits.sealed.end}
              </Badge>
            </div>
          </CardContent>
        </Card>
      </div>

      {intervals.map((interval) => (
        <Card key={interval}>
          <CardHeader>
            <CardTitle className="text-sm capitalize">{interval} bars</CardTitle>
            <CardDescription>Yahoo Finance chart API, regular session only. {interval === "daily" ? "Daily bars carry adj_close." : "Hourly bars are unadjusted; the last bar of each day is 30 minutes."}</CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Symbol</TableHead>
                  <TableHead className="text-right">Bars</TableHead>
                  <TableHead>First</TableHead>
                  <TableHead>Last</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(config.snapshot.coverage[interval]).map(([sym, c]) => (
                  <TableRow key={sym}>
                    <TableCell className="font-mono">{sym}</TableCell>
                    <TableCell className="text-right font-mono">{c.rows.toLocaleString()}</TableCell>
                    <TableCell className="font-mono text-xs">{utc(c.first)}</TableCell>
                    <TableCell className="font-mono text-xs">{utc(c.last)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ))}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">News and context sources</CardTitle>
          <CardDescription>
            {config.snapshot.news.rows} records.{" "}
            {Object.entries(config.snapshot.news.by_provider)
              .map(([p, n]) => `${p}: ${n}`)
              .join(" · ")}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {Object.entries((sources.news ?? {}) as Record<string, string>).map(([k, v]) => (
            <div key={k} className="grid gap-1 sm:grid-cols-[160px_1fr]">
              <span className="font-mono text-xs">{k}</span>
              <span className="text-muted-foreground">{v}</span>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
