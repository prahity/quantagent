import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "AI Trader Simulator",
  description: "Independent market simulator and evaluator: time-gated real prices and filings, fixed JSON decisions, ledger-based scoring.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <header className="border-b">
          <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
            <Link href="/" className="flex items-baseline gap-2">
              <span className="text-sm font-semibold tracking-tight">AI Trader</span>
              <span className="text-xs text-muted-foreground">simulator &amp; evaluator</span>
            </Link>
            <nav className="flex items-center gap-4 text-xs text-muted-foreground">
              <Link href="/" className="hover:text-foreground">
                Runs
              </Link>
              <Link href="/data" className="hover:text-foreground">
                Data
              </Link>
              <a href="/api/config" className="hover:text-foreground" target="_blank" rel="noreferrer">
                API
              </a>
            </nav>
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
