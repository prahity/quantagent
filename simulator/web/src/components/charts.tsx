"use client";

import { Area, Bar, CartesianGrid, ComposedChart, Line, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Bar as BarRow, EquityPoint, Fill } from "@/lib/api";
import { money, num, utcShort } from "@/lib/format";

const AXIS = { fontSize: 11, fill: "var(--muted-foreground)" };

export function PriceChart({ bars, fills, symbol, asOf }: { bars: BarRow[]; fills: Fill[]; symbol: string; asOf: string }) {
  const data = bars.map((b) => ({ ...b, t: new Date(b.ts).getTime() }));
  const fillDots = fills
    .filter((f) => f.symbol === symbol)
    .map((f) => ({ t: new Date(f.filled_at).getTime(), price: f.fill_price, side: f.side, qty: f.quantity }));
  if (data.length === 0) {
    return (
      <div className="flex h-72 items-center justify-center text-sm text-muted-foreground">
        No completed {symbol} bars have been released yet at {utcShort(asOf)}.
      </div>
    );
  }
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time" tickFormatter={(v) => utcShort(new Date(v).toISOString())} tick={AXIS} minTickGap={48} />
          <YAxis yAxisId="price" domain={["auto", "auto"]} tick={AXIS} width={62} tickFormatter={(v) => num(v, 0)} />
          <YAxis yAxisId="vol" orientation="right" tick={AXIS} width={48} tickFormatter={(v) => `${Math.round(v / 1e6)}M`} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--border)", background: "var(--popover)", color: "var(--popover-foreground)" }}
            labelFormatter={(v) => `bar start ${utcShort(new Date(Number(v)).toISOString())} UTC`}
            formatter={(value, name) => {
              if (name === "volume") return [Number(value).toLocaleString(), "volume"];
              return [num(Number(value), 2), String(name)];
            }}
          />
          <Bar yAxisId="vol" dataKey="volume" fill="var(--muted)" isAnimationActive={false} />
          <Line yAxisId="price" type="monotone" dataKey="close" stroke="var(--foreground)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
          {fillDots.map((d, i) => (
            <ReferenceDot key={i} yAxisId="price" x={d.t} y={d.price} r={5} fill={d.side === "BUY" ? "#059669" : "#e11d48"} stroke="var(--background)" strokeWidth={1.5} />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export function EquityChart({ points, initialCash }: { points: EquityPoint[]; initialCash: number }) {
  const data = points.map((p) => ({ ...p, t: new Date(p.ts).getTime(), dd: -p.drawdown * 100 }));
  if (data.length < 2) {
    return <div className="flex h-72 items-center justify-center text-sm text-muted-foreground">Equity is recorded after the first decision.</div>;
  }
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time" tickFormatter={(v) => utcShort(new Date(v).toISOString())} tick={AXIS} minTickGap={48} />
          <YAxis yAxisId="eq" domain={["auto", "auto"]} tick={AXIS} width={70} tickFormatter={(v) => money(v, 0)} />
          <YAxis yAxisId="dd" orientation="right" domain={[(min: number) => Math.min(min, -1), 0]} tick={AXIS} width={44} tickFormatter={(v) => `${num(v, 1)}%`} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--border)", background: "var(--popover)", color: "var(--popover-foreground)" }}
            labelFormatter={(v) => utcShort(new Date(Number(v)).toISOString()) + " UTC"}
            formatter={(value, name) => {
              if (name === "dd") return [`${num(Number(value), 2)}%`, "drawdown"];
              return [money(Number(value)), String(name)];
            }}
          />
          <Area yAxisId="dd" type="monotone" dataKey="dd" fill="#e11d48" fillOpacity={0.12} stroke="#e11d48" strokeWidth={1} isAnimationActive={false} />
          <Line yAxisId="eq" type="monotone" dataKey="equity" stroke="var(--foreground)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
          <Line yAxisId="eq" type="stepAfter" dataKey="cash" stroke="var(--muted-foreground)" dot={false} strokeWidth={1} strokeDasharray="4 3" isAnimationActive={false} />
          <ReferenceDot yAxisId="eq" x={data[0].t} y={initialCash} r={0} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
