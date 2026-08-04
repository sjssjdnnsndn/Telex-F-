import {
  getOrCreateUser,
  getUsersCollection,
  getUsedUtrsCollection,
  getDepositSessionsCollection,
  type DepositSession,
} from "./mongo";
import { verifyUtrWithGateway, verifyOrderWithGateway } from "./upiVerify";

export interface CreditResult {
  ok: true;
  amount: number;
  oldBalance: number;
  newBalance: number;
  utr: string;
  ref: string;
  date: string;
}

export interface CreditError {
  ok: false;
  error: string;
}

export interface SessionStatusResult {
  orderId: string;
  status: DepositSession["status"];
  amountInr: number;
  amountUsd: number;
  newBalance?: number;
  utr?: string;
  ref?: string;
  date?: string;
  error?: string;
}

// Matches the bot's own conversion rate (python-bot/main.py: USD_TO_INR_RATE)
// so amounts credited here line up with the bot's USD-denominated balance.
const USD_TO_INR_RATE = Number(process.env["USD_TO_INR_RATE"] ?? "96.0");
const PAYMENT_AMOUNT_TOLERANCE = 0.01;

function generateRef(telegramUserId: number): string {
  return `TXN${Date.now()}${String(telegramUserId).slice(-4)}`;
}

/**
 * Verifies a UTR against the payment gateway and, if valid and globally
 * unused, credits the corresponding Telegram user's balance.
 *
 * UTR uniqueness is enforced globally (not per-user) via a unique index on
 * `used_utrs.utr`: the claim is inserted first and atomically, so the same
 * UTR can never be credited twice even under concurrent requests or when
 * submitted for different user ids.
 */
export async function verifyAndCredit(
  telegramUserId: number,
  utrRaw: string,
): Promise<CreditResult | CreditError> {
  const utr = utrRaw.trim();

  if (utr.length < 6 || !/^[a-zA-Z0-9]+$/.test(utr)) {
    return { ok: false, error: "Invalid UTR. Please enter a valid transaction/UTR number." };
  }

  const usedUtrsCol = await getUsedUtrsCollection();
  const usersCol = await getUsersCollection();

  const verification = await verifyUtrWithGateway(utr);

  if (!verification.success) {
    return { ok: false, error: verification.error ?? "Payment could not be verified." };
  }

  const amountInr = verification.amount;
  const amountUsd = amountInr / USD_TO_INR_RATE;
  const ref = generateRef(telegramUserId);

  // Atomic global claim: this insert fails with a duplicate-key error if the
  // UTR has already been claimed by anyone, so only one caller ever proceeds
  // to credit a balance for a given UTR.
  try {
    await usedUtrsCol.insertOne({
      utr,
      telegramUserId,
      amountInr,
      amountUsd,
      ref,
      date: verification.date,
      createdAt: new Date(),
    });
  } catch (err) {
    const isDuplicateKey =
      typeof err === "object" && err !== null && "code" in err && (err as { code?: number }).code === 11000;
    if (isDuplicateKey) {
      return { ok: false, error: "This UTR has already been used. Please make a new deposit." };
    }
    throw err;
  }

  await getOrCreateUser(telegramUserId);

  const updated = await usersCol.findOneAndUpdate(
    { id: telegramUserId },
    { $inc: { balance: amountUsd } },
    { returnDocument: "after" },
  );

  if (!updated) {
    throw new Error(`Failed to credit balance for telegramUserId=${telegramUserId} after claiming UTR ${utr}`);
  }

  const newBalance = updated.balance;
  const oldBalance = newBalance - amountUsd;

  return {
    ok: true,
    amount: amountInr,
    oldBalance,
    newBalance,
    utr,
    ref,
    date: verification.date,
  };
}

const activeSessionMonitors = new Set<string>();

