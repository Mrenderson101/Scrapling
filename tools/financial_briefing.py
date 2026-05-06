#!/usr/bin/env python3
"""Financial briefing via Scrapling.

Pulls a snapshot per ticker (price, change, market cap, P/E, perf) and prints a
markdown briefing. Default source is Finviz (no consent loop). Yahoo Finance is
available via `--source yahoo` and uses StealthyFetcher to bypass the EU GDPR
consent wall.

Usage:
  . .venv/bin/activate
  python tools/financial_briefing.py
  python tools/financial_briefing.py AAPL MSFT NVDA
  python tools/financial_briefing.py --source yahoo AAPL
  python tools/financial_briefing.py --out briefing.md
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]

KEY_METRICS = [
    "Market Cap",
    "P/E",
    "Forward P/E",
    "EPS (ttm)",
    "Dividend TTM",
    "52W Range",
    "Perf Week",
    "Perf Month",
    "Perf YTD",
    "Volatility",
]


@dataclass
class Quote:
    ticker: str
    name: str = ""
    price: str = ""
    change: str = ""
    metrics: dict = field(default_factory=dict)
    source_url: str = ""
    error: str = ""


def fetch_finviz(ticker: str) -> Quote:
    from scrapling.fetchers import Fetcher

    url = f"https://finviz.com/quote.ashx?t={ticker}"
    q = Quote(ticker=ticker, source_url=url)
    try:
        r = Fetcher.get(url, timeout=20)
        if r.status != 200:
            q.error = f"HTTP {r.status}"
            return q

        title = r.css("title::text").get() or ""
        q.name = title.split(" Stock Price")[0].replace(f"{ticker} - ", "").strip()
        q.price = (r.css("strong[class*=quote-price] ::text").get() or "").strip()
        change_parts = [
            t.strip()
            for t in r.css(".quote-price_wrapper [class*=change] ::text").getall()
            if t.strip() and not t.strip().lower().startswith(("dollar", "percentage"))
        ]
        q.change = " ".join(change_parts)

        for row in r.css(".snapshot-table2 tr"):
            cells = [
                " ".join(td.css("::text").getall()).strip() for td in row.css("td")
            ]
            for i in range(0, len(cells) - 1, 2):
                q.metrics[cells[i]] = cells[i + 1]
    except Exception as exc:
        q.error = f"{type(exc).__name__}: {exc}"
    return q


def _accept_yahoo_consent(page) -> None:
    """Click Yahoo's GDPR consent button if present, else no-op."""
    selectors = [
        'button[name="agree"]',
        'button[value="agree"]',
        'button.accept-all',
        'button:has-text("Alles accepteren")',
        'button:has-text("Accept all")',
        'button:has-text("Accepteer alle")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=15000)
                return
        except Exception:
            continue


def fetch_yahoo(ticker: str) -> Quote:
    from scrapling.fetchers import StealthyFetcher

    url = f"https://finance.yahoo.com/quote/{ticker}/"
    q = Quote(ticker=ticker, source_url=url)
    try:
        r = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            humanize=True,
            solve_cloudflare=False,
            page_action=_accept_yahoo_consent,
            wait_selector='fin-streamer[data-field="regularMarketPrice"]',
            wait_selector_state="attached",
            timeout=45000,
        )
        if r.status != 200:
            q.error = f"HTTP {r.status}"
            return q
        q.name = (r.css("h1::text").get() or "").strip()
        q.price = (
            r.css('[data-testid="qsp-price"]::text').get()
            or r.css('fin-streamer[data-field="regularMarketPrice"]::text').get()
            or ""
        ).strip()
        q.change = (
            r.css('[data-testid="qsp-price-change"]::text').get()
            or r.css('fin-streamer[data-field="regularMarketChangePercent"]::text').get()
            or ""
        ).strip()
        for li in r.css('[data-testid="quote-statistics"] li'):
            label = (li.css("span::text").get() or "").strip()
            value = " ".join(
                t.strip() for t in li.css("fin-streamer ::text, span ::text").getall()
            ).strip()
            if label and value:
                q.metrics[label] = value
    except Exception as exc:
        q.error = f"{type(exc).__name__}: {exc}"
    return q


def render_briefing(quotes: list[Quote], source: str) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# Financial briefing — {now}",
        f"_Source: {source}_",
        "",
    ]
    for q in quotes:
        header = f"## {q.ticker}"
        if q.name:
            header += f" — {q.name}"
        lines.append(header)
        if q.error:
            lines.append(f"> ⚠️  Failed: {q.error}")
            lines.append("")
            continue
        lines.append(f"- **Price:** {q.price or 'n/a'}")
        if q.change:
            lines.append(f"- **Change:** {q.change}")
        for key in KEY_METRICS:
            if key in q.metrics:
                lines.append(f"- {key}: {q.metrics[key]}")
        lines.append(f"- _Source:_ <{q.source_url}>")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Financial briefing via Scrapling")
    parser.add_argument("tickers", nargs="*", default=DEFAULT_TICKERS)
    parser.add_argument("--source", choices=["finviz", "yahoo"], default="finviz")
    parser.add_argument("--out", help="Write markdown to this file (else stdout)")
    args = parser.parse_args(argv)

    fetcher = fetch_finviz if args.source == "finviz" else fetch_yahoo
    quotes = []
    for ticker in args.tickers:
        ticker = ticker.upper().strip()
        print(f"[fetch] {ticker} via {args.source}...", file=sys.stderr)
        quotes.append(fetcher(ticker))

    output = render_briefing(quotes, args.source)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output)
        print(f"[done] Wrote briefing to {args.out}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
