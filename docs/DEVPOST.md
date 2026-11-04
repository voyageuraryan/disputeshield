# DisputeShield

_PayPal AI Hackathon submission. Same text as the public /demo page (frontend/src/demo/content.ts)._

## What it does

An AI agent that investigates PayPal disputes for small merchants, recommends whether to fight, settle or gather more evidence, drafts the response, and sends it through the PayPal Disputes API only after the merchant approves.

## The 90-second version

- **0:00**: A buyer opens a dispute in PayPal. PayPal's webhook reaches DisputeShield and the case appears in the queue within seconds, with the response deadline counting down.
- **0:15**: The Investigator gathers evidence from PayPal (the dispute, the capture and seller protection, tracking) and from the store (order, shipment, buyer messages, listing, purchase history, hashed checkout signals).
- **0:30**: Fixed rules decide which evidence this dispute reason needs and score the case; a money calculator compares fighting, a full refund and a partial offer.
- **0:45**: The Analyst (an LLM) explains the decision in plain English. It may only cite evidence that exists and may move one step from the rules, only with a cited reason.
- **1:00**: The merchant sees the options in money, can add missing evidence or invite the buyer to guided troubleshooting, and approves.
- **1:15**: Only then is the response sent to PayPal: evidence, an offer, a refund or an escalation. The outcome feeds Analytics and recalibrates the win estimates.

## How PayPal is used

- **Disputes API (v1)**: list and read disputes, provide evidence and supporting info, accept claims, make offers, escalate, send messages.
- **Webhooks**: CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED with signature verification; a poller as backup.
- **Orders v2 + Payments v2**: the demo store's checkout (JS SDK v6), captures and seller-protection status as evidence.
- **Shipment tracking**: trackers on orders, read back as PayPal-side tracking evidence.
- **Log in with PayPal**: merchant sign-in to the dashboard.

## How AI is used

- **Analyst**: Groq (primary) with Gemini fallback, strict JSON-schema output, retried once then a person decides.
- **Seller notes**: factual notes that cite only case evidence; checked for over-claims and leaked personal data.
- **Buyer troubleshooting**: a guided, multilingual help page for faulty electronics, with grounded web research and a hard safety stop.
- **Document reading**: Gemini reads uploaded proof (labels, receipts) into fields the merchant confirms.
- **Ask your disputes**: questions become a whitelisted structured query, never SQL.
- **Evaluation**: DeepEval goldens, judged metrics and simulated buyers measure accuracy, faithfulness and safety.

## Architecture

PayPal (sandbox) webhooks and APIs feed a FastAPI backend. A LangGraph pipeline runs the Investigator, the deterministic rules and money calculator, the Analyst (Groq, Gemini fallback) and Resolution; every step is written to SQLite and streamed to the React dashboard (AG Grid, AG Charts). The approval gate (hard rule 5) sits in front of every PayPal call that moves money. Diagram: the Architecture section of the /demo page.

## Guardrails

- No money moves without the merchant's approval, checked twice in code.
- The AI cites only evidence that exists; rules decide what evidence a reason needs.
- Personal data is redacted before any AI call; checkout signals are stored only as hashes.
- PayPal sandbox only; every agent step is on a live timeline.
