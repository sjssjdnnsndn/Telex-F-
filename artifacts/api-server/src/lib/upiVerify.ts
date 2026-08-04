import { logger } from "./logger";

const BHARATPE_TOKEN_ENV = process.env["BHARATPE_TOKEN"];

if (!BHARATPE_TOKEN_ENV) {
  throw new Error(
    "BHARATPE_TOKEN environment variable is required but was not provided.",
  );
}

const BHARATPE_TOKEN: string = BHARATPE_TOKEN_ENV;

export interface UtrVerificationResult {
  success: boolean;
  amount: number;
  date: string;
  error?: string;
}

export interface OrderVerificationResult {
  success: boolean;
  amount: number;
  utr: string;
  date: string;
  error?: string;
}

/**
 * Verifies a UTR/transaction id against the payment gateway.
 * Mirrors the reference bot's BharatPe/BharatQR worker verification API.
 */
export async function verifyUtrWithGateway(
  utr: string,
): Promise<UtrVerificationResult> {
  const url = new URL("https://bharatqr.udayscriptsx.workers.dev/");
  url.searchParams.set("token", BHARATPE_TOKEN);
  url.searchParams.set("id", utr);
  url.searchParams.set("type", "verify");

  let data: unknown;
  try {
    const response = await fetch(url.toString());
    data = await response.json();
  } catch (err) {
    logger.error({ err }, "UPI verification gateway request failed");
    return { success: false, amount: 0, date: "", error: "Verification service is unavailable right now" };
  }

  if (!data || typeof data !== "object") {
    return { success: false, amount: 0, date: "", error: "No response from verification service" };
  }

  const record = data as Record<string, unknown>;
  const status = String(record["status"] ?? "").toUpperCase();

  if (status !== "SUCCESS") {
    return { success: false, amount: 0, date: "", error: "Payment not found. Please check your UTR." };
  }

  const amount = Number(record["amount"] ?? 0);
  if (!Number.isFinite(amount) || amount <= 0) {
    return { success: false, amount: 0, date: "", error: "Invalid payment amount returned" };
  }

  const date = String(record["date"] ?? "Unknown");

  return { success: true, amount, date };
}

/**
 * Polls the same merchant-status gateway used by the uploaded Python bot.
 * The merchant id never reaches the browser; only the API server calls this.
 */
export async function verifyOrderWithGateway(
  orderId: string,
  merchantId: string,
): Promise<OrderVerificationResult> {
  const url = new URL("https://paytm-api.lightdns.me/");
  url.searchParams.set("mid", merchantId);
  url.searchParams.set("oid", orderId);

  try {
    const response = await fetch(url.toString());
    const data = (await response.json()) as Record<string, unknown>;
    if (String(data["STATUS"] ?? "").toUpperCase() !== "TXN_SUCCESS") {
      return { success: false, amount: 0, utr: "", date: "" };
    }

    const amount = Number(data["TXNAMOUNT"] ?? data["amount"] ?? 0);
    const utr = String(data["BANKTXNID"] ?? data["TXNID"] ?? "");
    if (!Number.isFinite(amount) || amount <= 0 || !utr) {
      return { success: false, amount: 0, utr: "", date: "", error: "Gateway returned incomplete payment data" };
    }
    return {
      success: true,
      amount,
      utr,
      date: String(data["TXNDATE"] ?? data["date"] ?? "Unknown"),
    };
  } catch (err) {
    logger.error({ err }, "UPI order verification gateway request failed");
    return { success: false, amount: 0, utr: "", date: "", error: "Verification service is unavailable right now" };
  }
}
