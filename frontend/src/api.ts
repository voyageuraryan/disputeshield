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
  troubleshooting?: string | null; // buyer troubleshooting session status (Phase 9D)
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
  offer_types?: string[];
  can_accept_claim?: boolean;
  can_offer?: boolean;
  can_replace?: boolean;
  can_fight: boolean;
  can_settle: boolean;
};

/** Phase 13: one way to resolve the dispute, with its expected net outcome (app/rules/money.py). */
export type MoneyOption = {
  action: "FIGHT" | "SETTLE" | "SETTLE_PARTIAL" | "REPLACEMENT";
  label: string;
  available: boolean;
  net: string | null;
  chance: string | null;
  offer_amount: string | null;
  why: string;
};

export type Decision = {
  id: number;
  dispute_id: number;
  recommendation: "FIGHT" | "SETTLE" | "SETTLE_PARTIAL" | "REPLACEMENT" | "GATHER_MORE";
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
  data_as_of?: string | null;
  stale_sources?: string[] | null;
  offer_amount?: string | null; // Phase 13: SETTLE_PARTIAL offer
  options?: MoneyOption[] | null; // calculator: Fight / Full refund / Partial offer / Replacement
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

export type CoachItem = {
  type: string; // one evidence type, or "a+b" for all missing required items together
  required: boolean;
  score_now: number;
  score_after: number;
  gain: number;
  action_now: string;
  action_after: string;
  howto: string[];
  assumption: string;
};

export type Checklist = {
  reason_label: string | null;
  supported: boolean;
  rule_score: number;
  items: { type: string; required: boolean; present: boolean; evidence_ids: number[] }[];
  red_flags: { code: string; description: string }[];
  baseline: string | null;
  baseline_why: string | null;
  coach?: CoachItem[];
};

export type KeyFields = {
  tracking_number: string;
  carrier: string;
  delivery_date: string;
  delivered_to_address: string;
  amount: string;
  currency: string;
};

/** Phase 12: evidence the merchant added (PENDING until they confirm the fields). */
export type MerchantUpload = {
  id: number;
  evidence_type: string;
  status: "PENDING" | "CONFIRMED" | "DISCARDED";
  kind: "file" | "fact";
  original_name: string | null;
  mime: string | null;
  size: number | null;
  fact: string | null;
  extraction: { document_kind?: string; key_fields?: KeyFields; confidence?: string; skipped?: string; error?: string } | null;
  fields: Record<string, string> | null;
  flags: string[] | null;
  summary: string | null;
  created_at: string;
  confirmed_at: string | null;
};

export type ConfirmBody = Partial<KeyFields> & { evidence_type?: string; document_kind?: string; note?: string };
export type RunInfo = { started: boolean; run_key: string | null; message: string };

export type EvidenceSource = { evidence_id: number; source: string; source_ref: string; title: string; origin: string; record: unknown };

export type DisputeDetail = {
  settle_threshold: string;
  dispute: DisputeRow;
  order: Order | null;
  case: CaseFile;
  checklist: Checklist;
  decisions: Decision[];
  timeline: Step[];
  uploads?: MerchantUpload[];
  upload_types?: string[];
  offer_bounds?: { min: string; max: string } | null;
};

export type SendAction = "FIGHT" | "SETTLE" | "SETTLE_PARTIAL" | "REPLACEMENT";
export type ApproveBody = { action?: SendAction; note?: string; approved_by?: string; offer_amount?: string };

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

// ---- Phase 12: merchant evidence uploads ---------------------------------------------------------

const uploadsPath = (id: string) => `/api/disputes/${encodeURIComponent(id)}/uploads`;

export const uploadsApi = {
  /** multipart: evidence_type + file and/or fact (the browser sets the multipart boundary). */
  create: async (id: string, evidenceType: string, file: File | null, fact: string): Promise<MerchantUpload> => {
    const form = new FormData();
    form.append("evidence_type", evidenceType);
    if (file) form.append("file", file, file.name || "upload");
    if (fact.trim()) form.append("fact", fact.trim());
    let res: Response;
    try {
      res = await fetch(`${API_BASE}${uploadsPath(id)}`, {
        method: "POST",
        body: form,
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
      });
    } catch {
      throw new Error("Can't reach the DisputeShield server. Check your connection and try again.");
    }
    const body = await res.json().catch(() => null);
    if (res.status === 401) {
      goToLogin("Your session ended. Please log in again.");
      throw new Error("401: login required");
    }
    if (!res.ok) throw new ApiError(errorText(body?.detail), res.status, body?.detail);
    return body as MerchantUpload;
  },
  confirm: (id: string, uploadId: number, body: ConfirmBody) =>
    req<{ upload: MerchantUpload; run: RunInfo }>(`${uploadsPath(id)}/${uploadId}/confirm`, { method: "POST", body: JSON.stringify(body) }),
  remove: (id: string, uploadId: number) => req<RunInfo>(`${uploadsPath(id)}/${uploadId}`, { method: "DELETE" }),
  fileUrl: (id: string, uploadId: number) =>
    `${API_BASE}${uploadsPath(id)}/${uploadId}/file?token=${encodeURIComponent(getToken() ?? "")}`,
};

// ---- settings & live events --------------------------------------------------------------

export type MerchantSettings = {
  settle_threshold: string;
  refund_policy: string | null;
  auto_invite_troubleshooting: boolean;
  currency: string;
  updated_at: string;
  dispute_fee?: string;
  handling_minutes?: number;
  hourly_value?: string;
  max_offer_pct?: number;
  fee_sources?: { country: string; standard: string; high_volume: string; url: string }[];
};

export type SettingsPatch = {
  settle_threshold?: string;
  refund_policy?: string;
  auto_invite_troubleshooting?: boolean;
  dispute_fee?: string;
  handling_minutes?: number;
  hourly_value?: string;
  max_offer_pct?: number;
};

export const settingsApi = {
  get: () => req<MerchantSettings>("/api/settings"),
  save: (patch: SettingsPatch) =>
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
  finished_at?: string | null;
  merchant_active?: boolean; // the seller replied in the chat: the assistant is paused
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
  proofs?: TSProofView[];
};

export type TSProofChecks = {
  detected_type?: string;
  converted?: string;
  duration_s?: number;
  metadata_removed?: string[];
  taken_after_dispute?: "yes" | "no" | "unknown";
  capture_time_note?: string;
  duplicate_of_steps?: number[];
  reused_from_other_cases?: string[];
  near_duplicates?: { proof_id: number; distance: number; step?: number; case?: string }[];
  evidence_ready?: boolean;
  evidence_copy?: string;
  purged_at?: string; // G44: file deleted under the retention policy (the record stays)
  purged_reason?: string;
};
export type TSProofView = {
  id: number;
  step_id: number | null;
  mime: string;
  size: number;
  created_at: string;
  original_mime?: string | null;
  checks?: TSProofChecks | null;
  vision_verdict?: { shows_step?: "yes" | "no" | "unclear"; what_is_visible?: string; concerns?: string; skipped?: string; error?: string } | null;
  evidence_ready?: boolean;
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
  reply: (disputeId: string, body: string) =>
    req<TroubleshootingStatus>(`/api/disputes/${encodeURIComponent(disputeId)}/troubleshooting/messages`, {
      method: "POST",
      body: JSON.stringify({ body }),
    }),
  resumeAssistant: (disputeId: string) =>
    req<TroubleshootingStatus>(`/api/disputes/${encodeURIComponent(disputeId)}/troubleshooting/assistant`, {
      method: "POST",
      body: JSON.stringify({ resume: true }),
    }),
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
  proof_files?: { id: number; kind: "image" | "video" }[];
  buyer_note?: string | null;
};
export type TSSourceView = { id: number; title: string | null; url: string };
export type TSMessageView = { id: number; role: string; body: string; step_id: number | null; created_at: string };

export type BuyerSession = {
  id: number;
  status: string;
  product_name: string;
  product_model: string | null;
  expires_at: string;
  language: string | null;
  plan_generic: boolean | null;
  awaiting_fixed_confirm: boolean;
  agent_working?: boolean;
  seller_active?: boolean; // the seller is chatting in person (the assistant is paused)
  ended: boolean;
  product_image?: string | null;
  store_name?: string;
  order_ref?: string | null;
  paypal_case_id?: string | null;
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
function helpHeaders(sessionId: string, token: string): Record<string, string> {
  const dev = deviceKey(sessionId);
  return { "X-Help-Token": token, ...(dev ? { "X-Help-Device": dev } : {}) };
}

async function helpReq<T = BuyerSession>(sessionId: string, token: string, path = "", init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/help/${encodeURIComponent(sessionId)}${path}`, {
      ...init,
      headers: { ...helpHeaders(sessionId, token), ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("We can't reach the server. Check your connection and try again.", 0, null);
  }
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(errorText(body?.detail) || "Something went wrong.", res.status, body?.detail);
  if (body?.device_key) rememberDevice(sessionId, body.device_key);
  return body as T;
}

export type StepAnswer = "DONE_FIXED" | "DONE_NOT_FIXED" | "CANT_DO";
export type ProofUpload = BuyerSession & { proof: { id: number; step_id: number; mime: string; size: number; already_uploaded: boolean } };

/** Upload with progress (fetch can't report upload progress, XMLHttpRequest can). */
function uploadWithProgress(sessionId: string, token: string, stepId: number, file: File, onProgress?: (pct: number) => void): Promise<ProofUpload> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/help/${encodeURIComponent(sessionId)}/steps/${stepId}/proof`);
    for (const [k, v] of Object.entries(helpHeaders(sessionId, token))) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(Math.round((e.loaded / e.total) * 100));
    xhr.onerror = () => reject(new ApiError("The upload didn't go through. Check your connection and try again.", 0, null));
    xhr.onload = () => {
      let body: { detail?: unknown } | null = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as unknown as ProofUpload);
      else reject(new ApiError(errorText(body?.detail) || "The upload failed.", xhr.status, body?.detail));
    };
    const form = new FormData();
    form.append("file", file, file.name || "upload");
    xhr.send(form);
  });
}

