// Phase 16: the public /demo landing page content. docs/DEVPOST.md is the same text for the Devpost write-up;
// backend/tests/test_devpost_sync.py fails if their section headings or key sentences drift apart.

export const TAGLINE = "An AI agent that investigates PayPal disputes for small merchants, recommends whether to fight, settle or gather more evidence, drafts the response, and sends it through the PayPal Disputes API only after the merchant approves.";

export const EXPLAINER: { t: string; text: string }[] = [
  { t: "0:00", text: "A buyer opens a dispute in PayPal. PayPal's webhook reaches DisputeShield and the case appears in the queue within seconds, with the response deadline counting down." },
  { t: "0:15", text: "The Investigator gathers evidence from PayPal (the dispute, the capture and seller protection, tracking) and from the store (order, shipment, buyer messages, listing, purchase history, hashed checkout signals)." },
  { t: "0:30", text: "Fixed rules decide which evidence this dispute reason needs and score the case; a money calculator compares fighting, a full refund and a partial offer." },
  { t: "0:45", text: "The Analyst (an LLM) explains the decision in plain English. It may only cite evidence that exists and may move one step from the rules, only with a cited reason." },
  { t: "1:00", text: "The merchant sees the options in money, can add missing evidence or invite the buyer to guided troubleshooting, and approves." },
  { t: "1:15", text: "Only then is the response sent to PayPal: evidence, an offer, a refund or an escalation. The outcome feeds Analytics and recalibrates the win estimates." },
];

export const PAYPAL_USES: { name: string; text: string }[] = [
  { name: "Disputes API (v1)", text: "list and read disputes, provide evidence and supporting info, accept claims, make offers, escalate, send messages." },
  { name: "Webhooks", text: "CUSTOMER.DISPUTE.CREATED / UPDATED / RESOLVED with signature verification; a poller as backup." },
  { name: "Orders v2 + Payments v2", text: "the demo store's checkout (JS SDK v6), captures and seller-protection status as evidence." },
  { name: "Shipment tracking", text: "trackers on orders, read back as PayPal-side tracking evidence." },
  { name: "Log in with PayPal", text: "merchant sign-in to the dashboard." },
];

export const AI_USES: { name: string; text: string }[] = [
  { name: "Analyst", text: "Groq (primary) with Gemini fallback, strict JSON-schema output, retried once then a person decides." },
  { name: "Seller notes", text: "factual notes that cite only case evidence; checked for over-claims and leaked personal data." },
  { name: "Buyer troubleshooting", text: "a guided, multilingual help page for faulty electronics, with grounded web research and a hard safety stop." },
  { name: "Document reading", text: "Gemini reads uploaded proof (labels, receipts) into fields the merchant confirms." },
  { name: "Ask your disputes", text: "questions become a whitelisted structured query, never SQL." },
  { name: "Evaluation", text: "DeepEval goldens, judged metrics and simulated buyers measure accuracy, faithfulness and safety." },
];

export const GUARDRAILS_SHORT = [
  "No money moves without the merchant's approval, checked twice in code.",
  "The AI cites only evidence that exists; rules decide what evidence a reason needs.",
  "Personal data is redacted before any AI call; checkout signals are stored only as hashes.",
  "PayPal sandbox only; every agent step is on a live timeline.",
];

export const SECTIONS = ["What it does", "The 90-second version", "How PayPal is used", "How AI is used", "Architecture", "Guardrails"];
