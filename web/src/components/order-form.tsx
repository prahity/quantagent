"use client";

import { useState } from "react";
import type { DecisionRequest } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

type Draft = { order_id: string; symbol: string; side: "BUY" | "SELL"; quantity: number; offset_seconds: number; order_type: "MARKET" | "LIMIT"; limit_price: number | null };

export function buildEnvelope(request: DecisionRequest, drafts: Draft[], reason: string) {
  const start = new Date(request.window_start).getTime();
  return {
    schema_version: "1.0",
    run_id: request.run_id,
    request_id: request.request_id,
    mode: request.mode,
    window_start: request.window_start,
    window_end: request.window_end,
    orders: drafts.map((d) => ({
      order_id: d.order_id,
      execute_at: new Date(start + (request.mode === "V2" ? d.offset_seconds : 0) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z"),
      symbol: d.symbol,
      side: d.side,
      quantity: d.quantity,
      order_type: d.order_type,
      limit_price: d.order_type === "LIMIT" ? d.limit_price : null,
    })),
    reason,
  };
}

export function OrderForm({ request, busy, onSubmit }: { request: DecisionRequest; busy: boolean; onSubmit: (payload: unknown) => Promise<void> }) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [symbol, setSymbol] = useState(request.data.symbols[0] ?? "");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("1");
  const [offset, setOffset] = useState("0");
  const [type, setType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [limit, setLimit] = useState("");
  const [reason, setReason] = useState("manual command from the web interface");

  const windowSeconds = Math.round((new Date(request.window_end).getTime() - new Date(request.window_start).getTime()) / 1000);

  function add() {
    const q = Number(qty);
    if (!symbol || !Number.isFinite(q) || q <= 0) return;
    setDrafts((cur) => [
      ...cur,
      {
        order_id: `${request.request_id}_m${String(cur.length + 1).padStart(2, "0")}`,
        symbol,
        side,
        quantity: Math.floor(q),
        offset_seconds: Number(offset) || 0,
        order_type: type,
        limit_price: type === "LIMIT" ? Number(limit) || null : null,
      },
    ]);
  }

  async function send(orders: Draft[]) {
    await onSubmit(buildEnvelope(request, orders, orders.length ? reason : "hold"));
    setDrafts([]);
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-6">
        <div className="space-y-1">
          <Label className="text-xs">Symbol</Label>
          <Select value={symbol} onValueChange={(v) => setSymbol(String(v))}>
            <SelectTrigger size="sm" className="w-full font-mono">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {request.data.symbols.map((s) => (
                <SelectItem key={s} value={s} className="font-mono">
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Side</Label>
          <Select value={side} onValueChange={(v) => setSide(v as "BUY" | "SELL")}>
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="BUY">BUY</SelectItem>
              <SelectItem value="SELL">SELL</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Quantity</Label>
          <Input className="h-8 font-mono" type="number" min={1} step={1} value={qty} onChange={(e) => setQty(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Type</Label>
          <Select value={type} onValueChange={(v) => setType(v as "MARKET" | "LIMIT")}>
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="MARKET">MARKET</SelectItem>
              <SelectItem value="LIMIT">LIMIT</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">{type === "LIMIT" ? "Limit price" : "Limit (n/a)"}</Label>
          <Input className="h-8 font-mono" type="number" step={0.01} disabled={type !== "LIMIT"} value={limit} onChange={(e) => setLimit(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label className="text-xs">{request.mode === "V2" ? `Offset s (0–${windowSeconds - 1})` : "Offset (V1: boundary)"}</Label>
          <Input className="h-8 font-mono" type="number" min={0} step={1} disabled={request.mode !== "V2"} value={offset} onChange={(e) => setOffset(e.target.value)} />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" variant="outline" onClick={add} disabled={busy}>
          Add order to plan
        </Button>
        <Input className="h-8 flex-1 text-xs" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="reason (recorded, not scored)" />
      </div>

      {drafts.length > 0 && (
        <ul className="divide-y rounded-md border text-xs">
          {drafts.map((d, i) => (
            <li key={d.order_id} className="flex items-center justify-between gap-2 px-2 py-1.5 font-mono">
              <span>
                <span className={d.side === "BUY" ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}>{d.side}</span> {d.quantity} {d.symbol} {d.order_type}
                {d.limit_price ? ` @${d.limit_price}` : ""} {request.mode === "V2" ? `+${d.offset_seconds}s` : ""}
              </span>
              <button type="button" className="text-muted-foreground hover:text-foreground" onClick={() => setDrafts((cur) => cur.filter((_, j) => j !== i))}>
                remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-2">
        <Button type="button" size="sm" onClick={() => send(drafts)} disabled={busy || drafts.length === 0}>
          Submit {drafts.length} order{drafts.length === 1 ? "" : "s"}
        </Button>
        <Button type="button" size="sm" variant="secondary" onClick={() => send([])} disabled={busy}>
          Hold this {request.clock.interval === "hourly" ? "hour" : "day"}
        </Button>
      </div>
    </div>
  );
}