export type HelpStreamHandlers = {
  onMessage: (m: TSMessageView) => void;
  onState: (s: Partial<BuyerSession>) => void;
};

/** One connection to the buyer's live stream. fetch() (not EventSource) keeps the token and device key in
 * headers, out of URLs and server logs. Resolves when the server ends the stream ("bye") or the connection drops;
 * rejects with ApiError for 4xx (link no longer valid) so the caller stops reconnecting. */
async function helpStream(sessionId: string, token: string, after: number, h: HelpStreamHandlers, signal: AbortSignal): Promise<void> {
  const res = await fetch(`${API_BASE}/api/help/${encodeURIComponent(sessionId)}/events?after=${after}`, {
    headers: { ...helpHeaders(sessionId, token), Accept: "text/event-stream" },
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(errorText(body?.detail) || "stream refused", res.status, body?.detail);
  }
  if (!res.body) throw new ApiError("streaming not supported", 0, null);
  const { parseSSE } = await import("./help/logic");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += decoder.decode(value, { stream: true });
    const { events, rest } = parseSSE(buf);
    buf = rest;
    for (const e of events) {
      if (e.event === "message") h.onMessage(JSON.parse(e.data));
      else if (e.event === "state") h.onState(JSON.parse(e.data));
      else if (e.event === "bye") return;
    }
  }
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
  /** 202: the reply arrives on the stream (and in the next GET). */
  send: (sessionId: string, token: string, body: string) =>
    helpReq<{ accepted: boolean; agent_working: boolean }>(sessionId, token, "/messages", json({ body })),
  step: (sessionId: string, token: string, stepId: number, status: StepAnswer, note = "") =>
    helpReq(sessionId, token, `/steps/${stepId}`, json({ status, note })),
  confirm: (sessionId: string, token: string, fixed: boolean) => helpReq(sessionId, token, "/confirm", json({ fixed })),
  handoff: (sessionId: string, token: string) => helpReq(sessionId, token, "/handoff", { method: "POST" }),
  uploadProof: uploadWithProgress,
  /** The buyer's own upload as a blob (an <img> can't send the link headers). */
  proofBlob: async (sessionId: string, token: string, proofId: number): Promise<Blob> => {
    const res = await fetch(`${API_BASE}/api/help/${encodeURIComponent(sessionId)}/proofs/${proofId}`, { headers: helpHeaders(sessionId, token) });
    if (!res.ok) throw new ApiError("couldn't load the file", res.status, null);
    return res.blob();
  },
  /** G44: the buyer removes their own upload while the page is open. */
  deleteProof: (sessionId: string, token: string, proofId: number) =>
    helpReq(sessionId, token, `/proofs/${proofId}`, { method: "DELETE" }),
  stream: helpStream,
};