function sessionResult(session: DepositSession): SessionStatusResult {
  return {
    orderId: session.orderId,
    status: session.status,
    amountInr: session.actualAmountInr ?? session.amountInr,
    amountUsd: session.amountUsd,
    newBalance: session.newBalance,
    utr: session.utr,
    ref: session.ref,
    date: session.date,
    error: session.error,
  };
}

async function creditPaidSession(session: DepositSession, actualAmountInr: number, utr: string, date: string): Promise<void> {
  const sessions = await getDepositSessionsCollection();
  const amountUsd = actualAmountInr / USD_TO_INR_RATE;
  const ref = generateRef(session.telegramUserId);
  const usedUtrs = await getUsedUtrsCollection();

  // Claim the gateway transaction before changing the session or balance. This
  // makes automatic verification and the manual UTR fallback share one global
  // duplicate-payment guard.
  try {
    await usedUtrs.insertOne({
      utr,
      telegramUserId: session.telegramUserId,
      amountInr: actualAmountInr,
      amountUsd,
      ref,
      date,
      createdAt: new Date(),
    });
  } catch (err) {
    const duplicate =
      typeof err === "object" &&
      err !== null &&
      "code" in err &&
      (err as { code?: number }).code === 11000;
    if (duplicate) return;
    throw err;
  }

  const claimed = await sessions.findOneAndUpdate(
    { orderId: session.orderId, status: "pending" },
    { $set: { status: "paid", actualAmountInr, amountUsd, utr, date, ref } },
    { returnDocument: "after" },
  );
  if (!claimed) {
    await usedUtrs.deleteOne({ utr, ref });
    return;
  }

  const users = await getUsersCollection();
  await getOrCreateUser(session.telegramUserId);
  const updated = await users.findOneAndUpdate(
    { id: session.telegramUserId },
    { $inc: { balance: amountUsd } },
    { returnDocument: "after" },
  );
  if (!updated) {
    await sessions.updateOne(
      { orderId: session.orderId, status: "paid" },
      { $set: { status: "failed", error: "Balance update failed. Please contact support." } },
    );
    // Release the gateway transaction claim when the balance write did not
    // complete. This keeps the manual UTR fallback usable instead of leaving
    // the payment permanently marked as consumed.
    await usedUtrs.deleteOne({ utr, ref });
    return;
  }

  await sessions.updateOne(
    { orderId: session.orderId, status: "paid" },
    { $set: { amountUsd, newBalance: updated.balance, ref } },
  );
}

export async function monitorDepositSession(orderId: string, merchantId: string): Promise<void> {
  if (activeSessionMonitors.has(orderId)) return;
  activeSessionMonitors.add(orderId);
  try {
    const sessions = await getDepositSessionsCollection();
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const session = await sessions.findOne({ orderId });
      if (!session || session.status !== "pending") return;
      if (session.expiresAt.getTime() <= Date.now()) {
        await sessions.updateOne({ orderId, status: "pending" }, { $set: { status: "expired" } });
        return;
      }

      const result = await verifyOrderWithGateway(orderId, merchantId);
      if (result.success) {
        const allowedDifference = session.amountInr * PAYMENT_AMOUNT_TOLERANCE;
        if (Math.abs(result.amount - session.amountInr) > allowedDifference) {
          await sessions.updateOne(
            { orderId, status: "pending" },
            {
              $set: {
                status: "failed",
                error: `Payment amount mismatch. Expected ₹${session.amountInr.toFixed(2)}, received ₹${result.amount.toFixed(2)}.`,
              },
            },
          );
          return;
        }
        await creditPaidSession(session, result.amount, result.utr, result.date);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 10_000));
    }
  } finally {
    activeSessionMonitors.delete(orderId);
  }
}

export async function getDepositSessionStatus(orderId: string): Promise<SessionStatusResult | null> {
  const sessions = await getDepositSessionsCollection();
  const session = await sessions.findOne({ orderId });
  if (!session) return null;
  return sessionResult(session);
}
