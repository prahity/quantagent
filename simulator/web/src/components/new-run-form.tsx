"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { api, ApiError, type Config } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

const PRESETS = [
  { id: "week_hourly", label: "One week, hourly", start: "2025-01-27", end: "2025-01-31", interval: "hourly" },
  { id: "q1_daily", label: "Q1 2025, daily", start: "2025-01-02", end: "2025-03-31", interval: "daily" },
  { id: "val_2026q2", label: "Validation window (2026 Q2), daily", start: "2026-04-01", end: "2026-06-30", interval: "daily" },
];

export function NewRunForm({ config, onCreated }: { config: Config; onCreated: () => void }) {
  const router = useRouter();
  const env = config.environment;
  const all = config.universe.symbols.map((s) => s.ticker);
  const [interval, setInterval] = useState<string>(String(env.interval ?? "hourly"));
  const [mode, setMode] = useState<string>(String(env.mode ?? "V1"));
  const [start, setStart] = useState("2025-01-27");
  const [end, setEnd] = useState("2025-01-31");
  const [cash, setCash] = useState(String(env.initial_cash ?? 10000));
  const [label, setLabel] = useState("");
  const [symbols, setSymbols] = useState<string[]>(["AAPL", "MSFT", "NVDA"]);
  const [slippage, setSlippage] = useState(String(env.execution?.slippage_bps ?? 1));
  const [commission, setCommission] = useState(String(env.execution?.commission_per_share ?? 0));
  const [cap, setCap] = useState(String(Number(env.execution?.max_position_pct_equity ?? 0.25) * 100));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const estimate = useMemo(() => {
    const days = Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 86400000) + 1);
    const trading = Math.round(days * (5 / 7));
    return interval === "hourly" ? `≈ ${trading * 7} decisions` : `≈ ${trading} decisions`;
  }, [start, end, interval]);

  function toggle(sym: string) {
    setSymbols((cur) => (cur.includes(sym) ? cur.filter((s) => s !== sym) : [...cur, sym]));
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const req = await api.createRun({
        interval,
        mode,
        start,
        end,
        initial_cash: Number(cash),
        symbols,
        label: label || undefined,
        execution: {
          slippage_bps: Number(slippage),
          commission_per_share: Number(commission),
          max_position_pct_equity: Number(cap) / 100,
        },
      });
      onCreated();
      router.push(`/runs/${req.run_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">New run</CardTitle>
        <CardDescription>Capital and clock are separate settings. Data outside the snapshot range is unavailable.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="space-y-4">
          <div className="flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <Button
                key={p.id}
                type="button"
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                onClick={() => {
                  setStart(p.start);
                  setEnd(p.end);
                  setInterval(p.interval);
                }}
              >
                {p.label}
              </Button>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Bar interval</Label>
              <Tabs value={interval} onValueChange={(v) => setInterval(String(v))}>
                <TabsList className="w-full">
                  <TabsTrigger value="hourly" className="flex-1">
                    Hourly
                  </TabsTrigger>
                  <TabsTrigger value="daily" className="flex-1">
                    Daily
                  </TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
            <div className="space-y-1.5">
              <Label>Mode</Label>
              <Tabs value={mode} onValueChange={(v) => setMode(String(v))}>
                <TabsList className="w-full">
                  <TabsTrigger value="V1" className="flex-1">
                    V1 immediate
                  </TabsTrigger>
                  <TabsTrigger value="V2" className="flex-1">
                    V2 scheduled
                  </TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="start">Start</Label>
              <Input id="start" type="date" min={config.universe.data_start} max={config.universe.data_end} value={start} onChange={(e) => setStart(e.target.value)} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="end">End</Label>
              <Input id="end" type="date" min={config.universe.data_start} max={config.universe.data_end} value={end} onChange={(e) => setEnd(e.target.value)} required />
            </div>
          </div>
          <p className="-mt-2 text-xs text-muted-foreground">{estimate} · one request at every bar start</p>

          <div className="space-y-1.5">
            <Label>Symbols ({symbols.length})</Label>
            <div className="flex flex-wrap gap-1.5">
              {all.map((sym) => {
                const on = symbols.includes(sym);
                return (
                  <button
                    key={sym}
                    type="button"
                    onClick={() => toggle(sym)}
                    className={`rounded-md border px-2 py-0.5 font-mono text-xs transition-colors ${on ? "border-foreground bg-foreground text-background" : "hover:bg-muted"}`}
                  >
                    {sym}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="cash">Initial cash K</Label>
              <Input id="cash" type="number" min={1} step={100} value={cash} onChange={(e) => setCash(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="label">Label</Label>
              <Input id="label" placeholder="optional" value={label} onChange={(e) => setLabel(e.target.value)} />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="slip">Slippage bps</Label>
              <Input id="slip" type="number" min={0} step={0.5} value={slippage} onChange={(e) => setSlippage(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="comm">$/share</Label>
              <Input id="comm" type="number" min={0} step={0.001} value={commission} onChange={(e) => setCommission(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap">Max pos %</Label>
              <Input id="cap" type="number" min={1} max={100} step={1} value={cap} onChange={(e) => setCap(e.target.value)} />
            </div>
          </div>

          {error && <p className="text-xs text-destructive">{error}</p>}
          <Button type="submit" className="w-full" disabled={busy || symbols.length === 0}>
            {busy ? "Starting…" : "Start run"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
