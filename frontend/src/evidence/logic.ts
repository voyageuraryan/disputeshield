// Phase 12: pure logic for adding evidence (kept out of the components so it can be unit-tested).
import type { CoachItem, ConfirmBody, KeyFields, MerchantUpload } from "../api";
import { label } from "../format";

export const MAX_BYTES = 10 * 1024 * 1024; // PayPal: each evidence file under 10 MB
export const ACCEPT = "application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png";
const OK_TYPES = new Set(["application/pdf", "image/jpeg", "image/png"]);
const OK_EXT = /\.(pdf|jpe?g|png)$/i;
export const FACT_MAX = 500;

/** A quick check before uploading (the server checks the real bytes again). */
export function checkFile(file: { name: string; size: number; type: string }): string | null {
  if (file.size === 0) return "The file is empty.";
  if (file.size >= MAX_BYTES) return `The file is ${(file.size / 1024 / 1024).toFixed(1)} MB; PayPal accepts evidence files under 10 MB.`;
  if (!OK_TYPES.has(file.type) && !OK_EXT.test(file.name)) return "Only PDF, JPG or PNG files can be sent to PayPal as evidence.";
  return null;
}

export function checkInput(file: { name: string; size: number; type: string } | null, fact: string): string | null {
  if (!file && !fact.trim()) return "Attach a file or type the fact.";
  if (fact.length > FACT_MAX) return `Keep the fact under ${FACT_MAX} characters.`;
  return file ? checkFile(file) : null;
}

export type FieldValues = KeyFields & { document_kind: string; note: string };
export const FIELD_KEYS: { key: keyof FieldValues; label: string; placeholder: string; types?: string[] }[] = [
  { key: "document_kind", label: "What it is", placeholder: "e.g. USPS proof of delivery" },
  { key: "tracking_number", label: "Tracking number", placeholder: "as printed", types: ["tracking", "delivery_confirmation"] },
  { key: "carrier", label: "Carrier", placeholder: "USPS, UPS, FedEx…", types: ["tracking", "delivery_confirmation"] },
  { key: "delivery_date", label: "Delivery date", placeholder: "YYYY-MM-DD", types: ["delivery_confirmation", "tracking"] },
  { key: "delivered_to_address", label: "Delivered to", placeholder: "street, city, ZIP", types: ["delivery_confirmation", "tracking"] },
  { key: "amount", label: "Amount", placeholder: "89.00", types: ["invoice", "order"] },
  { key: "currency", label: "Currency", placeholder: "USD", types: ["invoice", "order"] },
  { key: "note", label: "Note (optional)", placeholder: "anything PayPal should know about this document" },
];

/** Which fields to show for an evidence type (the rest stay hidden but keep their values). */
export const fieldsFor = (evidenceType: string) => FIELD_KEYS.filter((f) => !f.types || f.types.includes(evidenceType));

/** The form's starting values: what Gemini read (a suggestion), never confirmed until the merchant presses Confirm. */
export function prefill(u: MerchantUpload): FieldValues {
  const k = u.extraction?.key_fields;
  return {
    document_kind: u.extraction?.document_kind ?? "",
    tracking_number: k?.tracking_number ?? "",
    carrier: k?.carrier ?? "",
    delivery_date: /^\d{4}-\d{2}-\d{2}$/.test(k?.delivery_date ?? "") ? (k?.delivery_date as string) : "",
    delivered_to_address: k?.delivered_to_address ?? "",
    amount: /^\d+(\.\d{1,2})?$/.test(k?.amount ?? "") ? (k?.amount as string) : "",
    currency: k?.currency ?? "",
    note: "",
  };
}

/** Form values -> confirm body: trimmed, blanks dropped (the server treats them as "not given"). */
export function toConfirmBody(v: FieldValues, evidenceType: string): ConfirmBody {
  const body: Record<string, string> = { evidence_type: evidenceType };
  const shown = new Set(fieldsFor(evidenceType).map((f) => f.key));
  for (const [k, val] of Object.entries(v)) {
    const s = String(val ?? "").trim();
    if (s && shown.has(k as keyof FieldValues)) body[k] = s;
  }
  return body as ConfirmBody;
}

/** One line of "What would win this?": "Add delivery confirmation → score 50 → 85 → Fight". */
export function coachLine(c: CoachItem): string {
  const what = c.type.split("+").map((t) => label(t).toLowerCase()).join(" + ");
  const pct = (n: number) => Math.round(n * 100);
  const changes = c.action_after !== c.action_now ? ` → ${label(c.action_after)}` : "";
  return `Add ${what} → score ${pct(c.score_now)} → ${pct(c.score_after)}${changes}`;
}

/** What Gemini said about the read, in words for the merchant. */
export function readNote(u: MerchantUpload): string {
  const e = u.extraction;
  if (!e) return "";
  if (e.skipped) return e.skipped.charAt(0).toUpperCase() + e.skipped.slice(1) + ".";
  if (e.error) return "Couldn't read the document automatically: fill in the fields yourself.";
  return `Read automatically (${e.confidence ?? "unknown"} confidence). Check every field: nothing is used until you confirm.`;
}
