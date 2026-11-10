# Vigil MD: front end

The website for Vigil MD: **search** for a medical device, company or FDA product code,
open its **device page** to see what FDA adverse-event reports and recalls say about it, and
ask the **assistant** (a chat panel on every page). The assistant only reports what FDA data
says; it never gives medical advice or judges whether a device is safe.

Built to the front-end handover of 2026-11-09, against its draft API. The result is a
**static build** (`dist/`: HTML, JS, CSS). There is no Node server at runtime; the API serves
`dist/` in one container. Docker, the web server, Cloud Run, login, the API and the model are
handled separately.

## Contents

1. [Requirements](#requirements)
2. [Install and run with example data](#install-and-run-with-example-data)
3. [Build](#build)
4. [Settings](#settings)
5. [Run against a local API](#run-against-a-local-api)
6. [Checks](#checks)
7. [Pages](#pages)
8. [Example data (mock mode)](#example-data-mock-mode)
9. [API contract used](#api-contract-used)
10. [Rules the code follows](#rules-the-code-follows)
11. [Code layout](#code-layout)
12. [Notes for the API and hosting team](#notes-for-the-api-and-hosting-team)
13. [Handover checklist](#handover-checklist)

## Requirements

- **Node 22** (pinned in `.nvmrc` and `engines` in `package.json`). Check with `node --version`.
  `.npmrc` has `engine-strict=true`, so npm refuses to install on another Node version.
  On Windows: `winget install --id OpenJS.NodeJS.22 -e`.
- Nothing else: no API, database or cloud login is needed in mock mode.

## Install and run with example data

From `Web-App/frontend/`:

```bash
npm ci                    # installs the exact versions in package-lock.json
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
npm run dev               # open http://localhost:5173
```

`.env.example` has `VITE_USE_MOCKS=true`, so the site answers from the example files in
`src/mocks/` and the header shows an **Example data** badge. Saved changes appear in the
browser straight away. After changing `.env`, stop the server (Ctrl+C) and start it again.

## Build

```bash
npm ci && npm run build   # type check + static build into dist/
npm run preview           # serve dist/ at http://localhost:4173 to check it
```

Settings are read **at build time**. A build made without `VITE_USE_MOCKS=true` contains no
mock code or example data and calls the API at `VITE_API_BASE_URL` (default `/api`).

## Settings

In `.env` (copy of `.env.example`), `.env.local`, or the environment of the build:

| Variable | Default | Meaning |
|---|---|---|
| `VITE_API_BASE_URL` | `/api` | API address. The default is the same origin that serves the site. No other address is written in the code. |
| `VITE_USE_MOCKS` | not set (off) | Exactly `true` = answer from `src/mocks/`; anything else = call the API. |
| `DEV_API_PROXY` | empty | `npm run dev` only, never in the build: forward `/api` to a local API, e.g. `http://127.0.0.1:8000`. |

A request that gets no answer within **30 seconds** is abandoned (see `src/config.ts`).

## Run against a local API

When the API exists, run it locally (e.g. on port 8000), then in `.env`:

```
VITE_USE_MOCKS=false
VITE_API_BASE_URL=/api
DEV_API_PROXY=http://127.0.0.1:8000
```

and restart `npm run dev`. The browser keeps talking to `localhost:5173/api/...`, and the
dev server forwards those requests to the API, so no CORS setup is needed.

## Checks

```bash
npm run lint        # ESLint
npm run typecheck   # TypeScript
npm test            # Vitest (jsdom), 62 tests
npm run build       # must also pass
```

ESLint also blocks, as errors: `console` calls (questions can contain health details),
`localStorage` / `sessionStorage`, and `dangerouslySetInnerHTML` (no raw HTML from responses).

## Pages

| Page | Address | What it shows |
|---|---|---|
| Search | `/` (`/?q=…`) | A search box. Searches 300 ms after typing stops, from 2 characters. Results grouped as Devices / Companies / Product codes, each with its title, company and report count, in the API's order. A result opens its device page. No results: *"Nothing found. Try the brand name, the company or the 3-letter product code."* |
| Device page | `/device/<kind>/<entity_id>` | Name, company, product codes; total reports with the API's `counts_note` beside them; reports by event type; reports per year (bars, plus a table by event type); top device and patient problems and the "no patient harm reported" count; recalls in the API's order (newest first) with class, date, status, reason and more details; sources and data date. `found: false`: a note that there is no data. Unknown entry: "Page not found". Failure: the service message and **Try again**. |
| Assistant | panel on every page | Opened from the header ("Ask the assistant") or a device page's button. On a device page it sends that device as `entity` and says so; elsewhere `entity: null`. Conversation kept on screen for the session only. Enter sends, Shift+Enter is a new line, Esc closes. Side panel on wide screens, full screen at 720 px and below. |

Assistant replies, by `status` (handover §5):

| status | Shown |
|---|---|
| `answered` | All sections, in the order received (a medical refusal, if present, comes first) |
| `refused` | The refusal text only |
| `help`, `scope`, `unclear`, `budget` | The text |
| `need_device` | The text; the cursor goes back into the question box |
| `not_found` | The text and a link to the search page |
| `ambiguous` | The whole text, and one button per `search.matches` entry; a click sends the same question with `{entity_type: match.type, entity_id: match.id}` |
| `error` | The text and a **Try again** button (same question, same entity) |
| no answer in 30 s / network failure | *"Can't reach the service, please try again."* and **Try again** |

While an answer is awaited the reply shows *"Looking this up…"* and a second question can't be
sent.

## Example data (mock mode)

`src/mocks/` holds placeholder responses in the contract's shapes. The devices, companies and
numbers are **fictional** (AcmeAir CPAP, Example Medical Inc., …); the fixed texts (refusal,
help, disclaimer, UDI note) are the chatbot's own. **Replace them with the real example
responses from the development dataset when they arrive**: drop the files in, keeping the
folder layout below. Entity files are matched on `entity.type` + `entity.id` inside each
file, so file names don't matter.

```
src/mocks/
├── search.json          GET /api/search: all hits; mock search filters them by the words typed
├── entities/*.json      GET /api/entity/<kind>/<id>
├── answers/*.json       POST /api/answer, one per case below
└── mockApi.ts           answers from these files, with small delays to show loading states
```

The example set covers long data (very long names and recall reasons, 600-character report
excerpts, one containing `<script>`) and missing data (null fields, 0 reports, no recalls, an
entry with `found: false`).

**Questions to try** (answers are picked by keywords, first match wins):

| Ask | Reply |
|---|---|
| Was the AcmeAir CPAP recalled, and why? | `answered` |
| Any deaths for the AcmeAir CPAP? Should I stop using it? | `answered`, refusal first |
| Should I stop using it? | `refused` |
| What can you do? | `help` |
| Write me a poem | `scope` |
| asdf | `unclear` |
| How many deaths were reported? | `need_device` |
| Recalls for xyz | `not_found` |
| How many reports for acme pumps? | `ambiguous` (choice buttons) |
| What do reports say about the GlucoSense insulin system? | `answered`, long, with `<script>` in an excerpt |
| Was the Acme Infusion Pump recalled? | `answered`, no reports |
| AcmeAir vs Acme Infusion vs nebulizer vs ZZZ | `answered`, comparison with a `note` |
| mock budget / mock error | `budget` / `error` |
| mock offline | network failure |

On the AcmeAir CPAP, Acme GlucoSense and Acme Infusion Pump pages, questions get that device's
answer; on the other pages they get the AcmeAir answer (the mocks have no answers for them).
**Search** for `acme`, `example` or `zzz` to see every kind of result and entry.

## API contract used

From the handover (section 2). Field names are fixed; types are in `src/api/types.ts`; every
call goes through `src/api/client.ts`.

| Call | Request | Used for |
|---|---|---|
| `GET {base}/search?q=<text>&limit=10` | | Search page |
| `GET {base}/entity/<kind>/<entity_id>` | `kind`: `device`, `manufacturer`, `product_code`; both parts URL-encoded (`dev:…` → `dev%3A…`) | Device page |
| `POST {base}/answer` | `{"question": "...", "entity": null}` or `{"question": "...", "entity": {"entity_type": "...", "entity_id": "..."}}` | Assistant |

Errors: HTTP 404 → "Page not found" on a device page. Network failure, a 5xx, an unreadable
response or no response in 30 s → *"Can't reach the service, please try again."*

## Rules the code follows

From handover sections 4 and 7:

- **The bot's text is shown as given:** sections in the order received, never reworded,
  shortened or reordered. `SafeText` turns only `**bold**`, `*italic*` and `- ` list lines
  into formatting; everything else, including `<script>`, HTML, links and headings in report
  excerpts, shows as plain text. No raw HTML is ever inserted (and ESLint forbids it).
- **The medical refusal** is shown first and unchanged, as a neutral note, never as an error.
- **The footer** (disclaimer, UDI note, sources with the data date) is always shown.
- **No judgement in the UI:** no safe/unsafe labels, ratings, scores or rankings; search
  cards show only kind, title, company and report count; one neutral accent colour, no red or
  green. Device-page numbers are always called **reports**, with `counts_note` beside them.
- **Privacy:** no analytics, trackers or third-party scripts; questions are kept in memory
  only (not in `localStorage`, cookies or the address) and never logged; the browser talks
  only to our API.
- **Accessibility:** labelled inputs and buttons, keyboard use (skip link, focus outlines, Esc
  closes the assistant, focus moves into and out of it), live regions for loading and
  results, the year chart also as a table, layout down to 360 px wide, light and dark mode.

## Code layout

```
Web-App/frontend/
├── .env.example  .nvmrc  .npmrc  .gitignore  package.json  package-lock.json
├── index.html  vite.config.ts  tsconfig.json  eslint.config.js
└── src/
    ├── main.tsx, App.tsx       entry point; routes: /, /device/:kind/:entityId
    ├── config.ts               build-time settings (API base URL, mocks, 30 s timeout)
    ├── routes.ts               page addresses
    ├── styles.css              all styles (colour tokens for light and dark mode)
    ├── api/
    │   ├── types.ts            the contract's shapes
    │   ├── client.ts           the only code that calls the API (timeout, errors, mock switch)
    │   └── errors.ts           "Can't reach the service…", not found, cancelled
    ├── chat/                   the assistant conversation (in memory) and the device-page entity
    ├── hooks/                  useSearch, useEntity, useDebouncedValue
    ├── lib/                    formatting, safe-text parsing, status rules, data date
    ├── components/
    │   ├── SafeText.tsx        safe rendering of the bot's text
    │   ├── SearchResults.tsx
    │   ├── Layout.tsx          header (assistant toggle), page, panel, footer
    │   ├── chat/               ChatPanel, BotReply (status table), ChatToggle
    │   └── device/             ReportCounts, YearChart, ProblemsList, RecallsList, DataSources
    ├── pages/                  SearchPage, DevicePage
    ├── mocks/                  example data and the mock API
    └── test/setup.ts           test setup (jsdom matchers)
```

Tests sit next to the code they test (`*.test.ts(x)`).

## Notes for the API and hosting team

1. **Page addresses:** the site uses normal paths (`/device/device/dev%3A…`). When such a page
   is opened or reloaded directly, the server must answer with `index.html` (a single-page app
   fallback) for any path that isn't `/api/...` or a file in `dist/`. Assets are referenced from
   the site root (`/assets/...`), so the site must be served at `/`.
2. **Unknown entity:** the device page treats HTTP 404 as "Page not found" and
   `found: false` as "no data for this entry". Please confirm which one the API returns for an
   id it doesn't know.
3. **Kinds:** the device page calls `/api/entity/` for all three kinds (`device`,
   `manufacturer`, `product_code`), because search results of every kind open a page.
4. **Null fields:** the front end accepts `null` for `subtitle`, `reports`, `deaths`,
   `injuries`, `product_codes`, `counts_note`, `no_harm_reports` and every recall field.
5. **Site footer wording:** "Vigil MD shows what FDA adverse-event reports and recalls say. It
   does not give medical advice." Please confirm or replace it.

## Handover checklist

| Item | Where |
|---|---|
| `npm ci && npm run build` works on a clean copy and outputs `dist/` | [Build](#build) |
| API address only via `VITE_API_BASE_URL`; mocks via `VITE_USE_MOCKS`; `.env.example` | [Settings](#settings) |
| Search, device page and chat work fully in mock mode | [Example data](#example-data-mock-mode) |
| All statuses in section 5 handled; ambiguous choices clickable | [Pages](#pages), `ChatPanel.test.tsx` |
| Footer, disclaimer, UDI note and sources always shown; no judgement wording, ratings or danger colours | [Rules](#rules-the-code-follows) |
| Safe Markdown rendering (test included); no raw HTML from responses | `SafeText.test.tsx`, ESLint rule |
| No secrets, no analytics, no storage of questions | [Rules](#rules-the-code-follows), ESLint rules |
| Works at phone width and by keyboard | [Rules](#rules-the-code-follows) |
| README; lint and tests pass | this file, [Checks](#checks) |
