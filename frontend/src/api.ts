import { getToken, goToLogin } from "./auth";
export type Product = {
  sku: string;
  name: string;
  price: string;
  currency: string;
  category: string; // electronics / accessory / digital / home
  paypal_category: string; // PHYSICAL_GOODS / DIGITAL_GOODS
  model: string | null;
  listing: string;
  image: string;
};

export type StoreConfig = { client_id: string; currency: string; sdk_url: string };

// Empty in dev (Vite proxies /api); on Render, the backend's public URL (set at build time).
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/** An HTTP error from the API. `detail` keeps the server's structured detail (e.g. {reason, email_available}). */
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail: unknown,
  ) {
    super(message);
  }
}

function errorText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  const d = detail as { reason?: string; error?: string } | null;
  return d?.reason ?? d?.error ?? (d ? JSON.stringify(d) : "request failed");
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    // fetch only throws on network failure ("Failed to fetch"): say it in plain words
    throw new Error("Can't reach the DisputeShield server. Check your connection and try again.");
  }
  const body = await res.json().catch(() => null);
  if (res.status === 401 && !path.startsWith("/api/store") && !path.startsWith("/api/auth/config")) {
    goToLogin("Your session ended. Please log in again.");
    throw new Error("401: login required");
  }
  if (!res.ok) {
    const detail = body?.detail;
    const debug = detail?.debug_id ? ` (PayPal debug id ${detail.debug_id})` : "";
    throw new ApiError(`${res.status}: ${errorText(detail)}${debug}`, res.status, detail);
  }
  return body as T;
}

export const api = {
  storeConfig: () => req<StoreConfig>("/api/store/config"),
  products: () => req<Product[]>("/api/store/products"),
  createOrder: (sku: string) =>
    req<{ id: string; order_id: number }>("/api/store/orders", { method: "POST", body: JSON.stringify({ sku }) }),
  captureOrder: (paypalOrderId: string) =>
    req<{ order_id: number; paypal_order_id: string; capture_id: string; status: string }>(
      `/api/store/orders/${encodeURIComponent(paypalOrderId)}/capture`,
      { method: "POST" },
    ),
};

// ---- disputes & decisions ----------------------------------------------------------------

export type DecisionBrief = { id: number; recommendation: string; confidence: string; status: string };

export type DisputeRow = {
  id: number;
  paypal_dispute_id: string;
  reason: string;
  stage: string | null;
  status: string;
  amount: string;
  currency: string;
  seller_response_due: string | null;
  agent_status: string | null;
  order_id: number | null;
  buyer_email: string | null;
  updated_at: string | null;
  latest_decision: DecisionBrief | null;
};

export type Evidence = { id: number; type: string; source: string; source_ref: string; summary: string; flags: string[] };

export type CaseFile = {
  dispute_id: string;
  reason: string;
  amount: string;
  currency: string;
  due_date: string | null;
  order_id: number | null;
  evidence: Evidence[];
  status: string | null;
  stage: string | null;
  dispute_state: string | null;
  allowed_actions: string[];
  can_fight: boolean;
  can_settle: boolean;
};

export type Decision = {
  id: number;
  dispute_id: number;
  recommendation: "FIGHT" | "SETTLE" | "GATHER_MORE";
  confidence: string;
  reasoning: string;
  cited_evidence_ids: number[];
  draft_note: string | null;
  final_note: string | null;
  approved_action: string | null;
  status: "PROPOSED" | "APPROVED" | "REJECTED" | "EXECUTED" | "FAILED" | "SUPERSEDED";
  approved_at: string | null;
  executed_at: string | null;
  action_request: { endpoint: string; [k: string]: unknown } | null;
  paypal_response: { error?: boolean; status_code?: number; debug_id?: string; issues?: string[]; message?: string } | null;
  retry_of: number | null;
  economic_note: string | null;
  missing_items: string[] | null;
  analysed_threshold: string | null;
  can_retry: boolean;
};

export type Step = { id: number; agent: string; action: string; detail: Record<string, unknown> | null; created_at: string };

export type Shipment = {
  id: number;
  carrier: string | null;
  tracking_number: string | null;
  status: string | null;
  delivered_at: string | null;
  delivered_address: Record<string, string> | null;
  delivery_recorded_by?: string | null;
};

export type Order = {
  id: number;
  created_at: string;
  buyer_email: string | null;
  amount: string;
  currency: string;
  // category + model are stored at order time (orders from before Phase 9A carry PayPal's category instead)
  items: { sku?: string; name?: string; quantity?: number; unit_amount?: string; category?: string; model?: string | null }[] | null;
  shipping_address: Record<string, string> | null;
  product_listing: string | null;
  paypal_order_id: string | null;
  capture_id: string | null;
  invoice_id: string | null;
  digital_only?: boolean;
  shipments: Shipment[];
};

