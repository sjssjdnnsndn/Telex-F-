import { Telegraf, Markup } from "telegraf";
import { logger } from "./logger";
import {
  getPaymentSettings,
  setPaymentSettings,
  getOrCreateUser,
} from "./mongo";

const BOT_TOKEN = process.env["BOT_TOKEN"];
const ADMIN_TELEGRAM_ID = process.env["ADMIN_TELEGRAM_ID"];

if (!BOT_TOKEN) {
  throw new Error("BOT_TOKEN environment variable is required but was not provided.");
}

if (!ADMIN_TELEGRAM_ID) {
  throw new Error(
    "ADMIN_TELEGRAM_ID environment variable is required but was not provided.",
  );
}

const adminId = Number(ADMIN_TELEGRAM_ID);

export const bot = new Telegraf(BOT_TOKEN);

/**
 * The mini app must be reachable at a stable HTTPS URL for Telegram to load it.
 * In production, use the published deployment domain (REPLIT_DOMAINS). During
 * development, fall back to the dev preview domain so the button still works
 * while iterating, but this should never be relied on for the live bot.
 */
function getMiniAppUrl(): string {
  const explicit = process.env["MINI_APP_URL"];
  if (explicit) return explicit;

  const domains = process.env["REPLIT_DOMAINS"];
  if (domains) {
    const first = domains.split(",")[0]?.trim();
    if (first) return `https://${first}/`;
  }

  const devDomain = process.env["REPLIT_DEV_DOMAIN"];
  if (devDomain) return `https://${devDomain}/`;

  throw new Error("Unable to determine mini app URL: set MINI_APP_URL.");
}

function isAdmin(userId: number | undefined): boolean {
  return userId === adminId;
}

bot.start(async (ctx) => {
  const name = ctx.from.first_name || "there";
  await ctx.reply(
    `Welcome, ${name}!\n\nUse the buttons below to check your balance or add funds via UPI.`,
    Markup.inlineKeyboard([
      [Markup.button.webApp("Deposit (UPI)", getMiniAppUrl())],
      [Markup.button.callback("Check Balance", "check_balance")],
    ]),
  );
});

bot.command("deposit", async (ctx) => {
  await ctx.reply(
    "Tap below to open the deposit page, scan the QR, pay, then paste your UTR to get credited instantly.",
    Markup.inlineKeyboard([
      [Markup.button.webApp("Open Deposit Page", getMiniAppUrl())],
    ]),
  );
});

bot.command("balance", async (ctx) => {
  const user = await getOrCreateUser(ctx.from.id);
  await ctx.reply(`Your current balance: ${user.balance.toFixed(2)}`);
});

bot.action("check_balance", async (ctx) => {
  const user = await getOrCreateUser(ctx.from.id);
  await ctx.answerCbQuery();
  await ctx.reply(`Your current balance: ${user.balance.toFixed(2)}`);
});

// ==================== ADMIN COMMANDS ====================

bot.command("setupi", async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;
  const upiId = ctx.message.text.split(" ").slice(1).join(" ").trim();
  if (!upiId) {
    await ctx.reply("Usage: /setupi yourupi@bank");
    return;
  }
  await setPaymentSettings({ upiId });
  await ctx.reply(`UPI ID updated to: ${upiId}`);
});

bot.command("setqr", async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;
  const url = ctx.message.text.split(" ").slice(1).join(" ").trim();
  if (!url) {
    await ctx.reply("Usage: /setqr <image_url>");
    return;
  }
  await setPaymentSettings({ qrImageUrl: url });
  await ctx.reply("QR code image updated.");
});

bot.command("setmindeposit", async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;
  const raw = ctx.message.text.split(" ")[1];
  const amount = Number(raw);
  if (!raw || !Number.isFinite(amount) || amount <= 0) {
    await ctx.reply("Usage: /setmindeposit 10");
    return;
  }
  await setPaymentSettings({ minDeposit: amount });
  await ctx.reply(`Minimum deposit updated to: ₹${amount}`);
});

bot.command("paymentinfo", async (ctx) => {
  if (!isAdmin(ctx.from.id)) return;
  const settings = await getPaymentSettings();
  await ctx.reply(
    `Current payment settings:\nUPI ID: ${settings.upiId}\nQR image: ${settings.qrImageUrl}\nMin deposit: ₹${settings.minDeposit}`,
  );
});

export async function notifyAdminOfDeposit(details: {
  telegramUserId: number;
  amount: number;
  newBalance: number;
  utr: string;
  ref: string;
  date: string;
}): Promise<void> {
  try {
    await bot.telegram.sendMessage(
      adminId,
      `New UPI Deposit\n\nUser ID: ${details.telegramUserId}\nAmount Paid: ₹${details.amount} INR\nNew Balance: ${details.newBalance.toFixed(2)} USD\nUTR: ${details.utr}\nRef: ${details.ref}\nDate: ${details.date}`,
    );
  } catch (err) {
    logger.error({ err }, "Failed to notify admin of deposit");
  }
}

/**
 * Sends the caller instant in-chat feedback right after they run the device
 * verification flow in the mini app. This only confirms device identity — the
 * bot (python-bot/main.py) is the source of truth for crediting any bonus
 * that was pending on verification, and sends its own message when it does.
 */
export async function notifyUserOfDeviceVerification(
  telegramUserId: number,
  verified: boolean,
): Promise<void> {
  try {
    await bot.telegram.sendMessage(
      telegramUserId,
      verified
        ? "✅ Device verified successfully."
        : "❌ Verification failed: this device is already linked to another account. You can't create multiple accounts on the same device.",
    );
  } catch (err) {
    logger.error({ err, telegramUserId }, "Failed to notify user of device verification result");
  }
}

/**
 * The Telegram account-shop bot (python-bot/main.py) is the bot that actually
 * polls Telegram for updates and owns /start, /deposit, etc — it embeds the
 * "Open UPI Deposit" WebApp button that launches this mini app. This Node
 * Telegraf client is intentionally never launched (no `bot.launch()`): a
 * single BOT_TOKEN can only have one active long-polling consumer, and
 * running two here would cause the two processes to fight over updates
 * (Telegram responds with a 409 Conflict). This client exists purely to send
 * admin notifications via `bot.telegram.sendMessage`, which works over the
 * plain Bot API without needing to own the update stream.
 */
export function startBot(): void {
  logger.info(
    "Telegram bot API client ready (not polling — python-bot/main.py owns polling for this token)",
  );
}
