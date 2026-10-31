// Minimal typings + loader for the PayPal JavaScript SDK v6
// (developer.paypal.com/sdk/js/set-up). Sandbox script only.

export type ApproveData = { orderId: string; payerId?: string };
export type PaymentSession = {
  start: (opts: { presentationMode: "auto" | "popup" | "modal" | "redirect" }, order: Promise<{ orderId: string }>) => Promise<void>;
};
export type SdkInstance = {
  findEligibleMethods: (o: { currencyCode: string }) => Promise<{ isEligible: (m: string) => boolean }>;
  createPayPalOneTimePaymentSession: (o: {
    onApprove: (d: ApproveData) => Promise<void> | void;
    onCancel?: (d: { orderId?: string }) => void;
    onError?: (e: { code?: string; message?: string }) => void;
  }) => PaymentSession;
};

declare global {
  interface Window {
    paypal?: { createInstance: (o: { clientId: string; components: string[]; pageType?: string }) => Promise<SdkInstance> };
  }
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      "paypal-button": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & { type?: string };
    }
  }
}

const SANDBOX_SDK = "https://www.sandbox.paypal.com/web-sdk/v6/core";
let loading: Promise<void> | null = null;

function loadScript(src: string): Promise<void> {
  if (src !== SANDBOX_SDK) return Promise.reject(new Error("refusing non-sandbox PayPal SDK"));
  if (window.paypal) return Promise.resolve();
  loading ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => {
      loading = null;
      reject(new Error("failed to load PayPal SDK"));
    };
    document.head.appendChild(s);
  });
  return loading;
}

export async function initPayPal(sdkUrl: string, clientId: string): Promise<SdkInstance> {
  if (!clientId) throw new Error("PAYPAL_CLIENT_ID is not configured on the backend");
  await loadScript(sdkUrl);
  if (!window.paypal) throw new Error("PayPal SDK did not initialise");
  return window.paypal.createInstance({ clientId, components: ["paypal-payments"], pageType: "checkout" });
}