export type Checklist = {
  reason_label: string | null;
  supported: boolean;
  rule_score: number;
  items: { type: string; required: boolean; present: boolean; evidence_ids: number[] }[];
  red_flags: { code: string; description: string }[];
  baseline: string | null;
  baseline_why: string | null;
};

export type EvidenceSource = { evidence_id: number; source: string; source_ref: string; title: string; origin: string; record: unknown };

export type DisputeDetail = {
  settle_threshold: string;
  dispute: DisputeRow;
  order: Order | null;
  case: CaseFile;
  checklist: Checklist;
  decisions: Decision[];
  timeline: Step[];
};

export type ApproveBody = { action?: "FIGHT" | "SETTLE"; note?: string; approved_by?: string };

export const disputesApi = {
  list: () => req<DisputeRow[]>("/api/disputes"),
  detail: (id: string) => req<DisputeDetail>(`/api/disputes/${encodeURIComponent(id)}`),
  approve: (id: number, body: ApproveBody) =>
    req<Decision>(`/api/decisions/${id}/approve`, { method: "POST", body: JSON.stringify(body) }),
  reject: (id: number, reason?: string) =>
    req<Decision>(`/api/decisions/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) }),
  retry: (id: number, body: ApproveBody) =>
    req<Decision>(`/api/decisions/${id}/retry`, { method: "POST", body: JSON.stringify(body) }),
  pdfUrl: (id: number) => `${API_BASE}/api/decisions/${id}/evidence.pdf?token=${encodeURIComponent(getToken() ?? "")}`,
  source: (evidenceId: number) => req<EvidenceSource>(`/api/evidence/${evidenceId}/source`),
  reanalyze: (id: string) =>
    req<{ queued: boolean; run_key: string }>(`/api/disputes/${encodeURIComponent(id)}/reanalyze`, { method: "POST" }),
};

// ---- settings & live events --------------------------------------------------------------

export type MerchantSettings = {
  settle_threshold: string;
  refund_policy: string | null;
  auto_invite_troubleshooting: boolean;
  currency: string;
  updated_at: string;
};

export const settingsApi = {
  get: () => req<MerchantSettings>("/api/settings"),
  save: (patch: { settle_threshold?: string; refund_policy?: string; auto_invite_troubleshooting?: boolean }) =>
    req<MerchantSettings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
};

// EventSource can't send headers, so the session token goes in the query string.
export const eventsUrl = (disputeId: string) =>
  `${API_BASE}/api/disputes/${encodeURIComponent(disputeId)}/events?token=${encodeURIComponent(getToken() ?? "")}`;

export const newDisputesUrl = () => `${API_BASE}/api/events/disputes?token=${encodeURIComponent(getToken() ?? "")}`;

export const authApi = {
  config: () => req<{ mode: "paypal" | "off" }>("/api/auth/config"),
  me: () => req<{ email: string | null; name: string | null; expires_at: number | null }>("/api/auth/me"),
  startUrl: (next: string) => `${API_BASE}/api/auth/paypal/start?next=${encodeURIComponent(next)}`,
};

// ---- store back-office: orders, tracking, delivery (merchant only) ---------------------------

export type StoreOrder = Order & { digital_only: boolean; disputes: string[] };
export type DeliveryAddress = { address_line_1: string; admin_area_2?: string; admin_area_1?: string; postal_code: string; country_code: string };

export const ordersApi = {
  list: () => req<StoreOrder[]>("/api/store/orders"),
  addTracking: (orderId: number, carrier: string, trackingNumber: string) =>
    req<{ id: number }>(`/api/store/orders/${orderId}/shipments`, {
      method: "POST",
      body: JSON.stringify({ carrier, tracking_number: trackingNumber }),
    }),
  recordDelivery: (orderId: number, shipmentId: number, deliveredOn: string, address?: DeliveryAddress) =>
    req<Shipment>(`/api/store/orders/${orderId}/shipments/${shipmentId}/delivery`, {
      method: "POST",
      body: JSON.stringify({ delivered_on: deliveredOn, address: address ?? null }),
    }),
};

// ---- buyer troubleshooting (phase 9A) -----------------------------------------------------------

export type TSSessionBrief = {
  language?: string | null;
  symptom?: string | null;
  plan_generic?: boolean | null;
  ended_reason?: string | null;
  id: number;
  status: string;
  product_name: string;
  product_model: string | null;
  expires_at: string;
  invited_at: string | null;
  invited_via: "paypal_message" | "email" | null;
  buyer_last_seen_at: string | null;
};

export type TroubleshootingStatus = {
  eligible: boolean;
  why: string;
  product_name: string | null;
  product_model: string | null;
  can_message: boolean;
  buyer_email_known: boolean;
  session: TSSessionBrief | null;
  steps?: TSStepView[];
  sources?: TSSourceView[];
  messages?: TSMessageView[];
  proofs?: { id: number; step_id: number | null; mime: string; size: number; created_at: string }[];
};

export type InviteResult = {
  sent: boolean;
  already_invited: boolean;
  via: string | null;
  session: TSSessionBrief;
  link: string | null; // email stub only
};

export const troubleshootingApi = {
  status: (disputeId: string) => req<TroubleshootingStatus>(`/api/disputes/${encodeURIComponent(disputeId)}/troubleshooting`),
  invite: (disputeId: string, via: "paypal_message" | "email", resend = false) =>
    req<InviteResult>(`/api/disputes/${encodeURIComponent(disputeId)}/troubleshooting/invite`, {
      method: "POST",
      body: JSON.stringify({ via, resend }),
    }),
  // <img>/<video>/<a> can't send headers, so the merchant session goes in the query string (like the PDF link).
  proofUrl: (disputeId: string, proofId: number) =>
    `${API_BASE}/api/disputes/${encodeURIComponent(disputeId)}/troubleshooting/proofs/${proofId}?token=${encodeURIComponent(getToken() ?? "")}`,
};

export type TSStepView = {
  id: number;
  ordinal: number;
  title: string;
  instruction: string;
  why: string | null;
  safety_level: string;
  requires_proof: boolean;
  proof_kind: "photo" | "video" | "screenshot" | "none";
  status: "PENDING" | "DONE_FIXED" | "DONE_NOT_FIXED" | "SKIPPED" | "CANT_DO";
  source_ids: number[];
  proofs: number;
};
export type TSSourceView = { id: number; title: string | null; url: string };
export type TSMessageView = { role: string; body: string; step_id: number | null; created_at: string };

export type BuyerSession = {
  id: number;
  status: string;
  product_name: string;
  product_model: string | null;
  expires_at: string;
  language: string | null;
  plan_generic: boolean | null;
  awaiting_fixed_confirm: boolean;
  ended: boolean;
  steps: TSStepView[];
  sources: TSSourceView[];
  messages: TSMessageView[];
  device_key?: string | null;
  reply?: string | null;
};

const DEVICE_KEY = (id: string) => `ds_help_dev_${id}`;
function deviceKey(id: string): string | null {
  try {
    return localStorage.getItem(DEVICE_KEY(id)) ?? sessionStorage.getItem(DEVICE_KEY(id));
  } catch {
    return null;
  }
}
function rememberDevice(id: string, key: string) {
  try {
    localStorage.setItem(DEVICE_KEY(id), key);
  } catch {
    try {
      sessionStorage.setItem(DEVICE_KEY(id), key);
    } catch {
      /* storage blocked: the page still works until it's reloaded */
    }
  }
}

/** Buyer page calls: the link token + this browser's device key, never the merchant's session. */
async function helpReq(sessionId: string, token: string, path = "", init?: RequestInit): Promise<BuyerSession> {
  const dev = deviceKey(sessionId);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/help/${encodeURIComponent(sessionId)}${path}`, {
      ...init,
      headers: { "X-Help-Token": token, ...(dev ? { "X-Help-Device": dev } : {}), ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("We can't reach the server. Check your connection and try again.", 0, null);
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(errorText(body?.detail) || "Something went wrong.", res.status, body?.detail);
  if (body?.device_key) rememberDevice(sessionId, body.device_key);
  return body as BuyerSession;
}

const json = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// The first page load binds the link to this browser. Concurrent loads (React StrictMode runs effects twice,
// fast reloads) must share that one request, or the second one arrives without the device key and is refused.
const inflight = new Map<string, Promise<BuyerSession>>();

export const helpApi = {
  get: (sessionId: string, token: string) => {
    const k = `${sessionId}:${token}`;
    let p = inflight.get(k);
    if (!p) {
      p = helpReq(sessionId, token).finally(() => inflight.delete(k));
      inflight.set(k, p);
    }
    return p;
  },
  send: (sessionId: string, token: string, body: string, stepId?: number, stepStatus?: TSStepView["status"]) =>
    helpReq(sessionId, token, "/messages", json({ body, step_id: stepId ?? null, step_status: stepStatus ?? null })),
  confirm: (sessionId: string, token: string, fixed: boolean) => helpReq(sessionId, token, "/confirm", json({ fixed })),
  handoff: (sessionId: string, token: string) => helpReq(sessionId, token, "/handoff", { method: "POST" }),
  uploadProof: (sessionId: string, token: string, stepId: number, file: File) =>
    helpReq(sessionId, token, `/steps/${stepId}/proof`, { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file }),
};
