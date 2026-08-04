import { Router, type IRouter } from "express";
import {
  GetPaymentInfoResponse,
  GetUpiBalanceParams,
  GetUpiBalanceResponse,
  VerifyUtrBody,
  VerifyUtrResponse,
  CheckAdminResponse,
  UpdatePaymentInfoBody,
  UpdatePaymentInfoResponse,
  VerifyDeviceBody,
  VerifyDeviceResponse,
  CreateDepositSessionBody,
  CreateDepositSessionResponse,
  GetDepositSessionParams,
  GetDepositSessionResponse,
} from "@workspace/api-zod";
import {
  getPaymentSettings,
  setPaymentSettings,
  getOrCreateUser,
  verifyAndBindDevice,
  getConfiguredMerchantId,
  getConfiguredUpiId,
  getDepositSessionsCollection,
  getDepositSession,
} from "../lib/mongo";
import { notifyUserOfDeviceVerification } from "../lib/bot";
import {
  verifyAndCredit,
  monitorDepositSession,
  getDepositSessionStatus,
} from "../lib/depositLogic";
import { notifyAdminOfDeposit } from "../lib/bot";
import { verifyTelegramInitData } from "../lib/telegramAuth";

const router: IRouter = Router();

const ADMIN_TELEGRAM_ID = process.env["ADMIN_TELEGRAM_ID"];
if (!ADMIN_TELEGRAM_ID) {
  throw new Error("ADMIN_TELEGRAM_ID environment variable is required but was not provided.");
}

function isAdminId(id: number): boolean {
  return String(id) === ADMIN_TELEGRAM_ID;
}

/**
 * Every UPI route that reads or mutates a user's balance must know WHO the
 * caller is from a source the caller cannot forge. We never trust a
 * client-supplied telegramUserId on its own — it must match the id embedded
 * in a Telegram-signed `initData` string (see telegramAuth.ts), sent by the
 * mini app on every request as the `x-telegram-init-data` header. Without
 * this, anyone could read or credit any other user's balance just by
 * guessing/enumerating ids.
 */
function requireVerifiedTelegramUser(
  req: { headers: Record<string, unknown> },
  res: { status: (code: number) => { json: (body: unknown) => void } },
): number | null {
  const initData = req.headers["x-telegram-init-data"];
  const verified = verifyTelegramInitData(typeof initData === "string" ? initData : undefined);
  if (!verified) {
    res.status(401).json({ error: "Missing or invalid Telegram authentication. Please reopen this app from the bot." });
    return null;
  }
  return verified.id;
}

router.get("/upi/payment-info", async (_req, res): Promise<void> => {
  const settings = await getPaymentSettings();
  const configuredUpiId = await getConfiguredUpiId();
  res.json(
    GetPaymentInfoResponse.parse({
      upiId: configuredUpiId,
      qrImageUrl: settings.qrImageUrl,
      minDeposit: settings.minDeposit,
    }),
  );
});

router.get("/upi/balance/:telegramUserId", async (req, res): Promise<void> => {
  const params = GetUpiBalanceParams.safeParse(req.params);
  if (!params.success) {
    res.status(400).json({ error: params.error.message });
    return;
  }

  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;
  if (verifiedUserId !== params.data.telegramUserId) {
    res.status(403).json({ error: "You can only view your own balance." });
    return;
  }

  const user = await getOrCreateUser(params.data.telegramUserId);
  res.json(
    GetUpiBalanceResponse.parse({
      telegramUserId: params.data.telegramUserId,
      balance: user.balance,
    }),
  );
});

router.get("/upi/admin/check", async (req, res): Promise<void> => {
  const initData = req.headers["x-telegram-init-data"];
  const verified = verifyTelegramInitData(typeof initData === "string" ? initData : undefined);
  res.json(CheckAdminResponse.parse({ isAdmin: verified !== null && isAdminId(verified.id) }));
});

router.put("/upi/admin/payment-info", async (req, res): Promise<void> => {
  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;
  if (!isAdminId(verifiedUserId)) {
    res.status(403).json({ error: "Only the bot admin can update payment settings." });
    return;
  }

  const parsed = UpdatePaymentInfoBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const settings = await setPaymentSettings(parsed.data);
  req.log.info({ adminId: verifiedUserId }, "Admin updated UPI payment settings");
  res.json(
    UpdatePaymentInfoResponse.parse({
      upiId: settings.upiId,
      qrImageUrl: settings.qrImageUrl,
      minDeposit: settings.minDeposit,
    }),
  );
});

