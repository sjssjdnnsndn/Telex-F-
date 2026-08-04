import crypto from "node:crypto";

const BOT_TOKEN = process.env["BOT_TOKEN"];

if (!BOT_TOKEN) {
  throw new Error("BOT_TOKEN environment variable is required but was not provided.");
}

export interface VerifiedTelegramUser {
  id: number;
}

/**
 * Verifies a Telegram WebApp `initData` string per Telegram's documented
 * scheme: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
 *
 * The secret key is HMAC-SHA256("WebAppData", botToken); the data-check
 * string (all fields except `hash`, sorted, "key=value" joined by "\n") is
 * then HMAC-SHA256'd with that secret and compared to the provided `hash`.
 *
 * Returns the verified Telegram user, or null if the signature is invalid,
 * missing, or expired (older than maxAgeSeconds).
 */
export function verifyTelegramInitData(
  initData: string | undefined,
  maxAgeSeconds = 24 * 60 * 60,
): VerifiedTelegramUser | null {
  if (!initData) return null;

  let params: URLSearchParams;
  try {
    params = new URLSearchParams(initData);
  } catch {
    return null;
  }

  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");

  const dataCheckString = Array.from(params.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");

  const secretKey = crypto.createHmac("sha256", "WebAppData").update(BOT_TOKEN as string).digest();
  const computedHash = crypto.createHmac("sha256", secretKey).update(dataCheckString).digest("hex");

  const providedBuf = Buffer.from(hash, "hex");
  const computedBuf = Buffer.from(computedHash, "hex");
  if (providedBuf.length !== computedBuf.length || !crypto.timingSafeEqual(providedBuf, computedBuf)) {
    return null;
  }

  const authDate = Number(params.get("auth_date"));
  if (!Number.isFinite(authDate) || Date.now() / 1000 - authDate > maxAgeSeconds) {
    return null;
  }

  const userRaw = params.get("user");
  if (!userRaw) return null;

  try {
    const user = JSON.parse(userRaw) as { id?: number };
    if (typeof user.id !== "number") return null;
    return { id: user.id };
  } catch {
    return null;
  }
}
