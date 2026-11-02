// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../api";

const calls: unknown[][] = [];
vi.mock("../api", () => ({
  ordersApi: {
    addTracking: (...a: unknown[]) => (calls.push(["track", ...a]), Promise.resolve({ id: 1 })),
    recordDelivery: (...a: unknown[]) => (calls.push(["deliver", ...a]), Promise.resolve({})),
  },
}));
import ShippingPanel, { todayIso } from "../ShippingPanel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const ADDR = { address_line_1: "1 Main St", postal_code: "95131", country_code: "US" };
const order = (shipments: Order["shipments"] = []): Order => ({
  id: 7, created_at: "2026-10-01T10:00:00Z", buyer_email: "b@x.test", amount: "89.00", currency: "USD", items: [],
  shipping_address: ADDR, product_listing: null, paypal_order_id: "P", capture_id: "C", invoice_id: null, shipments,
});
const shipped = { id: 3, carrier: "USPS", tracking_number: "9400", status: "SHIPPED", delivered_at: null, delivered_address: null };

let el: HTMLDivElement;
let root: Root;
const saved: string[] = [];
beforeEach(() => {
  el = document.createElement("div");
  document.body.appendChild(el);
  root = createRoot(el);
  calls.length = 0;
  saved.length = 0;
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
});

const render = async (o: Order, digitalOnly = false) =>
  act(async () => root.render(<ShippingPanel order={o} digitalOnly={digitalOnly} onSaved={(w) => saved.push(w)} />));
const button = (text: string) => [...el.querySelectorAll("button")].find((b) => b.textContent?.includes(text)) as HTMLButtonElement;
const field = (name: string) => el.querySelector(`[aria-label="${name}"]`) as HTMLInputElement;
async function type(name: string, value: string) {
  const input = field(name);
  const proto = input instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event(input instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
const click = (b: HTMLElement) => act(async () => b.click());

describe("ShippingPanel", () => {
  it("says there is nothing to ship for digital orders", async () => {
    await render(order(), true);
    expect(el.textContent).toContain("Digital item: nothing to ship");
    expect(button("Add tracking")).toBeUndefined();
  });

  it("adds tracking (save disabled until a number is typed)", async () => {
    await render(order());
    expect(el.textContent).toContain("No tracking yet");
    await click(button("Add tracking"));
    expect(button("Save tracking").disabled).toBe(true);
    await type("Tracking number", " 9400111 ");
    await click(button("Save tracking"));
    expect(calls).toEqual([["track", 7, "USPS", "9400111"]]);
    expect(saved).toEqual(["tracking"]);
  });

  it("uses the typed name for an Other carrier", async () => {
    await render(order());
    await click(button("Add tracking"));
    await type("Carrier", "OTHER");
    await type("Tracking number", "AB1");
    expect(button("Save tracking").disabled).toBe(true); // carrier name still empty
    await type("Carrier name", "India Post");
    await click(button("Save tracking"));
    expect(calls[0]).toEqual(["track", 7, "India Post", "AB1"]);
  });

  it("records delivery to the shipping address by default", async () => {
    await render(order([shipped]));
    await click(button("Record delivery"));
    await click(button("Save delivery"));
    expect(calls).toEqual([["deliver", 7, 3, todayIso(), undefined]]);
    expect(saved).toEqual(["delivery"]);
  });

  it("needs street, postal code and country for a different address; rejects future dates", async () => {
    await render(order([shipped]));
    await click(button("Record delivery"));
    await type("Delivered on", "2999-01-01");
    expect(button("Save delivery").disabled).toBe(true);
    expect(el.textContent).toContain("Pick a date between");
    await type("Delivered on", todayIso());
    await click([...el.querySelectorAll('input[type="radio"]')][1] as HTMLElement);
    expect(button("Save delivery").disabled).toBe(true);
    await type("Street", "9 Elm Rd");
    await type("Postal code", "10001");
    await click(button("Save delivery"));
    expect(calls[0][4]).toMatchObject({ address_line_1: "9 Elm Rd", postal_code: "10001", country_code: "US" });
  });

  it("shows a recorded delivery and lets you change it", async () => {
    await render(order([{ ...shipped, status: "DELIVERED", delivered_at: "2026-10-05T12:00:00Z", delivered_address: ADDR, delivery_recorded_by: "merchant" }]));
    expect(el.textContent).toContain("Delivered");
    expect(el.textContent).toContain("recorded by you");
    expect(button("Change delivery details")).toBeDefined();
  });
});