router.post("/upi/verify-device", async (req, res): Promise<void> => {
  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;

  const parsed = VerifyDeviceBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const result = await verifyAndBindDevice(parsed.data.fingerprint, verifiedUserId);
  req.log.info(
    { telegramUserId: verifiedUserId, verified: result.verified, reason: result.reason },
    "Device verification attempt",
  );

  await notifyUserOfDeviceVerification(verifiedUserId, result.verified);

  res.json(VerifyDeviceResponse.parse(result));
});

router.post("/upi/verify", async (req, res): Promise<void> => {
  const parsed = VerifyUtrBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;
  if (verifiedUserId !== parsed.data.telegramUserId) {
    res.status(403).json({ error: "You can only credit your own balance." });
    return;
  }

  const result = await verifyAndCredit(parsed.data.telegramUserId, parsed.data.utr);

  if (!result.ok) {
    req.log.warn({ telegramUserId: parsed.data.telegramUserId }, "UTR verification failed");
    res.status(400).json({ error: result.error });
    return;
  }

  req.log.info(
    { telegramUserId: parsed.data.telegramUserId, amount: result.amount },
    "UPI deposit credited",
  );

  await notifyAdminOfDeposit({
    telegramUserId: parsed.data.telegramUserId,
    amount: result.amount,
    newBalance: result.newBalance,
    utr: result.utr,
    ref: result.ref,
    date: result.date,
  });

  res.json(
    VerifyUtrResponse.parse({
      success: true,
      amount: result.amount,
      oldBalance: result.oldBalance,
      newBalance: result.newBalance,
      utr: result.utr,
      ref: result.ref,
      date: result.date,
    }),
  );
});

router.post("/upi/deposit-session", async (req, res): Promise<void> => {
  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;

  const parsed = CreateDepositSessionBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }

  const settings = await getPaymentSettings();
  const upiId = await getConfiguredUpiId();
  const merchantId = await getConfiguredMerchantId();
  if (!merchantId || !upiId || upiId === "yourupi@bank") {
    res.status(400).json({
      error: "Automatic UPI verification is not configured. Admin must set the UPI ID and MID first.",
    });
    return;
  }
  if (parsed.data.amountInr < settings.minDeposit) {
    res.status(400).json({ error: `Minimum deposit is ₹${settings.minDeposit}.` });
    return;
  }

  const orderId = `UPI${verifiedUserId}${Date.now()}`;
  const amountInr = Number(parsed.data.amountInr.toFixed(2));
  const amountUsd = amountInr / Number(process.env["USD_TO_INR_RATE"] ?? "96");
  const params = new URLSearchParams({
    pa: upiId,
    pn: "TelegramBot",
    am: amountInr.toFixed(2),
    cu: "INR",
    tn: orderId,
    tr: orderId,
  });
  const upiLink = `upi://pay?${params.toString()}`;
  const qrImageUrl = `https://quickchart.io/qr?size=420&margin=2&text=${encodeURIComponent(upiLink)}`;
  const expiresAt = new Date(Date.now() + 10 * 60 * 1000);
  const sessions = await getDepositSessionsCollection();
  await sessions.insertOne({
    orderId,
    telegramUserId: verifiedUserId,
    amountInr,
    amountUsd,
    upiId,
    upiLink,
    status: "pending",
    createdAt: new Date(),
    expiresAt,
  });

  // The API process owns this monitor. It uses the merchant id server-side and
  // never exposes it to the Telegram WebApp.
  void monitorDepositSession(orderId, merchantId);

  res.status(201).json(
    CreateDepositSessionResponse.parse({
      orderId,
      amountInr,
      amountUsd,
      upiId,
      upiLink,
      qrImageUrl,
      status: "pending",
      expiresAt: expiresAt.toISOString(),
    }),
  );
});

router.get("/upi/deposit-session/:orderId", async (req, res): Promise<void> => {
  const verifiedUserId = requireVerifiedTelegramUser(req, res);
  if (verifiedUserId === null) return;

  const parsed = GetDepositSessionParams.safeParse(req.params);
  if (!parsed.success) {
    res.status(400).json({ error: parsed.error.message });
    return;
  }
  const session = await getDepositSession(parsed.data.orderId);
  if (!session) {
    res.status(404).json({ error: "Deposit session not found." });
    return;
  }
  if (session.telegramUserId !== verifiedUserId) {
    res.status(403).json({ error: "You can only view your own deposit session." });
    return;
  }

  const status = await getDepositSessionStatus(session.orderId);
  if (!status) {
    res.status(404).json({ error: "Deposit session not found." });
    return;
  }
  if (status.status === "pending") {
    const merchantId = await getConfiguredMerchantId();
    if (merchantId) {
      void monitorDepositSession(session.orderId, merchantId);
    }
  }
  res.json(GetDepositSessionResponse.parse(status));
});

export default router;
