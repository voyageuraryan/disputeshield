export type Product = {
  sku: string;
  name: string;
  price: string;
  currency: string;
  category: string;
  listing: string;
  image: string;
};

export type StoreConfig = { client_id: string; currency: string; sdk_url: string };

// Empty in dev (Vite proxies /api); on Render, the backend's public URL (set at build time).
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body?.detail;
    const debug = detail?.debug_id ? ` (PayPal debug id ${detail.debug_id})` : "";
    const text = typeof detail === "string" ? detail : detail?.error ?? (detail ? JSON.stringify(detail) : "request failed");
    throw new Error(`${res.status}: ${text}${debug}`);
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
};

export type Order = {
  id: number;
  created_at: string;
  buyer_email: string | null;
  amount: string;
  currency: string;
  items: { sku?: string; name?: string; quantity?: number; unit_amount?: string }[] | null;
  shipping_address: Record<string, string> | null;
  product_listing: string | null;
  paypal_order_id: string | null;
  capture_id: string | null;
  invoice_id: string | null;
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
  pdfUrl: (id: number) => `${API_BASE}/api/decisions/${id}/evidence.pdf`,
  source: (evidenceId: number) => req<EvidenceSource>(`/api/evidence/${evidenceId}/source`),
};
