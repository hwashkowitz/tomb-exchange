# Tomb Exchange — repo architecture

## Layout

```
tomb-exchange/
├── index.html                      # the site (static, no build step)
├── prices.json                     # data + config. single source of truth.
├── .github/
│   └── workflows/
│       └── fetch-prices.yml        # scheduled job (next session)
├── scripts/
│   └── fetch_prices.py             # the fetcher (next session)
└── ARCHITECTURE.md
```

Everything is static. GitHub Pages serves the repo root; `index.html` fetches
`prices.json` from the same origin, so there is no CORS problem and no API key
in the browser.

## Data flow

```
GitHub Actions (cron, daily)
   └─> scripts/fetch_prices.py
         ├─ reads prices.json for the per-item config
         ├─ calls eBay Browse API (key from repo secrets)
         ├─ trims outliers, takes median, applies conditionFactor
         └─ appends one history entry per item
   └─> commits prices.json back to the repo
         └─> GitHub Pages redeploys automatically
               └─> index.html fetches the updated file
```

Git history *is* the price history archive. Every commit is a dated snapshot,
so the data is recoverable even if `prices.json` is ever corrupted.

## prices.json schema

Top level:

| field     | meaning |
|-----------|---------|
| `schema`  | version integer, bump on breaking changes |
| `updated` | ISO timestamp of the last successful fetch |
| `source`  | `seed` or `ebay-browse` — tells you if numbers are real yet |
| `items[]` | the catalogue |

Per item — **config fields** (hand-maintained, read by the fetcher):

| field             | purpose |
|-------------------|---------|
| `id`              | stable key, never changes |
| `symbol`          | ticker shown in the UI |
| `query`           | the eBay search string for this unit |
| `excludeTerms[]`  | kill-words that filter out parts/accessory listings |
| `conditionFactor` | multiplier mapping the tomb unit's real condition onto the market median |

Per item — **data fields** (machine-written, append-only):

```json
"history": [
  { "date": "2026-08-17", "price": 110.00, "samples": 24, "source": "ebay-browse" }
]
```

One entry per fetch. Never rewritten, only appended.

## Rules the fetcher must follow

These come from what the catalogue actually looks like:

1. **Median, never mean.** The first-gen iPod and first-gen iPhone attract
   novelty listings in the thousands of dollars. One of those destroys an average.
2. **Trim before taking the median.** Drop the top and bottom 15% of results.
3. **Exclude parts listings.** Searching `A1502` returns batteries, logic boards,
   and screen assemblies. That's what `excludeTerms` is for.
4. **Carry forward on empty.** Thin markets (MOTO, FUZE) will sometimes return
   zero results. Repeat the previous price rather than writing a zero — a zero
   would show as a -100% crash.
5. **Apply `conditionFactor` last**, after the median is computed.
6. **Never write a null or 0 price.** The site treats those as "no reading."

## What the site does with the numbers

- **Last** — most recent valid price.
- **Chg / Chg %** — compared against the immediately preceding reading. Shows `—`
  when there is no prior reading, rather than a fake `0.00%`.
- **Session Change** — sums only the items that have *both* a current and a prior
  reading. A newly added item can't fake a portfolio gain.
- **Low / High** — all-time across every stored reading.
- **Pts** — how many readings exist. Useful for spotting a stalled fetch.

## Current state

`prices.json` holds one seed reading per item, researched by hand on 2026-08-17
from active listings. Change columns are intentionally blank until the second
fetch runs. Seed total: **$834.00** across 13 items.
