# DisputeShield

An AI agent that investigates PayPal disputes for small merchants, recommends whether to fight, settle or gather more
evidence, drafts the response, and sends it through the PayPal Disputes API only after the merchant approves.
Built for the PayPal AI Hackathon (2026). The public explainer is the app's `/demo` page (same text:
[docs/DEVPOST.md](docs/DEVPOST.md)). Licence: [MIT](LICENSE).

## What it does

- **Disputes arrive live:** PayPal webhooks (plus a poller as backup) put each new dispute in the queue within
  seconds, with the response deadline counting down.
- **Agents investigate and recommend:**
  - The Investigator gathers PayPal and store evidence.
  - Fixed rules score it, and a money calculator compares fight / full refund / partial offer.
  - An LLM (Groq, with Gemini as fallback) explains the decision, citing only evidence that exists.
  - Every step streams onto a live timeline.
- **The merchant acts:**
  - add missing evidence (uploads read by Gemini; you confirm every field)
  - invite the buyer to a guided troubleshooting page
  - edit an offer
  - approve, after which the response is sent to PayPal (evidence, an offer, a refund or an escalation). Nothing is
    sent without approval.
- **Around it:**
  - Analytics (AG Grid + AG Charts, "Ask your disputes")
  - a Deadlines view (Bryntum Scheduler)
  - a Trust page (evals + guardrails)
  - email / Slack alerts
  - an MCP server, so you can use it from Claude Desktop (below)

## Run it yourself

**You need:** Python 3.11, Node.js 20+, a free [PayPal developer account](https://developer.paypal.com/) (one
sandbox REST app, plus its default sandbox business account and a **US** sandbox personal account), and a free
[Groq](https://console.groq.com/) and/or [Gemini](https://aistudio.google.com/) API key.

```bash
git clone <this repo> && cd <repo>

# backend
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in at least the values listed below
.venv/bin/python -m app.scripts.seed                   # demo store orders (no fake PayPal data)
.venv/bin/python -m app.scripts.seed --demo-history    # optional: labelled demo outcomes for Analytics
.venv/bin/uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend && npm install && npm run dev               # http://localhost:5173
```

Minimum `backend/.env` for a local run:

| Setting | Value |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | your sandbox app's credentials (the app is sandbox-only by design) |
| `GROQ_API_KEY` and/or `GEMINI_API_KEY` | at least one LLM key |
| `AUTH_MODE` | `off` for local use without login (refused when deployed), or `paypal` + `AUTH_SECRET`, `MERCHANT_LOGIN_EMAILS` |

Everything else has a working default and is explained in [backend/.env.example](backend/.env.example).
`./dev.sh` starts everything at once, including a public tunnel so PayPal webhooks reach your machine. Without a
webhook the poller still picks new disputes up within about a minute.

**Try a dispute end to end:**
1. In the app's **Store**, buy an item with the US sandbox personal account.
2. Log in to www.sandbox.paypal.com as that buyer: Activity → the payment → **Report a problem**.
3. The case appears under **Disputes**: watch the timeline, open **Options**, and approve or reject.

[docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md) lists every scenario, including **demo mode**: one-click
scenarios (set `DEMO_MODE=true` and `ADMIN_TOKEN`).

**Tests:**
- `cd backend && .venv/bin/python -m pytest -q` (offline, no API calls)
- `cd frontend && npm test`
- the Playwright UI suite in [e2e/README.md](e2e/README.md)
- the LLM evals (real models, uses quota): `make evals`

Deploying: [DEPLOY.md](DEPLOY.md) (Render). Developer notes: [CLAUDE.md](CLAUDE.md). Progress and open items:
[PROGRESS.md](PROGRESS.md).

**Third-party licences:** the Deadlines view uses Bryntum Scheduler 8 Community edition (MIT, from public npm).
Everything else in this repository is MIT-licensed, and the dependencies keep their own licences.

## Use DisputeShield from Claude Desktop

DisputeShield is also an MCP server (streamable HTTP at `<backend>/mcp`, official Python MCP SDK). It has 5 tools:

| Tool | What it does |
|---|---|
| `list_open_disputes` | open disputes with the agents' latest recommendation |
| `get_case(dispute_id)` | facts, checklist score, evidence items, the current decision |
| `explain_recommendation(dispute_id)` | reasoning, money options, cited evidence and the **confirmation phrase** (without its code) |
| `approve_decision(decision_id, confirm, note?)` | approve and send to PayPal: `confirm` = the phrase + the code from your dashboard |
| `reject_decision(decision_id, reason)` | reject (nothing is sent) |

Rule 5 still holds: `approve_decision` runs the same approval code as the dashboard's Approve button. That means an
atomic PROPOSED → APPROVED, PayPal re-checked first, and APPROVED checked again before any money-moving call. The
`confirm` argument must be exactly `APPROVE <ACTION> <dispute id> <amount> <currency> <CODE>`. The phrase comes from
`explain_recommendation`; the 6-character CODE is shown **only on the case page of your dashboard** and is never
returned by any tool. So an assistant can't approve on its own (even if text in a case tried to talk it into it), or
approve something other than what you saw. Five wrong confirmations lock MCP approval of that decision.

1. In DisputeShield go to **Settings → Use from Claude (MCP)** and press **Create token**. The token is shown once;
   only its hash is stored. A new token replaces the old one, and **Revoke** turns MCP access off.
2. Claude Desktop connects to remote servers through [`mcp-remote`](https://github.com/geelen/mcp-remote) (needs
   Node.js). Add this to `claude_desktop_config.json` (macOS:
   `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
   `%APPDATA%\Claude\claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "disputeshield": {
         "command": "npx",
         "args": ["-y", "mcp-remote@0.15.1", "https://YOUR-BACKEND/mcp", "--transport", "http-only",
                  "--header", "Authorization:${AUTH_HEADER}"],
         "env": { "AUTH_HEADER": "Bearer dsm_YOUR_TOKEN" }
       }
     }
   }
   ```

   (Locally: `http://localhost:8000/mcp`. No spaces around `:` in the header argument; the space in `Bearer …`
   lives in the env var, as mcp-remote's README recommends for Windows.)
3. Restart Claude Desktop and ask, for example: *"Which disputes are due this week, and what do the agents
   recommend for the lamp case?"* Claude shows the recommendation. Nothing is approved until you give Claude the
   confirmation code from that case's page in DisputeShield.

Your case data (amounts, evidence summaries, buyer emails in summaries) goes to the assistant you connect, so use
an assistant you trust with your business data.
