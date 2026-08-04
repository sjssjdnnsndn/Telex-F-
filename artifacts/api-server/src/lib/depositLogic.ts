import { getOrCreateUser, getUsersCollection, getUsedUtrsCollection } from "./mongo";
import { verifyUtrWithGateway } from "./upiVerify";

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

// Matches the bot's own conversion rate (python-bot/main.py: USD_TO_INR_RATE)
// so amounts credited here line up with the bot's USD-denominated balance.
const USD_TO_INR_RATE = Number(process.env["USD_TO_INR_RATE"] ?? "96.0");

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
