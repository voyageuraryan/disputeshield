# DisputeShield

An AI agent that investigates PayPal disputes for small merchants, recommends whether to fight, settle or gather more
evidence, drafts the response, and sends it through the PayPal Disputes API only after the merchant approves.
PayPal AI Hackathon project. The public explainer is the app's `/demo` page (same text: [docs/DEVPOST.md](docs/DEVPOST.md)).

- Run everything locally: `./dev.sh` (tunnel + backend + frontend). Developer notes: [CLAUDE.md](CLAUDE.md).
- Deploy: [DEPLOY.md](DEPLOY.md). Hand tests: [docs/TEST_SCENARIOS.md](docs/TEST_SCENARIOS.md). Progress and open
  items: [PROGRESS.md](PROGRESS.md).

## Use DisputeShield from Claude Desktop

DisputeShield is also an MCP server (streamable HTTP at `<backend>/mcp`, official Python MCP SDK). It has 5 tools:

| Tool | What it does |
|---|---|
| `list_open_disputes` | open disputes with the agents' latest recommendation |
| `get_case(dispute_id)` | facts, checklist score, evidence items, the current decision |
| `explain_recommendation(dispute_id)` | reasoning, money options, cited evidence and the **confirmation phrase** |
| `approve_decision(decision_id, confirm, note?)` | approve and send to PayPal: `confirm` must echo the phrase exactly |
| `reject_decision(decision_id, reason)` | reject (nothing is sent) |

Rule 5 still holds: `approve_decision` runs the same approval code as the dashboard's Approve button. That means an
atomic PROPOSED → APPROVED, PayPal re-checked first, and APPROVED checked again before any money-moving call. The
`confirm` argument must be exactly `APPROVE <ACTION> <dispute id> <amount> <currency>` (from
`explain_recommendation`), so an assistant can't approve blindly or approve something other than what you saw.

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
   recommend for the lamp case?"* Claude shows the recommendation and the confirmation phrase. Nothing is approved
   until you confirm that phrase.

Your case data (amounts, evidence summaries, buyer emails in summaries) goes to the assistant you connect, so use
an assistant you trust with your business data.
