#!/usr/bin/env python3
"""Daily financial briefing via native Scrapling patterns.

No custom verdict/util layer. Fetches use Scrapling StealthySession with
briefing defaults from the Obsidian runbook, render one markdown briefing and a
manifest under `.hermes-briefings/YYYY-MM-DD/`.

Usage:
  . .venv/bin/activate
  python tools/daily_financial_briefing.py
  python tools/daily_financial_briefing.py --tickers AAPL MSFT NVDA --articles https://...
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from scrapling.fetchers import StealthySession

CONSENT_SDK_DOMAINS = {
    "cmp.inmobi.com",
    "cmp.quantcast.com",
    "mgr.consensu.org",
    "sourcepointcmp.com",
    "sourcepoint.com",
    "cdn.privacy-mgmt.com",
    "consent.cookiebot.com",
    "consent.trustarc.com",
    "privacy-mgmt.com",
    "cmp.uniconsent.com",
    "quantcast.mgr.consensu.org",
    "cmp.admiralcloud.com",
    "cdn.admiralcloud.com",
    "admiral.mgr.consensu.org",
    "confiant-integrations.global.ssl.fastly.net",
    "confiant-integrations.net",
    "securepubads.g.doubleclick.net",
    "urbanlaurel.com",
    "cdn.hadronid.net",
}

BRIEFING_DEFAULTS: dict[str, Any] = dict(
    headless=True,
    network_idle=True,
    humanize=True,
    os_randomize=True,
    solve_cloudflare=True,
    blocked_domains=CONSENT_SDK_DOMAINS,
    timeout=60000,
)

DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "TSLA"]
FINVIZ_MAP_URL = "https://finviz.com/map.ashx?t=sec&st=d1"
STOCKANALYSIS_HEATMAP_URL = "https://stockanalysis.com/markets/heatmap/"
DEFAULT_ARTICLES = [
    "https://www.cnbc.com/2026/05/06/amd-lisa-su-stock-forecast-earnings.html",
]


@dataclass
class TargetStatus:
    name: str
    kind: str
    url: str
    final_url: str = ""
    status: int | None = None
    usable: bool = False
    error: str = ""
    output: str = ""


@dataclass
class QuoteRow:
    ticker: str
    name: str = ""
    price: str = ""
    change: str = ""
    market_cap: str = ""
    pe: str = ""
    source_url: str = ""


@dataclass
class Story:
    title: str
    url: str
    hero: str = ""
    body: str = ""


def is_usable(page) -> bool:
    """Runbook-native pass/fail: status 200 and no consent URL."""
    return (
        getattr(page, "status", None) == 200
        and getattr(page, "url", None) is not None
        and not any(d in (getattr(page, "url", "") or "") for d in ("consent.", "consent_"))
    )


def safe_text(value: Any) -> str:
    return (value or "").strip()


def get_first(page, selector: str) -> str:
    try:
        return safe_text(page.css_first(selector).get())
    except Exception:
        try:
            return safe_text(page.css(selector).get())
        except Exception:
            return ""


def screenshot_heatmap(out_path: Path):
    def action(page):
        page.wait_for_selector("canvas, svg, [class*='map']", state="visible", timeout=15000)
        page.screenshot(path=str(out_path), full_page=False, timeout=15000)

    return action


def fetch_heatmap(session: StealthySession, assets_dir: Path) -> TargetStatus:
    shot = assets_dir / "finviz_heatmap.png"
    status = TargetStatus("finviz_heatmap", "market_heatmap", FINVIZ_MAP_URL, output=str(shot))
    try:
        page = session.fetch(FINVIZ_MAP_URL, page_action=screenshot_heatmap(shot), timeout=60000)
        status.status = getattr(page, "status", None)
        status.final_url = getattr(page, "url", "") or ""
        status.usable = is_usable(page) and shot.exists()
    except Exception as exc:
        status.error = f"{type(exc).__name__}: {exc}"
    return status


def fetch_stockanalysis_heatmap(session: StealthySession, assets_dir: Path) -> TargetStatus:
    shot = assets_dir / "stockanalysis_heatmap.png"
    status = TargetStatus("stockanalysis_heatmap", "market_heatmap", STOCKANALYSIS_HEATMAP_URL, output=str(shot))
    try:
        page = session.fetch(
            STOCKANALYSIS_HEATMAP_URL,
            page_action=screenshot_heatmap(shot),
            wait_selector="body",
            wait_selector_state="visible",
            timeout=60000,
        )
        status.status = getattr(page, "status", None)
        status.final_url = getattr(page, "url", "") or ""
        status.usable = is_usable(page) and shot.exists()
    except Exception as exc:
        status.error = f"{type(exc).__name__}: {exc}"
    return status


def parse_finviz_quote(page, ticker: str, url: str) -> QuoteRow:
    labels = page.css("table.snapshot-table2 td:nth-child(odd)::text").getall()
    values = page.css("table.snapshot-table2 td:nth-child(even)::text").getall()
    metrics = {safe_text(k): safe_text(v) for k, v in zip(labels, values) if safe_text(k)}
    if not metrics:
        for row in page.css(".snapshot-table2 tr"):
            cells = [" ".join(td.css("::text").getall()).strip() for td in row.css("td")]
            for i in range(0, len(cells) - 1, 2):
                if cells[i]:
                    metrics[cells[i]] = cells[i + 1]
    title = get_first(page, "title::text")
    name = title.split(" Stock Price")[0].replace(f"{ticker} - ", "").strip()
    price = get_first(page, "strong[class*=quote-price] ::text")
    seen_change = []
    for raw in page.css(".quote-price_wrapper [class*=change] ::text").getall():
        value = safe_text(raw)
        if value and not value.lower().startswith(("dollar", "percentage")) and value not in seen_change:
            seen_change.append(value)
    change_parts = seen_change
    return QuoteRow(
        ticker=ticker,
        name=name,
        price=price,
        change=" ".join(change_parts),
        market_cap=metrics.get("Market Cap", ""),
        pe=metrics.get("P/E", ""),
        source_url=url,
    )


def fetch_quote(session: StealthySession, ticker: str) -> tuple[QuoteRow | None, TargetStatus]:
    ticker = ticker.upper().strip()
    url = f"https://finviz.com/quote.ashx?t={quote_plus(ticker)}"
    status = TargetStatus(ticker, "quote", url)
    try:
        page = session.fetch(
            url,
            wait_selector="table.snapshot-table2",
            wait_selector_state="visible",
            timeout=60000,
        )
        status.status = getattr(page, "status", None)
        status.final_url = getattr(page, "url", "") or ""
        status.usable = is_usable(page)
        if not status.usable:
            return None, status
        return parse_finviz_quote(page, ticker, url), status
    except Exception as exc:
        status.error = f"{type(exc).__name__}: {exc}"
        return None, status


def fetch_article(session: StealthySession, url: str, idx: int) -> tuple[Story | None, TargetStatus]:
    status = TargetStatus(f"article_{idx}", "article", url)
    try:
        page = session.fetch(
            url,
            wait_selector="h1",
            wait_selector_state="visible",
            timeout=60000,
        )
        status.status = getattr(page, "status", None)
        status.final_url = getattr(page, "url", "") or ""
        status.usable = is_usable(page)
        if not status.usable:
            return None, status
        title = get_first(page, "h1::text") or get_first(page, "title::text")
        hero = get_first(page, 'meta[property="og:image"]::attr(content)')
        body = " ".join(page.css("article p::text, main p::text, p::text").getall())
        return Story(title=title, url=status.final_url or url, hero=hero, body=body[:600]), status
    except Exception as exc:
        status.error = f"{type(exc).__name__}: {exc}"
        return None, status


def render_markdown(date_label: str, heatmap_path: str | None, quotes: list[QuoteRow], stories: list[Story], statuses: list[TargetStatus]) -> str:
    lines = [
        f"# Daily Financial Briefing — {date_label}",
        "",
        "## Markt",
    ]
    if heatmap_path:
        lines.append(f"![FINVIZ Sector Heatmap]({heatmap_path})")
    else:
        lines.append("> Geen marktvisual beschikbaar. FINVIZ/StockAnalysis fallback faalde.")
    lines += [
        "",
        "## Tickers",
        "| Ticker | Prijs | Δ | Market Cap | P/E |",
        "|---|---:|---:|---:|---:|",
    ]
    if quotes:
        for q in quotes:
            label = f"[{q.ticker}]({q.source_url})"
            lines.append(f"| {label} | {q.price or 'n/a'} | {q.change or 'n/a'} | {q.market_cap or 'n/a'} | {q.pe or 'n/a'} |")
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a |")
    lines += ["", "## Top Stories"]
    if stories:
        for s in stories:
            lines.append(f"### [{s.title}]({s.url})")
            if s.hero:
                lines.append(f"![hero]({s.hero})")
            if s.body:
                lines.append(s.body[:350].strip() + ("…" if len(s.body) > 350 else ""))
            lines.append("")
    else:
        lines.append("> Geen bruikbare artikelen opgehaald.")
    lines += ["", "## Run status", "| Target | Status | Usable | URL |", "|---|---:|---|---|"]
    for st in statuses:
        url = st.final_url or st.url
        mark = "✅" if st.usable else "❌"
        label = f"{st.kind}:{st.name}"
        lines.append(f"| {label} | {st.status if st.status is not None else 'err'} | {mark} | <{url}> |")
    lines.append("")
    return "\n".join(lines)


def build_briefing(tickers: list[str], articles: list[str], output_root: Path) -> Path:
    date_label = datetime.now().strftime("%Y-%m-%d")
    out_dir = output_root / date_label
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    statuses: list[TargetStatus] = []
    quotes: list[QuoteRow] = []
    stories: list[Story] = []
    heatmap_rel: str | None = None

    with StealthySession(**BRIEFING_DEFAULTS) as session:
        heatmap_status = fetch_heatmap(session, assets_dir)
        statuses.append(heatmap_status)
        if heatmap_status.usable:
            heatmap_rel = "./assets/finviz_heatmap.png"
        else:
            fallback_status = fetch_stockanalysis_heatmap(session, assets_dir)
            statuses.append(fallback_status)
            if fallback_status.usable:
                heatmap_rel = "./assets/stockanalysis_heatmap.png"

        for ticker in tickers:
            quote, st = fetch_quote(session, ticker)
            statuses.append(st)
            if quote:
                quotes.append(quote)

        for idx, url in enumerate(articles, start=1):
            story, st = fetch_article(session, url, idx)
            statuses.append(st)
            if story:
                stories.append(story)

    briefing = render_markdown(date_label, heatmap_rel, quotes, stories, statuses)
    briefing_path = out_dir / "briefing.md"
    manifest_path = out_dir / "manifest.json"
    briefing_path.write_text(briefing, encoding="utf-8")
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(out_dir),
        "briefing": str(briefing_path),
        "assets_dir": str(assets_dir),
        "tickers": tickers,
        "articles": articles,
        "statuses": [asdict(st) for st in statuses],
        "usable_count": sum(1 for st in statuses if st.usable),
        "total_count": len(statuses),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a daily financial briefing via native Scrapling patterns")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--articles", nargs="*", default=DEFAULT_ARTICLES)
    parser.add_argument("--output-root", default=".hermes-briefings")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = build_briefing(args.tickers, args.articles, Path(args.output_root))
    print(f"BRIEFING_DIR={out_dir}")
    print(f"BRIEFING={out_dir / 'briefing.md'}")
    print(f"MANIFEST={out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
