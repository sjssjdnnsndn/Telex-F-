import { MongoClient, type Collection, type Db } from "mongodb";
import { logger } from "./logger";

const MONGO_URL = process.env["MONGO_URL"];

if (!MONGO_URL) {
  throw new Error("MONGO_URL environment variable is required but was not provided.");
}

const client = new MongoClient(MONGO_URL);
let db: Db | null = null;
let indexesReady: Promise<void> | null = null;

// Shares the same database as the Telegram account-shop bot (python-bot/main.py)
// so a single balance store is used by both the bot and this mini app's API.
const DB_NAME = "telegram_bot";

export async function getDb(): Promise<Db> {
  if (db) return db;
  await client.connect();
  db = client.db(DB_NAME);
  logger.info({ db: DB_NAME }, "Connected to MongoDB");

  if (!indexesReady) {
    indexesReady = Promise.all([
      db
        .collection("used_utrs")
        .createIndex({ utr: 1 }, { unique: true })
        .catch((err) => {
          logger.error({ err }, "Failed to create unique index on used_utrs.utr");
        }),
      db
        .collection("device_fingerprints")
        .createIndex({ fingerprint: 1 }, { unique: true })
        .catch((err) => {
          logger.error({ err }, "Failed to create unique index on device_fingerprints.fingerprint");
        }),
    ]).then(() => undefined);
  }
  await indexesReady;

  return db;
}

export interface PaymentSettings {
  _id: "payment_settings";
  upiId: string;
  qrImageUrl: string;
  minDeposit: number;
}

/**
 * Matches the schema created by the Telegram bot (python-bot/main.py) for its
 * `users` collection: keyed by the Telegram user id (field `id`, not `_id`),
 * balance tracked in USD. This route only ever increments `balance`; it never
 * assumes any of the bot's other fields (referral stats, purchases, etc).
 */
export interface UserBalance {
  id: number; // telegramUserId
  balance: number;
  purchases?: number;
  loyalty_points?: number;
  device_verified?: boolean;
}

/** Global record of a claimed UTR — the unique index on `utr` is what makes
 * UTR reuse impossible across all users, not just within a single user. */
export interface UsedUtr {
  utr: string;
  telegramUserId: number;
  amountInr: number;
  amountUsd: number;
  ref: string;
  date: string;
  createdAt: Date;
}

export async function getPaymentSettingsCollection(): Promise<
  Collection<PaymentSettings>
> {
  const database = await getDb();
  return database.collection<PaymentSettings>("payment_settings");
}

export async function getUsersCollection(): Promise<Collection<UserBalance>> {
  const database = await getDb();
  return database.collection<UserBalance>("users");
}

export async function getUsedUtrsCollection(): Promise<Collection<UsedUtr>> {
  const database = await getDb();
  return database.collection<UsedUtr>("used_utrs");
}

/**
 * One row per device fingerprint, permanently bound to whichever Telegram
 * account first verified from that device. Used to stop a single person from
 * farming welcome bonuses / referral commissions by creating many Telegram
 * accounts on the same phone/browser (see verifyAndBindDevice below).
 */
export interface DeviceFingerprint {
  fingerprint: string;
  telegramUserId: number;
  createdAt: Date;
}

export async function getDeviceFingerprintsCollection(): Promise<
  Collection<DeviceFingerprint>
> {
  const database = await getDb();
  return database.collection<DeviceFingerprint>("device_fingerprints");
}

const DEFAULT_PAYMENT_SETTINGS: PaymentSettings = {
  _id: "payment_settings",
  upiId: "yourupi@bank",
  qrImageUrl: "https://i.ibb.co/B5vqn7NL/x.jpg",
  minDeposit: 1,
};

export async function getPaymentSettings(): Promise<PaymentSettings> {
  const col = await getPaymentSettingsCollection();
  const existing = await col.findOne({ _id: "payment_settings" });
  if (existing) return existing;
  await col.insertOne(DEFAULT_PAYMENT_SETTINGS);
  return DEFAULT_PAYMENT_SETTINGS;
}

export async function setPaymentSettings(
  update: Partial<Pick<PaymentSettings, "upiId" | "qrImageUrl" | "minDeposit">>,
): Promise<PaymentSettings> {
  const col = await getPaymentSettingsCollection();
  // MongoDB rejects an upsert whose $set and $setOnInsert touch the same
  // field, so only fall back to defaults for fields NOT present in `update`.
  const setOnInsert = Object.fromEntries(
    Object.entries(DEFAULT_PAYMENT_SETTINGS).filter(([key]) => !(key in update)),
  );
  await col.updateOne(
    { _id: "payment_settings" },
    { $set: update, $setOnInsert: setOnInsert },
    { upsert: true },
  );
  return getPaymentSettings();
}

export async function getOrCreateUser(
  telegramUserId: number,
): Promise<UserBalance> {
  const col = await getUsersCollection();
  const existing = await col.findOne({ id: telegramUserId });
  if (existing) return existing;

  // Matches the shape the bot's /start handler creates, so nothing the bot
  // relies on (referral stats, purchases, loyalty points) is left missing.
  const fresh: UserBalance = {
    id: telegramUserId,
    balance: 0,
    purchases: 0,
    loyalty_points: 0,
  };
  await col.insertOne(fresh);
  return fresh;
}

export type DeviceVerificationReason =
  | "new_device"
  | "already_verified"
  | "duplicate_device";

/**
 * Binds a device fingerprint to a Telegram account the first time it's seen,
 * or confirms it if the same account re-verifies from the same device.
 * Refuses (does not touch anything) if the fingerprint already belongs to a
 * DIFFERENT account — that's the multi-account referral abuse case.
 *
 * On success, also flags the user document with `deviceVerified: true` so
 * the bot (python-bot/main.py) can release any bonus it is holding for this
 * user pending verification.
 */
export async function verifyAndBindDevice(
  fingerprint: string,
  telegramUserId: number,
): Promise<{ verified: boolean; reason: DeviceVerificationReason }> {
  const col = await getDeviceFingerprintsCollection();

  // Atomic claim: this only ever inserts when no document with this
  // `fingerprint` exists yet, so two concurrent requests for a brand-new
  // fingerprint can't both "win" it — Mongo's unique index on `fingerprint`
  // guarantees exactly one insert succeeds and the other gets a duplicate
  // key error, which we treat as "go re-read who owns it now".
  let owner: number;
  let wasNewDevice: boolean;
  try {
    await col.insertOne({ fingerprint, telegramUserId, createdAt: new Date() });
    owner = telegramUserId;
    wasNewDevice = true;
  } catch (err) {
    const isDuplicateKey = (err as { code?: number }).code === 11000;
    if (!isDuplicateKey) throw err;
    const existing = await col.findOne({ fingerprint });
    // Should be unreachable (the doc that caused the conflict must exist),
    // but fail closed rather than crash if it somehow doesn't.
    if (!existing) return { verified: false, reason: "duplicate_device" };
    owner = existing.telegramUserId;
    wasNewDevice = false;
  }

  if (owner !== telegramUserId) {
    return { verified: false, reason: "duplicate_device" };
  }

  // Field name is snake_case to match the shared Mongo `users` schema owned
  // by python-bot/main.py (all its other user fields — referred_by,
  // loyalty_points, pending_welcome_bonus, etc — are snake_case), since
  // credit_pending_bonuses_loop() there polls this exact field.
  const usersCol = await getUsersCollection();
  await usersCol.updateOne(
    { id: telegramUserId },
    { $set: { device_verified: true } },
  );

  return { verified: true, reason: wasNewDevice ? "new_device" : "already_verified" };
}
