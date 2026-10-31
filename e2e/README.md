# End-to-end UI tests

Real browser (Playwright, Chromium) against a throwaway copy of the app: its own SQLite file seeded with demo
disputes, API on :8001, Vite on :5174. Your dev database and PayPal are never touched (the scratch API has
PayPal polling off; no test clicks "Yes, send it").

Two instances are started: one with `AUTH_MODE=off` for the screens, one with real login (`AUTH_MODE=paypal`)
for the login gate (a session token is minted with the scratch AUTH_SECRET; PayPal's login page itself is not
automated).

```bash
python -m venv .venv-e2e && .venv-e2e/bin/pip install -r e2e/requirements.txt
.venv-e2e/bin/playwright install chromium
# needs: backend/.venv (backend deps) and Node/npm with frontend/node_modules installed
.venv-e2e/bin/pytest -q e2e
```