// ---- Phase 14: analytics ---------------------------------------------------------------------------------

export type Fact = {
  id: number; ref: string; is_demo: boolean; reason: string; reason_label: string; stage: string | null;
  amount: number; currency: string; outcome: string; outcome_code: string | null; final_action: string;
  recommended_action: string | null; followed: boolean | null; overridden: boolean; won: boolean; contested: boolean;
  correct: boolean | null; rule_score: number | null; offer_amount: number | null; money_protected: number;
  money_refunded: number; created_at: string | null; response_at: string | null; resolved_at: string | null;
  due_at: string | null; month: string | null; hours_to_response: number | null; before_deadline: boolean | null;
};
export type Kpis = {
  count: number;
  money: Record<string, { protected: number; refunded: number }>;
  win_rate: number | null;
  contested: number;
  win_rate_by_reason: { reason: string; label: string; won: number; contested: number; win_rate: number | null; accuracy: number | null; count: number }[];
  accuracy: { rate: number | null; correct: number; scored: number };
  median_hours_to_response: number | null;
  before_deadline: { pct: number | null; on_time: number; answered: number };
  overrides: { count: number; outcomes: Record<string, number>; won: number; accuracy_when_overridden: number | null };
};
export type CalibrationBin = { lower: number; upper: number; n: number; wins: number; raw: number | null; default: number; p: number };
export type AnalyticsSummary = {
  dataset: "real" | "demo";
  demo: boolean;
  has_real: boolean;
  has_demo: boolean;
  kpis: Kpis;
  by_month: { month: string; money_protected: number; money_refunded: number; win_rate: number | null; accuracy: number | null; count: number; scored?: number }[];
  calibration: { min_outcomes: number; applies_to_decisions: boolean; reasons: { reason: string; n: number; used: boolean; bins: CalibrationBin[] }[] };
  facts: Fact[];
};
export type QFilter = { column: string; op: string; value: string };
export type AskQuery = { filters: QFilter[]; group_by: string[]; metric: string; chart: "bar" | "line" | "pie" | "table"; title: string };
export type QueryResult = {
  dataset: string; question?: string; query: AskQuery; kind: "rows" | "groups" | "value";
  columns: string[]; matched: number; rows: Record<string, unknown>[]; value: number | null;
};

export const analyticsApi = {
  summary: (dataset?: "real" | "demo") => req<AnalyticsSummary>(`/api/analytics${dataset ? `?dataset=${dataset}` : ""}`),
  query: (dataset: string, query: AskQuery) =>
    req<QueryResult>("/api/analytics/query", { method: "POST", body: JSON.stringify({ dataset, query }) }),
  ask: (dataset: string, question: string) =>
    req<QueryResult>("/api/analytics/ask", { method: "POST", body: JSON.stringify({ dataset, question }) }),
};
