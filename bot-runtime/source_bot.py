#!/usr/bin/env python3
import os
import logging
import asyncio
from typing import Optional, Tuple, Dict
import time
import aiohttp
import json
from datetime import datetime
import qrcode
from io import BytesIO
import random
import string
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))
from health_server import start_health_server, stop_health_server

from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

import zipfile
import tempfile

from motor.motor_asyncio import AsyncIOMotorClient

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneNumberInvalidError, FloodWaitError
from telethon.network import ConnectionTcpAbridged
from telethon.tl.types import InputPeerUser

# Credentials are provided via Replit Secrets / environment variables only.
API_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
TELETHON_API_ID = int(os.getenv("TELETHON_API_ID", "0")) or None
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH") or None
USD_TO_INR_RATE = float(os.getenv("USD_TO_INR_RATE", "96.0"))


def _resolve_mini_app_url() -> str:
    """Resolve the stable HTTPS URL for the UPI deposit mini app.

    The published URL is the safe default. Replit's REPLIT_DOMAINS and
    REPLIT_DEV_DOMAIN variables can point at an internal *.replit.dev host in
    the workspace, which Telegram users cannot reliably resolve.
    """
    explicit = os.getenv("MINI_APP_URL")
    if explicit:
        return explicit
    return "https://telegram-bot-deploy--matraca.replit.app/"


MINI_APP_URL = _resolve_mini_app_url()

SAVED_MESSAGES_WATERMARK = "This Account is Sold by This Bot : @TeleMartxBot"
SAVED_MESSAGES_DELETE_BATCH_SIZE = 100


def _verify_device_mini_app_url() -> str:
    """URL for the mini app's device-verification screen (see artifacts/upi-deposit/src/pages/Verify.tsx)."""
    base = MINI_APP_URL
    return base if base.endswith("/verify") else base.rstrip("/") + "/verify"


VERIFY_DEVICE_MINI_APP_URL = _verify_device_mini_app_url()

# ==================== UPI CONFIGURATION ====================
UPI_TRACKING_FILE = "transaction/upi_tracking.json"
UPI_VERIFICATION_DELAY = 10
UPI_VERIFICATION_TIMEOUT = 600
# SECURITY: Payment amount tolerance (1% allowed difference for currency conversion rounding)
PAYMENT_AMOUNT_TOLERANCE = 0.01

MIN_DEPOSIT_USD = 0.1
MIN_DEPOSIT_INR = 1.0

# ==================== REFERRAL SYSTEM CONFIGURATION ====================
DEFAULT_WELCOME_BONUS = 1.0
DEFAULT_REFERRAL_COMMISSION_RATE = 10.0

bot_instance = None

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Telegram's httpx request logger includes the bot API URL, which contains the
# secret bot token. Keep request details out of workflow/deployment logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ==================== COUNTRY DETECTION ====================
# Sorted longest-prefix-first so +380 (Ukraine) matches before +3, etc.
PHONE_PREFIX_MAP = sorted([
    ("+1",   "🇺🇸 USA"),
    ("+7",   "🇷🇺 RUS"),
    ("+20",  "🇪🇬 EGY"),
    ("+27",  "🇿🇦 ZAF"),
    ("+31",  "🇳🇱 NLD"),
    ("+33",  "🇫🇷 FRA"),
    ("+34",  "🇪🇸 ESP"),
    ("+39",  "🇮🇹 ITA"),
    ("+44",  "🇬🇧 UK"),
    ("+49",  "🇩🇪 GER"),
    ("+52",  "🇲🇽 MEX"),
    ("+55",  "🇧🇷 BRA"),
    ("+61",  "🇦🇺 AUS"),
    ("+62",  "🇮🇩 IDN"),
    ("+63",  "🇵🇭 PHL"),
    ("+81",  "🇯🇵 JPN"),
    ("+82",  "🇰🇷 KOR"),
    ("+86",  "🇨🇳 CHN"),
    ("+90",  "🇹🇷 TUR"),
    ("+91",  "🇮🇳 IND"),
    ("+92",  "🇵🇰 PAK"),
    ("+98",  "🇮🇷 IRN"),
    ("+234", "🇳🇬 NGA"),
    ("+254", "🇰🇪 KEN"),
    ("+380", "🇺🇦 UKR"),
    ("+880", "🇧🇩 BAN"),
    ("+966", "🇸🇦 KSA"),
    ("+971", "🇦🇪 UAE"),
], key=lambda x: -len(x[0]))

def detect_country_from_phone(phone: str) -> str:
    """Return a country label like '🇺🇸 USA' or '🌐 Unknown' from a phone number."""
    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone
    for prefix, country in PHONE_PREFIX_MAP:
        if phone.startswith(prefix):
            return country
    return "🌐 Unknown"

# MongoDB connection
MONGO_URL = os.environ["MONGO_URL"]
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client.telegram_bot

# Global variables
SUPPORT_CHAT: Dict[int, bool] = {}
LAST_INLINE_MSG: Dict[int, int] = {}  # user_id -> message_id of last inline-keyboard bot message
OTP_WATCHERS: Dict[int, Tuple[TelegramClient, object]] = {}
BULK_OTP_WATCHERS: Dict[str, Tuple[TelegramClient, object]] = {}

# Simple state storage
USER_STATES = {}
USER_DATA = {}

# ==================== SECURITY HELPER FUNCTIONS ====================
def verify_payment_amount(expected_amount: float, actual_amount: float, tolerance: float = PAYMENT_AMOUNT_TOLERANCE) -> tuple:
    """
    Verify that the actual paid amount matches the expected amount within tolerance.
    
    Args:
        expected_amount: The amount that was supposed to be paid
        actual_amount: The amount that was actually paid
        tolerance: Allowed percentage difference (default 1%)
    
    Returns:
        tuple: (is_valid, verified_amount)
    """
    if actual_amount <= 0:
        logger.error(f"❌ Invalid actual amount: {actual_amount}")
        return False, 0
    
    difference = abs(expected_amount - actual_amount)
    allowed_difference = expected_amount * tolerance
    
    is_valid = difference <= allowed_difference
    
    if not is_valid:
        logger.warning(
            f"⚠️ Payment amount mismatch! Expected: {expected_amount}, "
            f"Actual: {actual_amount}, Difference: {difference}"
        )
    
    return is_valid, actual_amount

def extract_amount_from_paytm_response(paytm_data: dict) -> Optional[float]:
    """
    Extract the actual paid amount from Paytm API response.
    
    Args:
        paytm_data: Response data from Paytm API
    
    Returns:
        float: Actual paid amount in INR, or None if not found
    """
    try:
        # Paytm returns amount in "TXNAMOUNT" field
        if "TXNAMOUNT" in paytm_data:
            return float(paytm_data["TXNAMOUNT"])
        elif "amount" in paytm_data:
            return float(paytm_data["amount"])
        else:
            logger.error(f"❌ No amount field found in Paytm response: {paytm_data}")
            return None
    except (ValueError, TypeError) as e:
        logger.error(f"❌ Error extracting amount from Paytm response: {e}")
        return None

# ==================== REFERRAL SYSTEM HELPER FUNCTIONS ====================
def generate_referral_code(user_id: int) -> str:
    """Generate unique random alphanumeric referral code"""
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    return f"REF{random_part}"

async def ensure_referral_code(user_id: int) -> str:
    """Ensure user has a referral code, create if doesn't exist"""
    user = await db.users.find_one({"id": user_id})

    if user and user.get("referral_code"):
        return user["referral_code"]

    while True:
        ref_code = generate_referral_code(user_id)
        existing = await db.users.find_one({"referral_code": ref_code})
        if not existing:
            break

    await db.users.update_one(
        {"id": user_id},
        {
            "$set": {
                "referral_code": ref_code,
                "referral_stats": {
                    "total_referred": 0,
                    "completed_purchases": 0,
                    "commission_earned": 0.0
                }
            }
        },
        upsert=True
    )

    return ref_code

async def get_welcome_bonus() -> float:
    """Get welcome bonus amount from config (in USD)"""
    config = await db.config.find_one({"key": "welcome_bonus"})
    if config and config.get("value"):
        return float(config["value"])
    return DEFAULT_WELCOME_BONUS

async def get_referral_commission_rate() -> float:
    """Get referral commission rate from config (as percentage)"""
    config = await db.config.find_one({"key": "referral_commission_rate"})
    if config and config.get("value"):
        return float(config["value"])
    return DEFAULT_REFERRAL_COMMISSION_RATE

# ==================== DEFAULT PRICES (per country + year) ====================

async def get_default_price(country: str, year: str) -> Optional[float]:
    """Return the admin-configured default price (USD) for a country+year, or None."""
    doc = await db.default_prices.find_one({"country": country, "year": str(year)})
    return float(doc["price_usd"]) if doc else None

async def set_default_price(country: str, year: str, price: float) -> None:
    """Upsert the default price for a country+year pair."""
    await db.default_prices.update_one(
        {"country": country, "year": str(year)},
        {"$set": {"price_usd": price, "updated_at": datetime.now()}},
        upsert=True,
    )

async def get_all_default_prices() -> list:
    """Return all configured default prices sorted by country then year."""
    return await db.default_prices.find().sort([("country", 1), ("year", 1)]).to_list(1000)

# ==================== DEFAULT 2FA PASSWORD ====================
DEFAULT_2FA_PASSWORD = os.getenv("DEFAULT_2FA_PASSWORD")
if not DEFAULT_2FA_PASSWORD:
    raise RuntimeError(
        "DEFAULT_2FA_PASSWORD environment variable is required; "
        "configure it in Replit Secrets."
    )

async def get_default_2fa_password() -> str:
    """Return the admin-configured default 2FA password, falling back to the built-in default."""
    doc = await db.config.find_one({"key": "default_2fa_password"})
    return doc["value"] if doc and doc.get("value") else DEFAULT_2FA_PASSWORD

async def set_default_2fa_password(password: str) -> None:
    """Persist the default 2FA password in config."""
    await db.config.update_one(
        {"key": "default_2fa_password"},
        {"$set": {"value": password, "updated_at": datetime.now()}},
        upsert=True,
    )

async def award_welcome_bonus(user_id: int) -> float:
    """Award welcome bonus to new user"""
    bonus_amount = await get_welcome_bonus()

    await db.users.update_one(
        {"id": user_id},
        {
            "$inc": {"balance": bonus_amount}
        }
    )

    await db.transactions.insert_one({
        "user_id": user_id,
        "type": "welcome_bonus",
        "amount": bonus_amount,
        "timestamp": datetime.now(),
        "status": "completed"
    })

    return bonus_amount

async def credit_pending_bonuses_loop(bot):
    """Background loop: releases welcome bonuses that were held pending
    device verification (see `start()`'s referral branch). Runs in-process
    alongside the bot's polling loop so no separate service/webhook is
    needed — the mini app's /upi/verify-device endpoint only flips
    `device_verified` to True on the shared Mongo `users` doc; this loop is
    what actually moves the money once that flag appears.
    """
    while True:
        try:
            cursor = db.users.find({
                "device_verified": True,
                "pending_welcome_bonus": {"$gt": 0},
            })
            async for user in cursor:
                user_id = user["id"]
                bonus = user["pending_welcome_bonus"]
                referrer_id = user.get("pending_referrer_id") or user.get("referred_by")

                # Atomic claim: only credit if this doc still has the pending
                # bonus we just read (matched_count/modified_count == 0 means
                # another loop tick/process already claimed it first). This
                # is what makes the credit step safe even if this loop is
                # ever run from more than one process.
                claim = await db.users.update_one(
                    {"id": user_id, "device_verified": True, "pending_welcome_bonus": bonus},
                    {
                        "$inc": {"balance": bonus},
                        "$set": {"pending_welcome_bonus": 0, "pending_referrer_id": None},
                    }
                )
                if claim.modified_count == 0:
                    continue

                await db.transactions.insert_one({
                    "user_id": user_id,
                    "type": "welcome_bonus",
                    "amount": bonus,
                    "timestamp": datetime.now(),
                    "status": "completed"
                })

                if referrer_id:
                    await db.users.update_one(
                        {"id": referrer_id},
                        {"$inc": {"referral_stats.total_referred": 1}}
                    )
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 <b>New Referral!</b>\n\n"
                            f"👤 User ID: <code>{user_id}</code>\n"
                            f"💰 They received: <b>{format_balance_dual_currency(bonus)}</b> welcome bonus\n"
                            f"📊 You'll earn commission on ALL their purchases!\n\n"
                            f"Keep sharing your referral link! 🚀",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.error(f"Error notifying referrer {referrer_id}: {e}")

                try:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Welcome Bonus Credited!</b>\n\n"
                        f"💰 <b>{format_balance_dual_currency(bonus)}</b> has been added to your balance.\n\n"
                        f"👇 Here's the main menu:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_keyboard()
                    )
                except Exception as e:
                    logger.error(f"Error notifying user {user_id} of credited bonus: {e}")
        except Exception as e:
            logger.error(f"Error in credit_pending_bonuses_loop: {e}")

        await asyncio.sleep(8)

async def award_referral_commission(referrer_id: int, referee_id: int, purchase_amount: float):
    """Award commission to referrer on referee's purchase"""
    commission_rate = await get_referral_commission_rate()
    commission = purchase_amount * (commission_rate / 100)

    # Credit commission to referrer's balance
    await db.users.update_one(
        {"id": referrer_id},
        {
            "$inc": {
                "balance": commission,
                "referral_stats.commission_earned": commission,
                "referral_stats.completed_purchases": 1
            }
        }
    )

    # Record transaction
    await db.transactions.insert_one({
        "user_id": referrer_id,
        "type": "referral_commission",
        "amount": commission,
        "referee_id": referee_id,
        "purchase_amount": purchase_amount,
        "commission_rate": commission_rate,
        "timestamp": datetime.now(),
        "status": "completed"
    })

    # Notify referrer
    try:
        if bot_instance:
            referrer = await db.users.find_one({"id": referrer_id})
            new_balance = referrer.get("balance", 0)

            await bot_instance.send_message(
                referrer_id,
                f"🎉 <b>Referral Commission Earned!</b>\n\n"
                f"💰 Commission ({commission_rate}%): <code>${commission:.2f} USD</code> (₹{commission * USD_TO_INR_RATE:.2f} INR)\n"
                f"📊 From: User ID {referee_id}\n"
                f"💵 Purchase Amount: <code>${purchase_amount:.2f} USD</code>\n"
                f"💵 Your New Balance: <b>{format_balance_dual_currency(new_balance)}</b>\n\n"
                f"Keep sharing your referral link to earn more! 🚀",
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Error notifying referrer: {e}")

async def get_referral_stats(user_id: int) -> dict:
    """Get referral statistics for user"""
    user = await db.users.find_one({"id": user_id})

    if not user:
        return {
            "total_referred": 0,
            "completed_purchases": 0,
            "commission_earned": 0.0
        }

    return user.get("referral_stats", {
        "total_referred": 0,
        "completed_purchases": 0,
        "commission_earned": 0.0
    })

# ==================== UPI HELPER FUNCTIONS ====================
async def fetch_paytm_data(order_id,mid):
    url = f"https://paytm-api.lightdns.me/?mid={mid}&oid={order_id}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.text()
                data = json.loads(data)
                if data.get("STATUS") == "TXN_SUCCESS":
                    return True, data
                else:
                    return False, data
            else:
                return False, None

def load_upi_tracking():
    """Load UPI payment tracking data"""
    try:
        if os.path.exists(UPI_TRACKING_FILE):
            with open(UPI_TRACKING_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading UPI tracking: {e}")
    return {"pending": {}, "completed": {}}

def save_upi_tracking(data):
    """Save UPI payment tracking data"""
    try:
        os.makedirs("transaction", exist_ok=True)
        with open(UPI_TRACKING_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Error saving UPI tracking: {e}")
        return False

def generate_upi_qr(upi_id: str, amount_inr: float, order_id: str, name: str = "TelegramBot") -> BytesIO:
    """Generate UPI QR code with deeplink"""
    upi_link = f"upi://pay?pa={upi_id}&pn={name}&am={amount_inr:.2f}&cu=INR&tn={order_id}&tr={order_id}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_link)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    bio = BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)

    return bio

async def verify_upi_payment_auto(order_id: str, mid: str, user_id: int, expected_amount_usd: float, expected_amount_inr: float) -> bool:
    """
    Automatically verify UPI payment with SECURITY FIX - verifies actual paid amount.
    
    SECURITY CHANGES:
    - Now accepts both expected_amount_usd and expected_amount_inr
    - Verifies the actual paid amount from Paytm API response
    - Only credits the actual amount paid (or rejects if mismatch)
    - Logs all payment verification attempts for audit trail
    """
    start_time = time.time()
    check_count = 0

    from telegram.ext import Application
    application = Application.builder().token(API_TOKEN).build()

    while (time.time() - start_time) < UPI_VERIFICATION_TIMEOUT:
        check_count += 1
        logger.info(f"🔍 Checking UPI payment status for order {order_id} (attempt {check_count})")

        try:
            success, data = await fetch_paytm_data(order_id, mid)

            if success and data:
                # ===== SECURITY FIX: VERIFY ACTUAL PAID AMOUNT =====
                actual_paid_inr = extract_amount_from_paytm_response(data)
                
                if actual_paid_inr is None:
                    logger.error(f"❌ Could not extract amount from Paytm response for order {order_id}")
                    await asyncio.sleep(UPI_VERIFICATION_DELAY)
                    continue
                
                # Verify the amount matches expected amount
                is_valid, verified_amount_inr = verify_payment_amount(expected_amount_inr, actual_paid_inr)
                
                if not is_valid:
                    # CRITICAL SECURITY: Amount mismatch detected!
                    logger.error(
                        f"🚨 PAYMENT FRAUD ATTEMPT DETECTED! 🚨\n"
                        f"User ID: {user_id}\n"
                        f"Order ID: {order_id}\n"
                        f"Expected: ₹{expected_amount_inr:.2f} INR\n"
                        f"Actually Paid: ₹{actual_paid_inr:.2f} INR\n"
                        f"Transaction Details: {data}"
                    )
                    
                    # Notify admin about fraud attempt
                    try:
                        async with application:
                            await application.bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>PAYMENT FRAUD ATTEMPT DETECTED!</b> 🚨\n\n"
                                f"👤 User ID: <code>{user_id}</code>\n"
                                f"📋 Order ID: <code>{order_id}</code>\n"
                                f"💰 Expected: <code>₹{expected_amount_inr:.2f} INR</code>\n"
                                f"💸 Actually Paid: <code>₹{actual_paid_inr:.2f} INR</code>\n"
                                f"⚠️ Difference: <code>₹{expected_amount_inr - actual_paid_inr:.2f}</code>\n\n"
                                f"Action: Payment REJECTED. User NOT credited.",
                                parse_mode=ParseMode.HTML
                            )
                    except Exception as e:
                        logger.error(f"Error notifying admin about fraud: {e}")
                    
                    # Notify user about failed payment
                    try:
                        async with application:
                            await application.bot.send_message(
                                user_id,
                                f"❌ <b>Payment Verification Failed</b>\n\n"
                                f"📋 Order ID: <code>{order_id}</code>\n"
                                f"⚠️ Payment amount mismatch detected.\n\n"
                                f"Expected: <code>₹{expected_amount_inr:.2f} INR</code>\n"
                                f"Received: <code>₹{actual_paid_inr:.2f} INR</code>\n\n"
                                f"Please contact support if you believe this is an error.",
                                parse_mode=ParseMode.HTML
                            )
                    except Exception as e:
                        logger.error(f"Error notifying user about failed payment: {e}")
                    
                    # Mark as failed in tracking
                    tracking_data = load_upi_tracking()
                    if order_id in tracking_data["pending"]:
                        tracking_data["pending"][order_id]["status"] = "fraud_detected"
                        tracking_data["pending"][order_id]["expected_amount"] = expected_amount_inr
                        tracking_data["pending"][order_id]["actual_amount"] = actual_paid_inr
                        tracking_data["pending"][order_id]["fraud_detected_at"] = datetime.now().isoformat()
                        save_upi_tracking(tracking_data)
                    
                    return False
                
                # ===== AMOUNT VERIFIED - PROCEED WITH CREDITING =====
                # Convert the ACTUAL paid amount to USD
                actual_amount_usd = verified_amount_inr / USD_TO_INR_RATE
                
                logger.info(
                    f"✅ UPI Payment verified and confirmed for order {order_id}\n"
                    f"User: {user_id}, Amount: ₹{verified_amount_inr:.2f} INR (${actual_amount_usd:.2f} USD)"
                )

                # Update user balance with ACTUAL paid amount
                user = await db.users.find_one({"id": user_id})
                if user:
                    new_balance = user.get("balance", 0) + actual_amount_usd
                    await db.users.update_one(
                        {"id": user_id},
                        {"$set": {"balance": new_balance}}
                    )
                    
                # Record transaction with ACTUAL amounts
                await db.transactions.insert_one({
                    "user_id": user_id,
                    "type": "upi_deposit",
                    "amount": actual_amount_usd,
                    "amount_inr": verified_amount_inr,
                    "expected_amount_usd": expected_amount_usd,
                    "expected_amount_inr": expected_amount_inr,
                    "order_id": order_id,
                    "txn_id": data.get("TXNID"),
                    "bank_txn_id": data.get("BANKTXNID"),
                    "timestamp": datetime.now(),
                    "status": "completed",
                    "paytm_data": data,
                    "security_verified": True
                })

                # REFERRAL COMMISSION: Award commission based on ACTUAL amount
                if user and user.get("referred_by"):
                    referrer_id = user["referred_by"]
                    await award_referral_commission(referrer_id, user_id, actual_amount_usd)

                # Update tracking
                tracking_data = load_upi_tracking()
                if order_id in tracking_data["pending"]:
                    payment = tracking_data["pending"][order_id]
                    payment["status"] = "completed"
                    payment["completed_at"] = datetime.now().isoformat()
                    payment["txn_data"] = data
                    payment["actual_paid_inr"] = verified_amount_inr
                    payment["actual_paid_usd"] = actual_amount_usd
                    tracking_data["completed"][order_id] = payment
                    del tracking_data["pending"][order_id]
                    save_upi_tracking(tracking_data)

                # Get user info for admin notification
                try:
                    async with application:
                        user_info = await application.bot.get_chat(user_id)
                        username = user_info.username if user_info.username else user_info.first_name
                except:
                    username = "Unknown"

                # Notify user
                try:
                    async with application:
                        await application.bot.send_message(
                            user_id,
                            f"✅ <b>UPI Payment Confirmed!</b>\n\n"
                            f"💰 Amount: <code>₹{verified_amount_inr:.2f} INR</code> (${actual_amount_usd:.2f} USD)\n"
                            f"📋 Order ID: <code>{order_id}</code>\n"
                            f"📖 Transaction ID: <code>{data.get('TXNID')}</code>\n"
                            f"💵 New Balance: <b>{format_balance_dual_currency(new_balance)}</b>\n\n"
                            f"Thank you for your deposit! 🎉",
                            parse_mode=ParseMode.HTML
                        )
                except Exception as e:
                    logger.error(f"Error notifying user: {e}")

                # Notify admin
                try:
                    async with application:
                        await application.bot.send_message(
                            ADMIN_ID,
                            f"✅ <b>New UPI Payment Received!</b>\n\n"
                            f"👤 User: <code>@{username}</code>\n"
                            f"🆔 User ID: <code>{user_id}</code>\n"
                            f"💰 Amount: <code>₹{verified_amount_inr:.2f} INR</code> (${actual_amount_usd:.2f} USD)\n"
                            f"📋 Order ID: <code>{order_id}</code>\n"
                            f"📖 Transaction ID: <code>{data.get('TXNID')}</code>\n"
                            f"🏦 Bank TXN ID: <code>{data.get('BANKTXNID')}</code>\n"
                            f"💵 New User Balance: <b>{format_balance_dual_currency(new_balance)}</b>\n"
                            f"✅ Security: Amount verified",
                            parse_mode=ParseMode.HTML
                        )
                except Exception as e:
                    logger.error(f"Error notifying admin: {e}")

                return True

        except Exception as e:
            logger.error(f"Error checking payment status: {e}")

        await asyncio.sleep(UPI_VERIFICATION_DELAY)

    # Timeout reached
    logger.warning(f"⏰ UPI payment verification timeout for order {order_id}")

    tracking_data = load_upi_tracking()
    if order_id in tracking_data["pending"]:
        tracking_data["pending"][order_id]["status"] = "timeout"
        tracking_data["pending"][order_id]["timeout_at"] = datetime.now().isoformat()
        save_upi_tracking(tracking_data)

    try:
        async with application:
            user_info = await application.bot.get_chat(user_id)
            username = user_info.username if user_info.username else user_info.first_name
    except:
        username = "Unknown"

    try:
        async with application:
            await application.bot.send_message(
                user_id,
                f"❌ <b>Payment Failed</b>\n\n"
                f"📋 Order ID: <code>{order_id}</code>\n"
                f"💰 Amount: <code>₹{expected_amount_inr:.2f} INR</code>\n\n"
                f"We couldn't verify your payment. Please contact support with your transaction details if you have completed the payment.",
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Error notifying user about timeout: {e}")

    try:
        async with application:
            await application.bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>UPI Payment Failed/Timeout</b>\n\n"
                f"👤 User: <code>@{username}</code>\n"
                f"🆔 User ID: <code>{user_id}</code>\n"
                f"💰 Amount: <code>₹{expected_amount_inr:.2f} INR</code> (${expected_amount_usd:.2f} USD)\n"
                f"📋 Order ID: <code>{order_id}</code>\n"
                f"⏰ Status: Verification timeout - payment not confirmed",
                parse_mode=ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Error notifying admin about timeout: {e}")

    return False

# State management functions
def clear_state(user_id: int):
    USER_STATES.pop(user_id, None)
    USER_DATA.pop(user_id, None)

def clear_state_only(user_id: int):
    """Remove the active state but keep USER_DATA intact (e.g. bulk-buy session vars)."""
    USER_STATES.pop(user_id, None)

def set_state(user_id: int, state: str):
    USER_STATES[user_id] = state
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {}

def get_state(user_id: int):
    return USER_STATES.get(user_id)

def update_data(user_id: int, **kwargs):
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {}
    USER_DATA[user_id].update(kwargs)

def get_data(user_id: int):
    return USER_DATA.get(user_id, {})

def should_cancel_state(update: Update) -> bool:
    """Check if we should cancel current state"""
    if update.callback_query:
        return True
    if update.message and update.message.text and update.message.text.startswith('/'):
        return True
    return False

async def create_telethon_client(session_string=None, max_retries=5):
    """Create telethon client with enhanced connection settings"""
    for attempt in range(max_retries):
        try:
            if session_string:
                client = TelegramClient(
                    StringSession(session_string),
                    TELETHON_API_ID,
                    TELETHON_API_HASH,
                    connection=ConnectionTcpAbridged,
                    connection_retries=5,
                    retry_delay=1,
                    timeout=30,
                    request_retries=5,
                    auto_reconnect=True,
                    sequential_updates=True
                )
            else:
                client = TelegramClient(
                    StringSession(),
                    TELETHON_API_ID,
                    TELETHON_API_HASH,
                    connection=ConnectionTcpAbridged,
                    connection_retries=5,
                    retry_delay=1,
                    timeout=30,
                    request_retries=5,
                    auto_reconnect=True,
                    sequential_updates=True
                )

            await asyncio.wait_for(client.connect(), timeout=30)

            if client.is_connected():
                logger.info(f"Successfully connected to Telegram (attempt {attempt + 1})")
                return client
            else:
                await client.disconnect()
                raise ConnectionError("Client connected but not authenticated")

        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                logger.info(f"Retrying in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Failed to connect after {max_retries} attempts")
                raise ConnectionError(f"Could not connect to Telegram after {max_retries} attempts")
        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep((attempt + 1) * 2)
            else:
                raise


async def clean_saved_messages_and_write_watermark(
    client: TelegramClient,
    *,
    account_label: str = "account",
) -> bool:
    """Keep only the ownership notice in this account's Saved Messages.

    The ``me`` peer is Telegram's Saved Messages chat. Only that peer is read
    and modified; normal private chats, groups, channels, and contacts are not
    touched. The watermark is written only after all existing messages have
    been deleted and the final state is verified.
    """
    if not client.is_connected():
        raise ConnectionError(f"Telegram client is not connected for {account_label}")

    message_ids = []
    async for message in client.iter_messages("me", limit=None):
        if message.id:
            message_ids.append(message.id)

    deleted = 0
    for start in range(0, len(message_ids), SAVED_MESSAGES_DELETE_BATCH_SIZE):
        batch = message_ids[start:start + SAVED_MESSAGES_DELETE_BATCH_SIZE]
        await asyncio.wait_for(client.delete_messages("me", batch), timeout=30)
        deleted += len(batch)

    watermark = await asyncio.wait_for(
        client.send_message("me", SAVED_MESSAGES_WATERMARK),
        timeout=30,
    )

    remaining = []
    async for message in client.iter_messages("me", limit=None):
        remaining.append(message)
        if len(remaining) > 1:
            break

    if (
        len(remaining) != 1
        or remaining[0].id != watermark.id
        or (remaining[0].raw_text or "") != SAVED_MESSAGES_WATERMARK
    ):
        raise RuntimeError(
            f"Saved Messages cleanup verification failed for {account_label}: "
            f"deleted={deleted}, remaining={len(remaining)}"
        )

    logger.info(
        "Saved Messages cleaned and ownership watermark written for %s (deleted=%d)",
        account_label,
        deleted,
    )
    return True


async def logout_sold_account_session(session_string: str = None, existing_client: TelegramClient = None) -> bool:
    """Logs the bot's side out of a sold account's Telegram session.

    Buyers need certainty that no one else (the seller/bot) still has an active
    session on the account they just paid for. This connects as the account
    (using its own session string, or an already-connected client if one is
    passed in) and calls log_out(), which invalidates that session server-side
    so it disappears from the account's active Devices list.

    Returns True only if the logout call actually succeeded.
    """
    client = existing_client
    created_here = False
    try:
        if client is None:
            if not session_string:
                return False
            if not (TELETHON_API_ID and TELETHON_API_HASH):
                logger.error("Cannot log out sold account session: TELETHON_API_ID/TELETHON_API_HASH not configured.")
                return False
            client = await create_telethon_client(session_string=session_string, max_retries=2)
            created_here = True

        await client.log_out()
        return True
    except Exception as e:
        logger.error(f"Failed to log out sold account session: {e}")
        return False
    finally:
        if client is not None and created_here:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("Telethon disconnect issue during sold-account logout")


async def close_telethon_client(
    client: TelegramClient,
    handler=None,
    *,
    log_out: bool = False,
) -> bool:
    """Remove an optional handler and close a Telethon client safely."""
    if handler is not None:
        try:
            client.remove_event_handler(handler)
        except Exception:
            try:
                client.remove_event_handler(handler, events.NewMessage(from_users=777000))
            except Exception:
                logger.debug("Failed to remove Telethon event handler cleanly")

    logged_out = False
    if log_out:
        logged_out = await logout_sold_account_session(existing_client=client)

    try:
        await client.disconnect()
    except Exception:
        logger.debug("Telethon disconnect issue")
    return logged_out


async def send_sale_logout_notice_and_rating(context: ContextTypes.DEFAULT_TYPE, user_id: int, account_id: int, logged_out: bool) -> None:
    """Tells the buyer whether the account was freed of the bot's session, then asks for a rating."""
    if logged_out:
        await context.bot.send_message(
            user_id,
            "🔒 I have successfully logged out from my side on this Telegram account. "
            "No other device is logged in anymore — it's fully and safely yours now.",
            parse_mode=ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            user_id,
            "⚠️ <b>Heads up:</b> I couldn't confirm automatic logout on this account. "
            "For your safety, please open Telegram Settings → Devices and terminate any session you don't recognize.",
            parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ Auto-logout failed for sold account {account_id} (buyer {user_id}). Manual check needed.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.debug("Failed to notify admin of logout failure")

    keyboard = [
        [InlineKeyboardButton("⭐ 1", callback_data=f"rate_1_{account_id}"),
         InlineKeyboardButton("⭐⭐⭐ 3", callback_data=f"rate_3_{account_id}"),
         InlineKeyboardButton("⭐⭐⭐⭐⭐ 5", callback_data=f"rate_5_{account_id}")]
    ]
    stars_kb = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        user_id,
        "💬 <b>Please rate our service:</b>",
        reply_markup=stars_kb,
        parse_mode=ParseMode.HTML
    )

# Helpers
def format_balance_dual_currency(usd_amount: float) -> str:
    """Format balance showing both USD and INR"""
    inr_amount = usd_amount * USD_TO_INR_RATE
    return f"${usd_amount:.2f} USD (₹{inr_amount:.2f} INR)"

def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🛒 Buy Account"), KeyboardButton("🛍️ Bulk Buy")],
        [KeyboardButton("📊 My Stats"), KeyboardButton("💵 Balance")],
        [KeyboardButton("💱 Deposit"), KeyboardButton("🎁 Referrals")],
        [KeyboardButton("🛟 Support"), KeyboardButton("📚 How To Use")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def get_all_cryptos():
    cryptos = await db.cryptos.find().to_list(1000)
    return cryptos

async def get_available_accounts():
    accounts = await db.accounts.find({"available": True}).to_list(1000)
    return accounts

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Add Account", callback_data="admin_add_account"),
         InlineKeyboardButton("📦 Add Bulk Accounts", callback_data="admin_add_bulk")],
        [InlineKeyboardButton("💳 Add Balance", callback_data="admin_add_balance"),
         InlineKeyboardButton("💰 Add Crypto", callback_data="admin_add_crypto")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_view_stats"),
         InlineKeyboardButton("📂 All Accounts", callback_data="admin_all_accounts")],
        [InlineKeyboardButton("🧑 User Details", callback_data="admin_user_details"),
         InlineKeyboardButton("💳 Sold Accounts", callback_data="admin_sold_accounts")],
        [InlineKeyboardButton("📊 Sales Report", callback_data="admin_sales_report"),
         InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📝 Set Welcome", callback_data="admin_set_welcome"),
         InlineKeyboardButton("🎹 User Manual Video", callback_data="admin_user_manual")],
        [InlineKeyboardButton("🇮🇳 Set UPI ID", callback_data="admin_set_upi"),
         InlineKeyboardButton("🔑 Set MID", callback_data="admin_set_mid")],
        [InlineKeyboardButton("🎁 Set Welcome Bonus", callback_data="admin_set_welcome_bonus"),
         InlineKeyboardButton("📈 Set Commission %", callback_data="admin_set_commission_rate")],
        [InlineKeyboardButton("💰 Default Prices", callback_data="admin_default_prices"),
         InlineKeyboardButton("🔐 Default 2FA Pass", callback_data="admin_default_2fa")],
        [InlineKeyboardButton("🔄 Refresh Panel", callback_data="admin_refresh"),
         InlineKeyboardButton("❌ Close Panel", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ======================== START COMMAND ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_state(user_id)

    # Check for referral code in command args
    referral_code = None
    if context.args and len(context.args) > 0:
        referral_code = context.args[0].upper()

    # Check if user exists
    user = await db.users.find_one({"id": user_id})

    if not user:
        # New user - create account
        new_user_data = {
            "id": user_id,
            "username": update.effective_user.username,
            "balance": 0.0,
            "purchases": 0,
            "failed_purchases": 0,
            "is_first_purchase": True,
            "referred_by": None,
            "referral_stats": {
                "total_referred": 0,
                "completed_purchases": 0,
                "commission_earned": 0.0
            }
        }

        # Handle referral code if provided
        if referral_code:
            # Find referrer by code
            referrer = await db.users.find_one({"referral_code": referral_code})

            if referrer and referrer["id"] != user_id:
                # Valid referral code. To stop one person farming welcome
                # bonuses by creating many Telegram accounts on the same
                # device, the bonus is held in escrow (pending_welcome_bonus)
                # until the mini app's device-verification check passes —
                # see credit_pending_bonuses_loop(), which releases it once
                # `device_verified` is set to True.
                welcome_bonus = await get_welcome_bonus()
                new_user_data["balance"] = 0.0
                new_user_data["referred_by"] = referrer["id"]
                new_user_data["device_verified"] = False
                new_user_data["pending_welcome_bonus"] = welcome_bonus
                new_user_data["pending_referrer_id"] = referrer["id"]

                # Create user
                await db.users.insert_one(new_user_data)

                # Generate referral code for new user
                await ensure_referral_code(user_id)

                await update.message.reply_text(
                    f"🌎 <b>Welcome to the Professional Telegram Account Shop!</b> 🌎\n\n"
                    f"You've been referred by someone awesome!\n"
                    f"🎁 A welcome bonus of <b>{format_balance_dual_currency(welcome_bonus)}</b> is waiting for you.\n\n"
                    f"🔒 <b>One quick step:</b> verify this device to claim it. "
                    f"This just checks you're not reusing a device that already has an account — it takes a few seconds.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔒 Verify Device", web_app=WebAppInfo(url=VERIFY_DEVICE_MINI_APP_URL))],
                    ]),
                    parse_mode=ParseMode.HTML
                )

                return

        # No referral or invalid referral - create normal user
        await db.users.insert_one(new_user_data)
        await ensure_referral_code(user_id)
    else:
        # Existing user - ensure they have referral code, keep username fresh
        await ensure_referral_code(user_id)
        if user.get("username") != update.effective_user.username:
            await db.users.update_one({"id": user_id}, {"$set": {"username": update.effective_user.username}})

    # Send welcome message
    welcome = await db.welcome.find_one()

    if welcome and welcome.get("photo_file_id") and welcome.get("description"):
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=welcome["photo_file_id"],
                caption=welcome["description"],
                reply_markup=main_keyboard(),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.message.reply_text(welcome["description"], reply_markup=main_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            "🌎 <b>Welcome to the Professional Telegram Account Shop!</b> 🌎\n\n"
            "💡 <b>Buy virtual accounts • Pay in crypto/UPI • Professional & Secure</b>\n"
            "💎 <b>NEW: Loyalty Points System! Earn points on every purchase!</b>\n\n"
            "👇 <b>Select an option:</b>",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML
        )

# ======================== REFERRAL SYSTEM COMMANDS ========================
async def _delete_last_inline(bot, user_id: int, chat_id: int):
    """Delete the last tracked inline-keyboard bot message for a user, silently."""
    msg_id = LAST_INLINE_MSG.pop(user_id, None)
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

async def show_referral_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral dashboard with stats and share link"""
    if should_cancel_state(update):
        clear_state(update.effective_user.id)

    user_id = update.effective_user.id

    # Ensure user has referral code
    ref_code = await ensure_referral_code(user_id)

    # Get referral stats
    stats = await get_referral_stats(user_id)

    # Get bot username for share link
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username

    # Create referral link
    referral_link = f"https://t.me/{bot_username}?start={ref_code}"

    # Create share buttons
    share_text = f"🎁 Join this awesome Telegram Account Shop and get ${DEFAULT_WELCOME_BONUS:.2f} USD welcome bonus! Use my referral link:"
    share_url = f"https://t.me/share/url?url={referral_link}&text={share_text}"

    keyboard = [
        [InlineKeyboardButton("📤 Share Referral Link", url=share_url)],
        [InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_ref_{ref_code}")],
        [InlineKeyboardButton("◀️ Back", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Format message
    commission_rate = await get_referral_commission_rate()
    message = (
        f"🎁 <b>REFERRALS</b>\n\n"
        f"🔗 <i>Code:</i> <b>{ref_code}</b>\n\n"
        f"👥 <i>Referred:</i> <b>{stats['total_referred']}</b>\n\n"
        f"✅ <i>Purchases:</i> <b>{stats['completed_purchases']}</b>\n\n"
        f"💰 <i>Earned:</i> <b>{format_balance_dual_currency(stats['commission_earned'])}</b>\n\n"
        f"📈 <i>Commission:</i> <b>{commission_rate}%</b> on every purchase\n\n"
        f"👇 <b>Your Referral Link:</b>\n<code>{referral_link}</code>"
    )

    sent = await update.message.reply_text(
        message,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    LAST_INLINE_MSG[user_id] = sent.message_id

async def handle_copy_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle copy referral link callback"""
    query = update.callback_query
    await query.answer("✅ Referral code shown above - copy it to share!", show_alert=True)

# ======================== BACK BUTTON HANDLERS ========================
async def handle_back_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all back button callbacks"""
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data == "back_to_main":
        await query.message.delete()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🏠 <b>Main Menu</b>\n\nSelect an option:",
            reply_markup=main_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
# ======================== SUPPORT SYSTEM ========================
async def start_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_state(user_id)

    if SUPPORT_CHAT.get(user_id):
        await update.message.reply_text("ℹ️ You are already in a support session. Use the messages here to chat.")
        return

    SUPPORT_CHAT[user_id] = True
    keyboard = [[InlineKeyboardButton("❌ End Support", callback_data="end_support")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_text(
        "💬 <b>You are now connected to admin support. Type your question, send images, or send payment proof.\n"
        "Click 'End Support' to finish.</b>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    LAST_INLINE_MSG[user_id] = sent.message_id
    await context.bot.send_message(ADMIN_ID, f"User {user_id} started support. Use /reply {user_id} <message> to answer or send photo with /replyimg {user_id} <caption>")

async def end_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    SUPPORT_CHAT.pop(user_id, None)
    await query.message.edit_text("✅ Support session ended. Thank you!")

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    clear_state(update.effective_user.id)

    try:
        parts = update.message.text.split(maxsplit=2)
        if len(parts) < 3:
            raise ValueError("bad usage")
        _, user_id_str, text = parts
        user_id = int(user_id_str)
        await context.bot.send_message(user_id, f"✉️ Admin: {text}")
    except Exception:
        await update.message.reply_text("Usage: /reply user_id your_message")

async def admin_reply_with_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin can reply to user with image + caption"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    try:
        caption = update.message.caption or ""
        if not caption.startswith("/replyimg"):
            return
        
        parts = caption.split(maxsplit=2)
        if len(parts) < 2:
            return
        
        user_id = int(parts[1])
        message_text = parts[2] if len(parts) > 2 else "Admin sent an image"
        
        photo_file_id = update.message.photo[-1].file_id
        await context.bot.send_photo(
            chat_id=user_id,
            photo=photo_file_id,
            caption=f"✉️ Admin: {message_text}"
        )
        await update.message.reply_text(f"✅ Image sent to user {user_id}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def admin_endchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    try:
        parts = update.message.text.split()
        if len(parts) < 2:
            raise ValueError("bad usage")
        
        user_id = int(parts[1])
        SUPPORT_CHAT.pop(user_id, None)
        await context.bot.send_message(user_id, "✅ Support session ended by admin.")
        await update.message.reply_text(f"✅ Chat ended with user {user_id}")
    except Exception:
        await update.message.reply_text("Usage: /endchat user_id")

# Placeholder functions (to be completed based on original code)
ACCOUNTS_PAGE_SIZE = 8

async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    if should_cancel_state(update):
        clear_state(update.effective_user.id)

    accounts = await get_available_accounts()

    if not accounts:
        text = "❌ No accounts available at the moment.\n\nPlease check back later!"
        if update.callback_query:
            await update.callback_query.message.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    # Stable ordering so pagination doesn't shift accounts between pages
    accounts.sort(key=lambda a: a.get("id", 0))

    # Group by country (for the summary header only)
    countries = {}
    for acc in accounts:
        country = acc.get("country", "Unknown")
        countries.setdefault(country, []).append(acc)

    total_pages = max(1, (len(accounts) + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * ACCOUNTS_PAGE_SIZE
    page_accounts = accounts[start:start + ACCOUNTS_PAGE_SIZE]

    message = "💳 <b>Available Telegram Accounts</b>\n\n"
    for country, accs in countries.items():
        message += f"🌎 <b>{country}</b>: {len(accs)} account(s) available\n"
    message += f"\nPage {page + 1}/{total_pages}"

    keyboard = []
    for acc in page_accounts:
        price_usd = acc.get("price_usd", 0)
        price_inr = price_usd * USD_TO_INR_RATE
        country = acc.get("country", "Unknown")
        keyboard.append([
            InlineKeyboardButton(
                f"{country} - ${price_usd:.2f} (₹{price_inr:.0f})",
                callback_data=f"buy_{acc['id']}"
            )
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"accpage_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"accpage_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_to_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        sent = await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        LAST_INLINE_MSG[update.effective_user.id] = sent.message_id

async def show_accounts_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        page = int(query.data.replace("accpage_", ""))
    except ValueError:
        page = 0
    await show_accounts(update, context, page=page)

async def _ensure_user(user_id: int, tg_user) -> dict:
    """Return the user doc, auto-creating it if it doesn't exist yet."""
    user = await db.users.find_one({"id": user_id})
    if not user:
        user = {
            "id": user_id,
            "username": getattr(tg_user, "username", None),
            "balance": 0.0,
            "purchases": 0,
            "failed_purchases": 0,
            "is_first_purchase": True,
            "referred_by": None,
            "referral_stats": {"total_referred": 0, "completed_purchases": 0, "commission_earned": 0.0},
        }
        await db.users.insert_one(user)
        await ensure_referral_code(user_id)
    return user

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if should_cancel_state(update):
        clear_state(update.effective_user.id)
    
    user_id = update.effective_user.id
    user = await _ensure_user(user_id, update.effective_user)
    
    balance = user.get("balance", 0)
    purchases = user.get("purchases", 0)
    ref_stats = user.get("referral_stats", {})

    ref_code = await ensure_referral_code(user_id)
    bot_info = await context.bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start={ref_code}"

    joined = None
    oid = user.get("_id")
    if oid is not None and hasattr(oid, "generation_time"):
        joined = oid.generation_time.strftime("%Y-%m-%d")

    message = (
        f"👤 <b>USER PROFILE</b>\n\n"
        f"🆔 <i>User ID:</i> <b>{user_id}</b>\n\n"
        f"💰 <i>Balance:</i> <b>{format_balance_dual_currency(balance)}</b>\n\n"
        f"🛒 <i>Purchases:</i> <b>{purchases}</b>\n\n"
    )
    if joined:
        message += f"📅 <i>Joined:</i> <b>{joined}</b>\n\n"
    message += (
        f"🎁 <i>Referred:</i> <b>{ref_stats.get('total_referred', 0)}</b> • "
        f"<i>Earned:</i> <b>{format_balance_dual_currency(ref_stats.get('commission_earned', 0))}</b>\n\n"
        f"👥 <b>Your Referral Link:</b>\n<code>{referral_link}</code>"
    )

    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    LAST_INLINE_MSG[user_id] = sent.message_id

async def deposit_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if should_cancel_state(update):
        clear_state(update.effective_user.id)
    
    keyboard = [
        [InlineKeyboardButton("💰 Crypto", callback_data="deposit_crypto_list")],
        [InlineKeyboardButton("🇮🇳 UPI Deposit — Open Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton("◀️ Back", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "💱 <b>Deposit Funds</b>\n\n"
        "Select your payment method:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if should_cancel_state(update):
        clear_state(update.effective_user.id)
    
    user_id = update.effective_user.id
    user = await _ensure_user(user_id, update.effective_user)
    
    balance = user.get("balance", 0)
    
    message = (
        f"💵 <b>YOUR BALANCE</b>\n\n"
        f"Balance: <b>{format_balance_dual_currency(balance)}</b>\n\n"
        f"Use /deposit to add funds!"
    )
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

HOW_TO_USE_TEXT = (
    f"📚 <b>How To Use</b>\n\n"
    f"1️⃣ <i>Deposit</i> via UPI or Crypto\n\n"
    f"2️⃣ <i>Browse</i> available accounts\n\n"
    f"3️⃣ <i>Purchase</i> an account\n\n"
    f"4️⃣ <i>Refer</i> friends & earn commission"
)

async def show_user_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if should_cancel_state(update):
        clear_state(update.effective_user.id)
    
    manual = await db.user_manual.find_one()
    
    if manual and manual.get("video_file_id"):
        try:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=manual["video_file_id"],
                caption="📚 <b>How To Use - User Manual</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.message.reply_text(HOW_TO_USE_TEXT, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(HOW_TO_USE_TEXT, parse_mode=ParseMode.HTML)

# Callback handlers for deposits
async def handle_upi_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Opens the UPI deposit Mini App for automatic QR payment verification."""
    query = update.callback_query
    await query.answer()

    await query.message.edit_text(
        f"🇮🇳 <b>UPI Deposit</b>\n\n"
        f"Minimum: ₹{MIN_DEPOSIT_INR:.0f} INR\n\n"
        f"Tap below to open the deposit page. Enter an amount, scan the unique QR, "
        f"and pay. The merchant gateway will verify the payment automatically and update your balance.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Open UPI Deposit Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
        ]),
    )

async def show_crypto_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists admin-configured crypto wallets for the user to pick from."""
    query = update.callback_query
    await query.answer()

    cryptos = await get_all_cryptos()

    if not cryptos:
        await query.message.edit_text("❌ No crypto payment methods configured. Please use UPI or contact support.")
        return

    keyboard = [
        [InlineKeyboardButton(f"💰 {crypto['name']}", callback_data=f"crypto_select_{crypto['id']}")]
        for crypto in cryptos
    ]
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_to_main")])

    await query.message.edit_text(
        "💰 <b>Crypto Deposit</b>\n\nSelect a coin to pay with:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def select_crypto_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the admin-set QR/address for the chosen coin, then asks how much the user sent."""
    query = update.callback_query
    await query.answer()

    crypto_id = int(query.data.replace("crypto_select_", ""))
    user_id = query.from_user.id

    crypto = await db.cryptos.find_one({"id": crypto_id})
    if not crypto:
        await query.message.edit_text("❌ This payment method is no longer available.")
        return

    update_data(user_id, crypto_id=crypto_id, crypto_name=crypto["name"])
    set_state(user_id, "crypto_waiting_amount")

    caption = (
        f"💰 <b>{crypto['name']} Deposit</b>\n\n"
        f"📮 Address:\n<code>{crypto['address']}</code>\n\n"
        f"Send any amount to this address using your wallet or exchange.\n\n"
        f"✏️ After sending, reply here with the <b>USD amount</b> you paid (e.g., 5 or 10):"
    )

    if crypto.get("qr_file_id"):
        await query.message.delete()
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=crypto["qr_file_id"],
            caption=caption,
            parse_mode=ParseMode.HTML
        )
    else:
        await query.message.edit_text(caption, parse_mode=ParseMode.HTML)

async def crypto_confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User confirms they've sent the crypto payment; notifies admin with an approve/reject request."""
    query = update.callback_query
    await query.answer()

    request_id = int(query.data.replace("crypto_confirm_", ""))
    deposit = await db.crypto_deposits.find_one({"id": request_id})

    if not deposit or deposit["user_id"] != query.from_user.id:
        await query.message.edit_text("❌ This payment request could not be found.")
        return

    if deposit["status"] != "pending_confirmation":
        await query.message.edit_text("ℹ️ This request has already been processed.")
        return

    await db.crypto_deposits.update_one(
        {"id": request_id},
        {"$set": {"status": "awaiting_admin", "confirmed_at": datetime.now()}}
    )

    await query.message.edit_text(
        "⏳ <b>Payment Submitted</b>\n\n"
        "Please wait until the admin manually verifies your crypto payment. "
        "It may take a few hours. Thank you for your patience — we will notify you once it's done!",
        reply_markup=None,
        parse_mode=ParseMode.HTML
    )

    try:
        user_info = await context.bot.get_chat(deposit["user_id"])
        username = f"@{user_info.username}" if user_info.username else user_info.first_name
    except Exception:
        username = "Unknown"

    await context.bot.send_message(
        ADMIN_ID,
        f"🪙 <b>New Crypto Payment Claim</b>\n\n"
        f"👤 User: <code>{username}</code>\n"
        f"🆔 User ID: <code>{deposit['user_id']}</code>\n"
        f"💰 Coin: <b>{deposit['crypto_name']}</b>\n"
        f"💵 Claimed Amount: <code>${deposit['amount_usd']:.2f} USD</code>\n\n"
        f"Please check your wallet, then approve or reject:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Approve", callback_data=f"crypto_approve_{request_id}"),
             InlineKeyboardButton("❌ Reject", callback_data=f"crypto_reject_{request_id}")]
        ])
    )

async def crypto_cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User cancels before confirming (e.g. changed their mind or made a typo)."""
    query = update.callback_query
    await query.answer()

    request_id = int(query.data.replace("crypto_cancel_", ""))
    deposit = await db.crypto_deposits.find_one({"id": request_id})

    if deposit and deposit["user_id"] == query.from_user.id and deposit["status"] == "pending_confirmation":
        await db.crypto_deposits.update_one({"id": request_id}, {"$set": {"status": "cancelled_by_user"}})

    await query.message.edit_text("❌ Cancelled. Use /deposit to try again.", reply_markup=None)
    clear_state(query.from_user.id)

async def crypto_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin approves or rejects a claimed crypto payment."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.message.edit_text("❌ Unauthorized")
        return

    approve = query.data.startswith("crypto_approve_")
    request_id = int(query.data.replace("crypto_approve_" if approve else "crypto_reject_", ""))

    deposit = await db.crypto_deposits.find_one({"id": request_id})
    if not deposit:
        await query.message.edit_text("❌ Request not found.")
        return

    if deposit["status"] not in ("awaiting_admin", "pending_confirmation"):
        await query.message.edit_text(f"ℹ️ Already resolved (status: {deposit['status']}).", reply_markup=None)
        return

    user_id = deposit["user_id"]
    amount_usd = deposit["amount_usd"]

    if approve:
        user = await db.users.find_one({"id": user_id})
        new_balance = user.get("balance", 0) + amount_usd if user else amount_usd
        await db.users.update_one({"id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)

        await db.crypto_deposits.update_one(
            {"id": request_id},
            {"$set": {"status": "approved", "resolved_at": datetime.now()}}
        )
        await db.transactions.insert_one({
            "user_id": user_id,
            "type": "crypto_deposit",
            "amount": amount_usd,
            "crypto_name": deposit["crypto_name"],
            "timestamp": datetime.now(),
            "status": "completed"
        })

        if user and user.get("referred_by"):
            await award_referral_commission(user["referred_by"], user_id, amount_usd)

        await context.bot.send_message(
            user_id,
            f"✅ <b>Payment Verified!</b>\n\n"
            f"Your crypto payment of <code>${amount_usd:.2f} USD</code> has been confirmed and credited.\n"
            f"💵 New Balance: <b>{format_balance_dual_currency(new_balance)}</b>\n\n"
            f"Thank you! 🎉",
            parse_mode=ParseMode.HTML
        )
        await query.message.edit_text(f"✅ Approved. Credited ${amount_usd:.2f} to user {user_id}.", reply_markup=None)
    else:
        await db.crypto_deposits.update_one(
            {"id": request_id},
            {"$set": {"status": "rejected", "resolved_at": datetime.now()}}
        )
        await context.bot.send_message(
            user_id,
            f"❌ <b>Payment Not Verified</b>\n\n"
            f"We could not confirm your crypto payment of <code>${amount_usd:.2f} USD</code>. "
            f"If you believe this is a mistake, please contact support with your transaction hash.",
            parse_mode=ParseMode.HTML
        )
        await query.message.edit_text(f"❌ Rejected request for user {user_id}.", reply_markup=None)

# Placeholder for account purchase
async def user_buy_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show two login options: OTP Login or Session File"""
    query = update.callback_query
    await query.answer()
    
    account_id = int(query.data.replace("buy_", ""))
    user_id = query.from_user.id
    
    account = await db.accounts.find_one({"id": account_id, "available": True})
    user = await db.users.find_one({"id": user_id})
    
    if not account:
        await query.message.edit_text("❌ Account no longer available.")
        return
    
    if not user:
        await query.message.edit_text("❌ User not found.")
        return
    
    price_usd = account.get("price_usd", 0)
    discounted_price = price_usd
    
    balance = user.get("balance", 0)
    
    if balance < discounted_price:
        await db.users.update_one({"id": user_id}, {"$inc": {"failed_purchases": 1}})
        await query.message.edit_text(
            f"❌ <b>Insufficient Balance</b>\n\n"
            f"Price: ${price_usd:.2f} USD\n"
            f"Your balance: ${balance:.2f} USD\n"
            f"Need: ${discounted_price - balance:.2f} USD more\n\n"
            f"Please deposit funds first!",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Show two login options
    keyboard = [
        [InlineKeyboardButton("🔐 OTP Login System", callback_data=f"otp_login_{account_id}")],
        [InlineKeyboardButton("📄 Session File System", callback_data=f"session_file_{account_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    inr_price = discounted_price * USD_TO_INR_RATE
    
    await query.message.edit_text(
        f"🎯 <b>Select Login Method</b>\n\n"
        f"Phone: <code>{account['phone']}</code>\n"
        f"🌎 Country: {account['country']}\n"
        f"💵 Price: <b>${discounted_price:.2f} USD (₹{inr_price:.2f} INR)</b>\n\n"
        f"<b>Choose your preferred method:</b>\n\n"
        f"🔐 <b>OTP Login:</b> I'll send you the number and forward OTPs automatically\n"
        f"📄 <b>Session File:</b> Get session file directly for manual import",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

async def handle_otp_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle OTP login system - forwards OTPs automatically"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    clear_state(user_id)
    
    try:
        account_id = int(query.data.split('_')[2])
    except Exception:
        await query.answer("Invalid selection", show_alert=True)
        return
    
    user = await db.users.find_one({"id": user_id})
    account = await db.accounts.find_one({"id": account_id, "available": True})
    
    if not (user and account):
        await context.bot.send_message(user_id, "❌ <b>Account not available or invalid.</b>", parse_mode=ParseMode.HTML)
        return
    
    price_usd = account.get("price_usd", 0)
    discounted_price = price_usd
    savings = 0
    discount_percent = 0
    
    # Check balance again
    if user.get("balance", 0) < discounted_price:
        await db.users.update_one({"id": user_id}, {"$inc": {"failed_purchases": 1}})
        await context.bot.send_message(user_id, "⚠️ <b>Insufficient balance! Please deposit funds.</b>", parse_mode=ParseMode.HTML)
        return
    
    try:
        telethon_client = await create_telethon_client(account["session"])
        
        # OTP handler - forwards OTP from Telegram (777000) to user
        async def otp_handler(event):
            try:
                import re
                full_msg = event.raw_text or str(event.message)
                
                # Extract 5-digit code using regex
                otp_match = re.search(r'\b(\d{5})\b', full_msg)
                
                if otp_match:
                    otp_code = otp_match.group(1)
                    await context.bot.send_message(
                        user_id,
                        f"🔐 <b>OTP for login:</b> <code>{otp_code}</code>\n\n"
                        "Use this code when logging in. If you need the 2FA password, tap 'Need 2FA Password?' below.",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    # Fallback: if no 5-digit code found, extract any digits
                    digits = re.findall(r'\d+', full_msg)
                    if digits:
                        longest_code = max(digits, key=len)
                        await context.bot.send_message(
                            user_id,
                            f"🔐 <b>OTP for login:</b> <code>{longest_code}</code>\n\n"
                            "Use this code when logging in. If you need the 2FA password, tap 'Need 2FA Password?' below.",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await context.bot.send_message(
                            user_id,
                            f"🔐 <b>OTP for login:</b> <code>{full_msg}</code>\n\n"
                            "Use this code when logging in. If you need the 2FA password, tap 'Need 2FA Password?' below.",
                            parse_mode=ParseMode.HTML
                        )
            except Exception as e:
                logger.exception("Error while forwarding OTP: %s", e)
        
        telethon_client.add_event_handler(otp_handler, events.NewMessage(from_users=777000))
        OTP_WATCHERS[user_id] = (telethon_client, otp_handler)
        
        keyboard = [[InlineKeyboardButton("✅ Login Done", callback_data=f"login_done_{account_id}")]]
        if account.get("twofa_pass"):
            keyboard.append([InlineKeyboardButton("🔐 Need 2FA Password?", callback_data=f"login_2fa_{account_id}")])
        
        login_inline = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(
            user_id,
            f"🚀 <b>Login to <code>{account['phone']}</code> in Telegram. I'll forward you OTPs instantly.</b>\n\n"
            f"Phone Number: <code>{account['phone']}</code>\n\n"
            f"When finished logging in, tap <b>✅ Login Done</b> below.",
            reply_markup=login_inline,
            parse_mode=ParseMode.HTML
        )
        
        set_state(user_id, "waiting_for_login_done")
        update_data(user_id,
            telethon_session=account["session"],
            acc_id=account_id,
            tc_name=account["phone"],
            country=account["country"],
            price_usd=price_usd,
            discounted_price=discounted_price,
            discount_percent=discount_percent,
            savings=savings,
            twofa_pass=account.get("twofa_pass")
        )
    
    except Exception as e:
        await context.bot.send_message(
            user_id,
            f"❌ <b>Failed to connect to account:</b> {str(e)}\n\nPlease try again later or contact admin.",
            parse_mode=ParseMode.HTML
        )
        logger.error(f"Failed to connect to telethon client for account {account_id}: {e}")

async def handle_session_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle session file system - sends session file directly"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    clear_state(user_id)
    
    try:
        account_id = int(query.data.split('_')[2])
    except Exception:
        await query.answer("Invalid selection", show_alert=True)
        return
    
    user = await db.users.find_one({"id": user_id})
    account = await db.accounts.find_one({"id": account_id, "available": True})
    
    if not (user and account):
        await context.bot.send_message(user_id, "❌ <b>Account not available or invalid.</b>", parse_mode=ParseMode.HTML)
        return
    
    price_usd = account.get("price_usd", 0)
    discounted_price = price_usd
    savings = 0
    discount_percent = 0
    
    # Check balance again
    if user.get("balance", 0) < discounted_price:
        await db.users.update_one({"id": user_id}, {"$inc": {"failed_purchases": 1}})
        await context.bot.send_message(user_id, "⚠️ <b>Insufficient balance! Please deposit funds.</b>", parse_mode=ParseMode.HTML)
        return

    cleanup_client = None
    try:
        cleanup_client = await create_telethon_client(account.get("session"), max_retries=2)
        await clean_saved_messages_and_write_watermark(
            cleanup_client,
            account_label=account.get("phone", str(account_id)),
        )
    except Exception as cleanup_err:
        logger.error("Saved Messages cleanup failed before session delivery for account %s: %s", account_id, cleanup_err)
        await context.bot.send_message(
            user_id,
            "❌ This account could not be cleaned safely, so the purchase was not completed. "
            "Please try another account or contact admin.",
        )
        return
    finally:
        if cleanup_client:
            try:
                await cleanup_client.disconnect()
            except Exception:
                pass
    
    # Process purchase
    new_balance = user.get("balance", 0) - discounted_price
    await db.users.update_one(
        {"id": user_id},
        {
            "$set": {"balance": new_balance},
            "$inc": {"purchases": 1}
        }
    )
    
    # Mark account as sold
    await db.accounts.update_one(
        {"id": account_id},
        {"$set": {"available": False, "sold_to": user_id, "sold_at": datetime.now()}}
    )
    
    # Award referral commission if user was referred
    if user.get("referred_by"):
        referrer_id = user["referred_by"]
        await award_referral_commission(referrer_id, user_id, discounted_price)
    
    # Record transaction
    await db.transactions.insert_one({
        "user_id": user_id,
        "type": "account_purchase",
        "account_id": account_id,
        "amount": discounted_price,
        "original_price": price_usd,
        "discount": savings,
        "timestamp": datetime.now()
    })
    
    # Send account details
    message = (
        f"✅ <b>Purchase Successful!</b>\n\n"
        f"Phone: <code>{account['phone']}</code>\n"
        f"🌎 Country: {account['country']}\n"
        f"💰 Price: ${price_usd:.2f} USD\n"
        f"💵 Paid: <b>${discounted_price:.2f} USD</b>\n"
        f"💵 New Balance: {format_balance_dual_currency(new_balance)}\n\n"
    )
    
    if account.get("twofa_pass"):
        message += f"🔐 2FA Password: <code>{account['twofa_pass']}</code>\n\n"
    
    message += f"📄 Session file will be sent separately..."
    
    await context.bot.send_message(user_id, message, parse_mode=ParseMode.HTML)
    
    # Send session file
    try:
        session_data = account.get("session", "")
        session_file = BytesIO(session_data.encode())
        session_file.name = f"account_{account_id}.session"
        await context.bot.send_document(
            chat_id=user_id,
            document=session_file,
            caption="📄 Session file for your account"
        )
    except Exception as e:
        logger.error(f"Error sending session file: {e}")
        await context.bot.send_message(
            user_id,
            "❌ Error sending session file. Please contact admin.",
            parse_mode=ParseMode.HTML
        )
    
    # Log the bot out of this account's session, then tell the buyer and ask for a rating
    logged_out = await logout_sold_account_session(session_string=account.get("session"))
    await send_sale_logout_notice_and_rating(context, user_id, account_id, logged_out)

async def send_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send 2FA password to user during OTP login.

    Priority: latest admin-configured default password from `db.config`
    (via get_default_2fa_password). Falls back to the account's
    originally stored twofa_pass if the admin default is unavailable.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Extract account_id from callback data: "login_2fa_{account_id}"
    account_id_str = query.data.replace("login_2fa_", "", 1)
    try:
        account_id = int(account_id_str)
    except ValueError:
        account_id = None

    data = get_data(user_id)

    # 1) Try latest admin panel default from MongoDB config
    twofa = None
    try:
        twofa = await get_default_2fa_password()
    except Exception as e:
        logger.exception("Failed to fetch default 2FA password from config: %s", e)

    # 2) Fallback to the account's original stored password (safety net)
    if not twofa:
        twofa = data.get('twofa_pass')

    if twofa:
        keyboard = []
        if account_id is not None:
            keyboard.append([InlineKeyboardButton("✅ Login Done", callback_data=f"login_done_{account_id}")])
            keyboard.append([InlineKeyboardButton("🔙 Back to Login Screen", callback_data=f"back_to_login_{account_id}")])
        else:
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_main")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.message.edit_text(
            f"🔑 <b>2FA Password:</b>\n\n<code>{twofa}</code>\n\n"
            "Enter this password in Telegram when asked.\n\n"
            "Once logged in, tap <b>✅ Login Done</b> to complete your purchase.",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await query.answer("No 2FA set for this account.", show_alert=True)


async def back_to_login_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rebuild the original OTP login screen (Login Done + Need 2FA Password?)
    without recreating the Telethon client / OTP watcher.

    Triggered by callback: back_to_login_{account_id}
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    account_id_str = query.data.replace("back_to_login_", "", 1)
    try:
        account_id = int(account_id_str)
    except ValueError:
        await query.answer("Invalid selection", show_alert=True)
        return

    account = await db.accounts.find_one({"id": account_id})
    if not account:
        # Fall back gracefully if account no longer available
        data = get_data(user_id)
        phone = data.get("tc_name") or "your account"
        has_twofa = bool(data.get("twofa_pass"))
    else:
        phone = account.get("phone", "your account")
        has_twofa = bool(account.get("twofa_pass"))

    keyboard = [[InlineKeyboardButton("✅ Login Done", callback_data=f"login_done_{account_id}")]]
    if has_twofa:
        keyboard.append([InlineKeyboardButton("🔐 Need 2FA Password?", callback_data=f"login_2fa_{account_id}")])
    login_inline = InlineKeyboardMarkup(keyboard)

    try:
        await query.message.edit_text(
            f"🚀 <b>Login to <code>{phone}</code> in Telegram. I'll forward you OTPs instantly.</b>\n\n"
            f"Phone Number: <code>{phone}</code>\n\n"
            f"When finished logging in, tap <b>✅ Login Done</b> below.",
            reply_markup=login_inline,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.exception("Failed to edit back-to-login message: %s", e)

async def login_done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle login completion for OTP login system"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async def abort_otp_watcher():
        watcher = OTP_WATCHERS.pop(user_id, None)
        if watcher:
            await close_telethon_client(watcher[0], watcher[1], log_out=False)
    
    data = get_data(user_id)
    account_id = data.get("acc_id")
    
    if account_id is None:
        await abort_otp_watcher()
        await query.message.edit_text("❌ Internal error: missing purchase data.")
        clear_state(user_id)
        return
    
    user = await db.users.find_one({"id": user_id})
    account = await db.accounts.find_one({"id": account_id})
    
    if not (user and account and account.get("available")):
        await abort_otp_watcher()
        await query.message.edit_text("❌ Account already sold or unavailable. Contact admin!")
        clear_state(user_id)
        return
    
    discounted_price = data.get("discounted_price")
    price_usd = data.get("price_usd")
    savings = data.get("savings")
    discount_percent = data.get("discount_percent")
    
    # Clean Saved Messages before charging the buyer. Reuse the live seller
    # session when available, but do not touch any other Telegram peer.
    watcher = OTP_WATCHERS.get(user_id)
    telethon_client, handler = watcher if watcher else (None, None)
    cleanup_client_created = False
    if telethon_client is None:
        try:
            telethon_client = await create_telethon_client(account.get("session"), max_retries=2)
            cleanup_client_created = True
        except Exception as cleanup_connect_err:
            logger.error("Could not connect for Saved Messages cleanup on account %s: %s", account_id, cleanup_connect_err)
            await query.message.edit_text(
                "❌ I could not safely clean this account's Saved Messages, so the purchase was not completed."
            )
            return

    try:
        await clean_saved_messages_and_write_watermark(
            telethon_client,
            account_label=account.get("phone", str(account_id)),
        )
    except Exception as cleanup_err:
        logger.error("Saved Messages cleanup failed before OTP delivery for account %s: %s", account_id, cleanup_err)
        OTP_WATCHERS.pop(user_id, None)
        if telethon_client:
            await close_telethon_client(telethon_client, handler, log_out=False)
        await query.message.edit_text(
            "❌ This account could not be cleaned safely, so the purchase was not completed."
        )
        return

    try:
        new_balance = user.get("balance", 0) - discounted_price
        await db.users.update_one(
            {"id": user_id},
            {"$set": {"balance": new_balance, "purchases": user.get("purchases", 0) + 1}}
        )
        await db.accounts.update_one(
            {"id": account_id},
            {"$set": {"available": False, "sold_to": user_id, "sold_at": datetime.now()}}
        )

        # Award referral commission if user was referred
        if user.get("referred_by"):
            referrer_id = user["referred_by"]
            await award_referral_commission(referrer_id, user_id, discounted_price)

        # Record transaction
        await db.transactions.insert_one({
            "user_id": user_id,
            "type": "account_purchase",
            "account_id": account_id,
            "amount": discounted_price,
            "original_price": price_usd,
            "discount": savings,
            "timestamp": datetime.now()
        })
    except Exception:
        OTP_WATCHERS.pop(user_id, None)
        if telethon_client:
            await close_telethon_client(telethon_client, handler, log_out=False)
        raise
    
    # Log the bot out of this account's session after delivery.
    logged_out = False
    if telethon_client:
        OTP_WATCHERS.pop(user_id, None)
        logged_out = await close_telethon_client(telethon_client, handler, log_out=True)
    else:
        # No live client from the OTP flow (e.g. bot restarted mid-flow) — log out using the stored session instead
        logged_out = await logout_sold_account_session(session_string=account.get("session"))
    
    # Send success message
    message = (
        f"🎉 <b>Congratulations! Purchase Complete!</b> 🎉\n\n"
        f"You have successfully purchased:\n"
        f"{account['country']} ({account['phone']})\n\n"
        f"💰 Price: ${price_usd:.2f} USD\n"
        f"💵 Paid: <b>${discounted_price:.2f} USD</b>\n"
        f"💵 New Balance: {format_balance_dual_currency(new_balance)}\n\n"
        f"✅ Please update credentials for your security.\n\n"
        f"💬 Recommend our bot to friends!"
    )
    
    await query.message.edit_text(message, parse_mode=ParseMode.HTML)
    
    # Tell the buyer whether the account was freed of the bot's session, then ask for a rating
    await send_sale_logout_notice_and_rating(context, user_id, account_id, logged_out)
    
    clear_state(user_id)

async def handle_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle service rating after purchase"""
    query = update.callback_query
    await query.answer()
    clear_state(query.from_user.id)
    
    try:
        _, stars_str, acc_id_str = query.data.split("_")
        stars = int(stars_str)
        account_id = int(acc_id_str)
    except Exception:
        await query.answer("Invalid rating", show_alert=True)
        return
    
    buyer_id = query.from_user.id
    
    # Generate unique ID for rating
    rating_id = random.randint(1, 1000000)
    
    await db.ratings.insert_one({
        "id": rating_id,
        "account_id": account_id,
        "buyer_id": buyer_id,
        "stars": stars,
        "timestamp": datetime.now()
    })
    
    await query.message.edit_text("⭐ Thank you for rating us! Please share our bot with friends 😁", parse_mode=ParseMode.HTML)

# ======================== BULK BUY FEATURE ========================

async def _bulk_get_countries():
    """Return distinct countries with available bulk accounts and their counts."""
    pipeline = [
        {"$match": {"available": True}},
        {"$group": {"_id": "$country", "count": {"$sum": 1}}}
    ]
    result = await db.bulk_accounts.aggregate(pipeline).to_list(100)
    return sorted(result, key=lambda x: x["_id"])

async def _bulk_get_years(country: str):
    """Return year+price groups for a country, sorted by year."""
    pipeline = [
        {"$match": {"available": True, "country": country}},
        {"$group": {
            "_id": {"year": "$year", "price_usd": "$price_usd"},
            "count": {"$sum": 1}
        }}
    ]
    result = await db.bulk_accounts.aggregate(pipeline).to_list(100)
    return sorted(result, key=lambda x: x["_id"]["year"])

async def show_bulk_buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for the Bulk Buy menu."""
    user_id = update.effective_user.id
    clear_state(user_id)

    countries = await _bulk_get_countries()
    if not countries:
        await update.message.reply_text(
            "😔 <b>No bulk accounts available right now.</b>\n\nCheck back later!",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )
        return

    update_data(user_id, bb_countries=countries)
    keyboard = []
    for i, row in enumerate(countries):
        country = row["_id"]
        count = row["count"]
        keyboard.append([InlineKeyboardButton(
            f"🌍 {country}  ({count} available)",
            callback_data=f"bb_c_{i}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="bb_cancel")])

    await update.message.reply_text(
        "🛍️ <b>Bulk Buy Accounts</b>\n\nSelect a country:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def bulk_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Central handler for all bb_* callbacks."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # ── Cancel ──────────────────────────────────────────────────────────────
    if data == "bb_cancel":
        # Stop Here/Cancel can be pressed while a bulk OTP watcher is active.
        # Close every watcher owned by this user before clearing the flow.
        watcher_keys = [
            key for key in BULK_OTP_WATCHERS
            if key.startswith(f"{user_id}_")
        ]
        for watcher_key in watcher_keys:
            watcher = BULK_OTP_WATCHERS.pop(watcher_key, None)
            if watcher:
                await close_telethon_client(watcher[0], watcher[1], log_out=False)
        clear_state(user_id)
        await query.message.edit_text("❌ Cancelled.", reply_markup=None)
        return

    # ── Back to country list ─────────────────────────────────────────────────
    if data == "bb_back":
        countries = await _bulk_get_countries()
        if not countries:
            await query.message.edit_text("😔 No bulk accounts available.", reply_markup=None)
            return
        update_data(user_id, bb_countries=countries)
        keyboard = []
        for i, row in enumerate(countries):
            keyboard.append([InlineKeyboardButton(
                f"🌍 {row['_id']}  ({row['count']} available)",
                callback_data=f"bb_c_{i}"
            )])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="bb_cancel")])
        await query.message.edit_text(
            "🛍️ <b>Bulk Buy Accounts</b>\n\nSelect a country:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return

    # ── Country selected → show years ────────────────────────────────────────
    if data.startswith("bb_c_"):
        try:
            idx = int(data[5:])
            d = get_data(user_id)
            countries = d.get("bb_countries") or await _bulk_get_countries()
            country = countries[idx]["_id"]
        except (IndexError, ValueError, KeyError):
            await query.message.edit_text("❌ Session expired. Please start again.")
            return

        years = await _bulk_get_years(country)
        if not years:
            await query.message.edit_text("😔 No accounts left for this country.")
            return

        update_data(user_id, bb_country=country, bb_years=years)
        keyboard = []
        for i, row in enumerate(years):
            yr = row["_id"]["year"]
            pr = row["_id"]["price_usd"]
            cnt = row["count"]
            pr_inr = pr * USD_TO_INR_RATE
            keyboard.append([InlineKeyboardButton(
                f"📅 {yr}   💰 ${pr:.2f} (₹{pr_inr:.0f})   ({cnt} left)",
                callback_data=f"bb_y_{i}"
            )])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="bb_back")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="bb_cancel")])
        await query.message.edit_text(
            f"🌍 <b>{country}</b>\n\nSelect account year & price:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return

    # ── Year+Price selected → ask quantity ────────────────────────────────────
    if data.startswith("bb_y_"):
        try:
            idx = int(data[5:])
            d = get_data(user_id)
            years = d.get("bb_years", [])
            country = d.get("bb_country", "")
            row = years[idx]
            year = row["_id"]["year"]
            price_usd = row["_id"]["price_usd"]
        except (IndexError, ValueError, KeyError):
            await query.message.edit_text("❌ Session expired. Please start again.")
            return

        available = await db.bulk_accounts.count_documents({
            "available": True, "country": country,
            "year": year, "price_usd": price_usd
        })
        if available == 0:
            await query.message.edit_text("😔 None left in this category. Choose another.")
            return

        update_data(user_id, bb_year=year, bb_price=price_usd, bb_max=available)
        set_state(user_id, "bulk_buy_quantity")

        price_inr = price_usd * USD_TO_INR_RATE
        await query.message.edit_text(
            f"🛍️ <b>Bulk Buy</b>\n\n"
            f"🌍 Country: <b>{country}</b>\n"
            f"📅 Year: <b>{year}</b>\n"
            f"💰 Price per account: <b>${price_usd:.2f}</b> (₹{price_inr:.0f})\n"
            f"📦 Available: <b>{available}</b>\n\n"
            f"Enter quantity (1 – {available}):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="bb_cancel")
            ]]),
            parse_mode=ParseMode.HTML
        )
        return

    # ── Delivery mode chosen ─────────────────────────────────────────────────
    if data.startswith("bb_mode_"):
        mode = data[8:]   # "sessions" or "otp"
        d = get_data(user_id)
        country  = d.get("bb_country")
        year     = d.get("bb_year")
        price    = d.get("bb_price")
        qty      = d.get("bb_qty")

        if not all([country, year, price is not None, qty]):
            await query.message.edit_text("❌ Session expired. Please start again.")
            clear_state(user_id)
            return

        user = await db.users.find_one({"id": user_id})
        if not user:
            new_user = {
                "id": user_id,
                "username": query.from_user.username,
                "balance": 0.0,
                "purchases": 0,
                "failed_purchases": 0,
                "is_first_purchase": True,
                "referred_by": None,
                "referral_stats": {"total_referred": 0, "completed_purchases": 0, "commission_earned": 0.0},
            }
            await db.users.insert_one(new_user)
            await ensure_referral_code(user_id)
            user = new_user
        total_price = price * qty
        balance = user.get("balance", 0)

        if balance < total_price:
            pr_inr = total_price * USD_TO_INR_RATE
            await query.message.edit_text(
                f"⚠️ <b>Insufficient Balance!</b>\n\n"
                f"Required: <b>${total_price:.2f}</b> (₹{pr_inr:.0f})\n"
                f"Your balance: <b>{format_balance_dual_currency(balance)}</b>\n\n"
                f"Please deposit funds first.",
                parse_mode=ParseMode.HTML
            )
            clear_state(user_id)
            return

        accounts = await db.bulk_accounts.find({
            "available": True, "country": country,
            "year": year, "price_usd": price
        }).limit(qty).to_list(qty)

        if len(accounts) < qty:
            await query.message.edit_text(
                f"😔 Only {len(accounts)} accounts available. Please restart and choose a smaller quantity."
            )
            clear_state(user_id)
            return

        if mode == "sessions":
            await _bulk_deliver_sessions(query, user_id, accounts, total_price, user, context)
        else:
            await _bulk_start_otp(query, user_id, accounts, total_price, user, context)
        return

    # ── OTP: ready for next account ──────────────────────────────────────────
    if data == "bb_next":
        await _bulk_otp_deliver_next(query, user_id, context)
        return

    # ── OTP: login done for current account ─────────────────────────────────
    if data.startswith("bb_done_"):
        try:
            acc_id = int(data[8:])
        except ValueError:
            return
        await _bulk_otp_login_done(query, user_id, acc_id, context)
        return

    # ── OTP: need 2FA password ───────────────────────────────────────────────
    if data.startswith("bb_2fa_"):
        try:
            acc_id = int(data.replace("bb_2fa_", "", 1))
        except ValueError:
            acc_id = None

        d = get_data(user_id)

        # Priority 1: latest admin panel default from MongoDB config
        twofa = None
        try:
            twofa = await get_default_2fa_password()
        except Exception as e:
            logger.exception("Failed to fetch default 2FA password (bulk): %s", e)

        # Fallback: original account 2FA password
        if not twofa:
            twofa = d.get("bb_cur_2fa")

        if twofa:
            kb = []
            if acc_id is not None:
                kb.append([InlineKeyboardButton(
                    f"✅ Login Done  (Account {d.get('bb_otp_idx', 0) + 1}/{d.get('bb_otp_total', 1)})",
                    callback_data=f"bb_done_{acc_id}"
                )])
                kb.append([InlineKeyboardButton(
                    "🔙 Back to Login Screen", callback_data=f"bb_back_login_{acc_id}"
                )])
            await query.message.edit_text(
                f"🔑 <b>2FA Password:</b>\n\n<code>{twofa}</code>\n\n"
                "Enter this password in Telegram when asked.\n\n"
                "Once logged in, tap <b>✅ Login Done</b> to complete.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb) if kb else None
            )
        else:
            await query.answer("No 2FA set for this account.", show_alert=True)
        return

    # ── OTP: back to login screen from 2FA view ──────────────────────────────
    if data.startswith("bb_back_login_"):
        try:
            acc_id = int(data.replace("bb_back_login_", "", 1))
        except ValueError:
            await query.answer("Invalid selection", show_alert=True)
            return

        d = get_data(user_id)
        idx = d.get("bb_otp_idx", 0)
        total = d.get("bb_otp_total", 1)

        account = await db.bulk_accounts.find_one({"id": acc_id})
        if not account:
            await query.answer("Account no longer available.", show_alert=True)
            return

        kb = [[InlineKeyboardButton(
            f"✅ Login Done  (Account {idx + 1}/{total})",
            callback_data=f"bb_done_{acc_id}"
        )]]
        if account.get("twofa_pass"):
            kb.append([InlineKeyboardButton(
                "🔐 Need 2FA Password?", callback_data=f"bb_2fa_{acc_id}"
            )])

        try:
            await query.message.edit_text(
                f"📲 <b>Account {idx + 1} of {total}</b>\n\n"
                f"📱 Phone: <code>{account['phone']}</code>\n"
                f"🌍 Country: {account.get('country')}\n"
                f"📅 Year: {account.get('year')}\n\n"
                f"Login to Telegram with this number. I'll forward OTPs instantly.\n"
                f"Tap <b>✅ Login Done</b> when finished.",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.exception("Failed to rebuild bulk login screen: %s", e)
        return

# ── Session-file delivery ─────────────────────────────────────────────────────
async def _bulk_deliver_sessions(query, user_id: int, accounts: list,
                                  total_price: float, user: dict, context=None):
    """Deduct balance and send all session files immediately."""
    await query.message.edit_text(
        f"⏳ <b>Processing your order…</b>\n\nPreparing {len(accounts)} session files…",
        parse_mode=ParseMode.HTML
    )

    cleanup_failures = []
    for index, account in enumerate(accounts, 1):
        cleanup_client = None
        try:
            cleanup_client = await create_telethon_client(account.get("session"), max_retries=2)
            await clean_saved_messages_and_write_watermark(
                cleanup_client,
                account_label=account.get("phone", f"account_{index}"),
            )
        except Exception as cleanup_err:
            cleanup_failures.append(f"{index}: {str(cleanup_err)[:80]}")
            logger.error(
                "Saved Messages cleanup failed before bulk session delivery for %s: %s",
                account.get("phone", f"account_{index}"),
                cleanup_err,
            )
        finally:
            if cleanup_client:
                try:
                    await cleanup_client.disconnect()
                except Exception:
                    pass

    if cleanup_failures:
        await query.message.edit_text(
            "❌ Bulk delivery was not completed because some accounts' Saved Messages "
            "could not be cleaned safely.\n\n"
            + "\n".join(cleanup_failures[:10]),
            parse_mode=ParseMode.HTML,
        )
        return

    new_balance = user.get("balance", 0) - total_price
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"balance": new_balance}, "$inc": {"purchases": len(accounts)}}
    )
    account_ids = [a["id"] for a in accounts]
    await db.bulk_accounts.update_many(
        {"id": {"$in": account_ids}},
        {"$set": {"available": False, "sold_to": user_id, "sold_at": datetime.now()}}
    )
    user_doc = await db.users.find_one({"id": user_id})
    if user_doc and user_doc.get("referred_by"):
        await award_referral_commission(user_doc["referred_by"], user_id, total_price)
    await db.transactions.insert_one({
        "user_id": user_id, "type": "bulk_purchase",
        "account_ids": account_ids, "amount": total_price,
        "quantity": len(accounts), "timestamp": datetime.now()
    })

    pr_inr = total_price * USD_TO_INR_RATE
    await query.message.edit_text(
        f"✅ <b>Bulk Purchase Successful!</b>\n\n"
        f"📦 Accounts: <b>{len(accounts)}</b>\n"
        f"💰 Total Paid: <b>${total_price:.2f}</b> (₹{pr_inr:.0f})\n"
        f"💵 New Balance: {format_balance_dual_currency(new_balance)}\n\n"
        f"📄 Sending session files below…",
        parse_mode=ParseMode.HTML
    )

    for i, acc in enumerate(accounts, 1):
        try:
            session_bytes = BytesIO(acc.get("session", "").encode())
            session_bytes.name = f"{acc.get('phone', f'account_{i}')}.session"
            caption = (
                f"📄 <b>Account {i}/{len(accounts)}</b>\n"
                f"📱 Phone: <code>{acc.get('phone', 'N/A')}</code>\n"
                f"🌍 Country: {acc.get('country')}\n"
                f"📅 Year: {acc.get('year')}"
            )
            if acc.get("twofa_pass"):
                caption += f"\n🔐 2FA: <code>{acc['twofa_pass']}</code>"
            await query.message.reply_document(
                document=session_bytes, caption=caption, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Error sending bulk session file {i}: {e}")
            await context.bot.send_message(query.message.chat_id, f"⚠️ Account {i}: error sending file — contact admin.")

    await context.bot.send_message(
        query.message.chat_id,
        "🎉 <b>All done! Enjoy your accounts.</b>",
        reply_markup=main_keyboard(), parse_mode=ParseMode.HTML
    )
    clear_state(user_id)

# ── OTP delivery ──────────────────────────────────────────────────────────────
async def _bulk_start_otp(query, user_id: int, accounts: list,
                           total_price: float, user: dict, context):
    """Store delivery queue and kick off the first OTP account."""
    account_ids = [a["id"] for a in accounts]
    price_each  = total_price / len(accounts)
    update_data(user_id,
        bb_otp_ids=account_ids, bb_otp_idx=0,
        bb_otp_total=len(accounts), bb_otp_price_each=price_each
    )
    set_state(user_id, "bulk_otp_delivery")
    await query.message.edit_text(
        f"📲 <b>OTP Delivery Started!</b>\n\n"
        f"I'll deliver <b>{len(accounts)}</b> accounts one by one.\n"
        f"For each account I'll give the phone number and forward the OTP live.\n\n"
        f"Getting account 1 ready…",
        parse_mode=ParseMode.HTML
    )
    await _bulk_otp_connect(user_id, account_ids[0], 0, len(accounts), context)

async def _bulk_otp_connect(user_id: int, account_id: int, index: int,
                              total: int, context):
    """Connect to one bulk account and set up the OTP watcher."""
    account = await db.bulk_accounts.find_one({"id": account_id, "available": True})
    if not account:
        await context.bot.send_message(
            user_id, f"⚠️ Account {index+1} unavailable. Skipping…"
        )
        d = get_data(user_id)
        ids = d.get("bb_otp_ids", [])
        nxt = index + 1
        update_data(user_id, bb_otp_idx=nxt)
        if nxt < len(ids):
            await _bulk_otp_connect(user_id, ids[nxt], nxt, total, context)
        else:
            await context.bot.send_message(user_id, "✅ All accounts processed!")
            clear_state(user_id)
        return

    try:
        tc = await create_telethon_client(account["session"])

        async def _otp_fwd(event):
            try:
                import re
                raw = event.raw_text or str(event.message)
                m = re.search(r'\b(\d{5})\b', raw)
                code = m.group(1) if m else max(re.findall(r'\d+', raw) or [raw], key=len)
                await context.bot.send_message(
                    user_id,
                    f"🔐 <b>OTP for Account {index+1}:</b> <code>{code}</code>",
                    parse_mode=ParseMode.HTML
                )
            except Exception as ex:
                logger.exception(f"Bulk OTP fwd error: {ex}")

        tc.add_event_handler(_otp_fwd, events.NewMessage(from_users=777000))
        BULK_OTP_WATCHERS[f"{user_id}_{index}"] = (tc, _otp_fwd)

        update_data(user_id, bb_cur_2fa=account.get("twofa_pass"))

        keyboard = [[InlineKeyboardButton(
            f"✅ Login Done  (Account {index+1}/{total})",
            callback_data=f"bb_done_{account_id}"
        )]]
        if account.get("twofa_pass"):
            keyboard.append([InlineKeyboardButton(
                "🔐 Need 2FA Password?", callback_data=f"bb_2fa_{account_id}"
            )])

        await context.bot.send_message(
            user_id,
            f"📲 <b>Account {index+1} of {total}</b>\n\n"
            f"📱 Phone: <code>{account['phone']}</code>\n"
            f"🌍 Country: {account.get('country')}\n"
            f"📅 Year: {account.get('year')}\n\n"
            f"Login to Telegram with this number. I'll forward OTPs instantly.\n"
            f"Tap <b>✅ Login Done</b> when finished.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await context.bot.send_message(
            user_id,
            f"❌ Failed to connect to account {index+1}: {e}\n\nContact admin."
        )
        logger.error(f"Bulk OTP connect error (acc {account_id}): {e}")

async def _bulk_otp_deliver_next(query, user_id: int, context):
    """User tapped 'Ready for next account'."""
    d    = get_data(user_id)
    ids  = d.get("bb_otp_ids", [])
    idx  = d.get("bb_otp_idx", 0)
    tot  = d.get("bb_otp_total", 1)

    if idx >= len(ids):
        await query.message.edit_text("✅ All accounts delivered!", reply_markup=None)
        clear_state(user_id)
        return

    await query.message.edit_text(
        f"⏳ Connecting to account {idx+1} of {tot}…", reply_markup=None
    )
    await _bulk_otp_connect(user_id, ids[idx], idx, tot, context)

async def _bulk_otp_login_done(query, user_id: int, acc_id: int, context):
    """User confirmed login for one bulk OTP account."""
    d          = get_data(user_id)
    ids        = d.get("bb_otp_ids", [])
    idx        = d.get("bb_otp_idx", 0)
    tot        = d.get("bb_otp_total", 1)
    price_each = d.get("bb_otp_price_each", 0)
    wkey       = f"{user_id}_{idx}"

    async def abort_bulk_watcher():
        watcher = BULK_OTP_WATCHERS.pop(wkey, None)
        if watcher:
            await close_telethon_client(watcher[0], watcher[1], log_out=False)

    account = await db.bulk_accounts.find_one({"id": acc_id, "available": True})
    user    = await db.users.find_one({"id": user_id})

    if not (user and account):
        await abort_bulk_watcher()
        await query.message.edit_text("❌ Error — account unavailable. Contact admin.")
        return

    if user.get("balance", 0) < price_each:
        await abort_bulk_watcher()
        await query.message.edit_text(
            f"⚠️ Insufficient balance for account {idx+1}. Purchase stopped."
        )
        clear_state(user_id)
        return

    watcher = BULK_OTP_WATCHERS.get(wkey)
    cleanup_client = watcher[0] if watcher else None
    cleanup_client_created = False
    if cleanup_client is None:
        try:
            cleanup_client = await create_telethon_client(account.get("session"), max_retries=2)
            cleanup_client_created = True
        except Exception as cleanup_connect_err:
            logger.error("Could not connect for bulk Saved Messages cleanup on account %s: %s", acc_id, cleanup_connect_err)
            await abort_bulk_watcher()
            await query.message.edit_text(
                f"❌ Account {idx + 1} could not be cleaned safely. Purchase stopped."
            )
            return

    try:
        await clean_saved_messages_and_write_watermark(
            cleanup_client,
            account_label=account.get("phone", str(acc_id)),
        )
    except Exception as cleanup_err:
        logger.error("Saved Messages cleanup failed before bulk OTP delivery for account %s: %s", acc_id, cleanup_err)
        tup = BULK_OTP_WATCHERS.pop(wkey, None)
        if tup:
            await close_telethon_client(tup[0], tup[1], log_out=False)
        elif cleanup_client_created and cleanup_client:
            await close_telethon_client(cleanup_client, log_out=False)
        await query.message.edit_text(
            f"❌ Account {idx + 1} could not be cleaned safely. Purchase stopped."
        )
        return
    finally:
        if cleanup_client_created and cleanup_client:
            try:
                await cleanup_client.disconnect()
            except Exception:
                pass

    try:
        # Deduct and mark sold
        new_balance = user.get("balance", 0) - price_each
        await db.users.update_one(
            {"id": user_id},
            {"$set": {"balance": new_balance}, "$inc": {"purchases": 1}}
        )
        await db.bulk_accounts.update_one(
            {"id": acc_id},
            {"$set": {"available": False, "sold_to": user_id, "sold_at": datetime.now()}}
        )
        if user.get("referred_by"):
            await award_referral_commission(user["referred_by"], user_id, price_each)
        await db.transactions.insert_one({
            "user_id": user_id, "type": "bulk_otp_purchase",
            "account_id": acc_id, "account_index": idx + 1,
            "amount": price_each, "timestamp": datetime.now()
        })
    except Exception:
        await abort_bulk_watcher()
        raise

    # Clean up watcher
    tup  = BULK_OTP_WATCHERS.pop(wkey, None)
    if tup:
        tc, handler = tup
        await close_telethon_client(tc, handler, log_out=True)

    pr_inr = price_each * USD_TO_INR_RATE
    next_idx = idx + 1
    update_data(user_id, bb_otp_idx=next_idx)

    if next_idx >= tot:
        await query.message.edit_text(
            f"✅ <b>Account {idx+1}/{tot} delivered!</b>\n\n"
            f"💰 Paid: ${price_each:.2f} (₹{pr_inr:.0f})\n"
            f"💵 Remaining Balance: {format_balance_dual_currency(new_balance)}\n\n"
            f"🎉 <b>All accounts delivered successfully!</b>\n\nThank you for your purchase!",
            reply_markup=None,
            parse_mode=ParseMode.HTML
        )
        clear_state(user_id)
    else:
        remaining = tot - next_idx
        keyboard = [
            [InlineKeyboardButton(
                f"✅ Ready for Account {next_idx+1}",
                callback_data="bb_next"
            )],
            [InlineKeyboardButton("❌ Stop Here", callback_data="bb_cancel")]
        ]
        await query.message.edit_text(
            f"✅ <b>Account {idx+1}/{tot} delivered!</b>\n\n"
            f"💰 Paid: ${price_each:.2f} (₹{pr_inr:.0f})\n"
            f"💵 Remaining Balance: {format_balance_dual_currency(new_balance)}\n\n"
            f"📦 {remaining} more remaining. Ready for account {next_idx+1}?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

# ── Admin: process bulk ZIP upload ────────────────────────────────────────────
async def process_bulk_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download the admin's ZIP, extract .session files, verify each, then ask mode.
    Country is auto-detected from each phone number prefix.
    Price is looked up from the default_prices collection (country + year).
    """
    user_id = update.effective_user.id
    d       = get_data(user_id)
    year    = d.get("bulk_year")
    twofa   = d.get("bulk_twofa")

    doc = update.message.document
    if not (doc and doc.file_name and doc.file_name.lower().endswith(".zip")):
        await update.message.reply_text("❌ Please send a .zip file.")
        return

    status_msg = await update.message.reply_text(
        "⏳ Downloading and verifying sessions… this may take a moment."
    )

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        zip_buf = BytesIO()
        await tg_file.download_to_memory(zip_buf)
        zip_buf.seek(0)

        if not zipfile.is_zipfile(zip_buf):
            await status_msg.edit_text("❌ Invalid ZIP file.")
            clear_state(user_id)
            return

        zip_buf.seek(0)
        valid_accounts = []
        failed_list    = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(zip_buf) as zf:
                session_files = [f for f in zf.namelist() if f.lower().endswith(".session")]
                if not session_files:
                    await status_msg.edit_text("❌ No .session files found in ZIP.")
                    clear_state(user_id)
                    return

                await status_msg.edit_text(
                    f"📂 Found {len(session_files)} session file(s). Verifying…"
                )

                for idx, sf in enumerate(session_files):
                    client = None
                    try:
                        zf.extract(sf, tmpdir)
                        full_path    = os.path.join(tmpdir, sf)
                        session_name = full_path[:-8]  # strip ".session"

                        client = TelegramClient(
                            session_name, TELETHON_API_ID, TELETHON_API_HASH,
                            connection_retries=1,
                        )
                        await asyncio.wait_for(client.connect(), timeout=20)

                        authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=15)
                        if authorized:
                            me    = await asyncio.wait_for(client.get_me(), timeout=15)
                            phone = f"+{me.phone}" if me and me.phone else os.path.basename(sf).replace(".session", "")
                            s_str = StringSession.save(client.session)
                            await clean_saved_messages_and_write_watermark(
                                client,
                                account_label=phone,
                            )
                            await client.disconnect()
                            country = detect_country_from_phone(phone)
                            valid_accounts.append({"phone": phone, "session": s_str, "country": country})
                        else:
                            await client.disconnect()
                            failed_list.append(f"{sf} (not authorized)")

                        # Progress update every 5 sessions
                        if (idx + 1) % 5 == 0:
                            await status_msg.edit_text(
                                f"📂 Verifying… {idx + 1}/{len(session_files)} done "
                                f"({len(valid_accounts)} valid so far)"
                            )
                    except asyncio.TimeoutError:
                        failed_list.append(f"{sf}: connection timed out (skipped)")
                        if client:
                            try: await client.disconnect()
                            except Exception: pass
                    except Exception as ex:
                        failed_list.append(f"{sf}: {str(ex)[:60]}")
                        if client:
                            try: await client.disconnect()
                            except Exception: pass

        if not valid_accounts:
            err_lines = "\n".join(failed_list[:10])
            await status_msg.edit_text(
                f"❌ No valid accounts found.\n\nFailed:\n{err_lines}"
            )
            clear_state(user_id)
            return

        # --- Auto-detect country + look up default prices per country ---
        country_counts: dict = {}
        for acc in valid_accounts:
            c = acc["country"]
            country_counts[c] = country_counts.get(c, 0) + 1

        # Resolve price for each account from default_prices DB
        price_cache: dict = {}
        no_price_countries: list = []
        for acc in valid_accounts:
            c = acc["country"]
            if c not in price_cache:
                dp = await get_default_price(c, str(year))
                price_cache[c] = dp
                if dp is None and c not in no_price_countries:
                    no_price_countries.append(c)
            acc["price_usd"] = price_cache[c] if price_cache[c] is not None else 0.0

        update_data(user_id, bulk_valid_accounts=valid_accounts, bulk_failed=failed_list)

        # Build country breakdown summary (stored for later display)
        country_lines = ""
        for c, cnt in sorted(country_counts.items(), key=lambda x: -x[1]):
            p = price_cache.get(c)
            price_str = f"${p:.2f}" if p is not None else "⚠️ no default price"
            country_lines += f"  {c}: {cnt} account(s) @ {price_str}\n"
        update_data(user_id, bulk_country_lines=country_lines,
                    bulk_no_price_countries=no_price_countries,
                    bulk_total_sessions=len(session_files))

        # Ask admin for the existing 2FA password on these accounts
        default_pw = await get_default_2fa_password()
        await status_msg.edit_text(
            f"✅ <b>Verified {len(valid_accounts)}/{len(session_files)} sessions</b>\n\n"
            f"🔐 <b>Step: Enter the existing 2FA password</b> currently set on these accounts\n"
            f"(or type <code>none</code> if accounts have no 2FA).\n\n"
            f"The bot will change it to the default: <code>{default_pw}</code>",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "bulk_existing_2fa")

    except Exception as e:
        await status_msg.edit_text(f"❌ Error processing ZIP: {e}")
        clear_state(user_id)
        logger.exception(f"Bulk ZIP processing error: {e}")

async def bulk_add_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin chooses whether to add verified bulk sessions to Bulk Buy or Normal mode.
    Each account carries its own auto-detected country and default price.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id != ADMIN_ID:
        return

    d             = get_data(user_id)
    accounts_data = d.get("bulk_valid_accounts", [])
    year          = d.get("bulk_year")
    twofa         = d.get("bulk_twofa")

    if not accounts_data:
        await query.message.edit_text("❌ No account data. Please start over.", reply_markup=None)
        clear_state(user_id)
        return

    mode   = query.data   # "ba_mode_bulk" or "ba_mode_normal"
    added  = 0
    grp_id = random.randint(100000, 9999999)

    if mode == "ba_mode_bulk":
        for acc in accounts_data:
            acc_id = random.randint(1, 9999999)
            await db.bulk_accounts.insert_one({
                "id": acc_id, "phone": acc["phone"],
                "country": acc.get("country", "🌐 Unknown"),
                "year": year,
                "price_usd": acc.get("price_usd", 0.0),
                "twofa_pass": twofa,
                "session": acc["session"], "available": True,
                "bulk_group_id": grp_id, "added_at": datetime.now()
            })
            added += 1
        await query.message.edit_text(
            f"✅ <b>Added {added} accounts to Bulk Buy!</b>\n\n"
            f"They are now available in the 🛍️ Bulk Buy section.",
            reply_markup=None, parse_mode=ParseMode.HTML
        )
    else:
        for acc in accounts_data:
            acc_id = random.randint(1, 1000000)
            await db.accounts.insert_one({
                "id": acc_id, "phone": acc["phone"],
                "country": acc.get("country", "🌐 Unknown"),
                "price_usd": acc.get("price_usd", 0.0),
                "available": True, "session": acc["session"],
                "twofa_pass": twofa
            })
            added += 1
        await query.message.edit_text(
            f"✅ <b>Added {added} accounts to Normal Accounts!</b>\n\n"
            f"They are now available in the 🛒 Buy Account section.",
            reply_markup=None, parse_mode=ParseMode.HTML
        )

    clear_state(user_id)


# ======================== ADMIN PANEL ========================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized")
        return
    
    clear_state(update.effective_user.id)
    
    await update.message.reply_text(
        "🔧 <b>Admin Panel</b>\n\nSelect an option:",
        reply_markup=admin_panel_keyboard(),
        parse_mode=ParseMode.HTML
    )

async def admin_panel_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        await query.message.edit_text("❌ Unauthorized")
        return
    
    user_id = query.from_user.id
    callback_data = query.data
    
    if callback_data == "admin_refresh":
        await query.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
        await query.answer("✅ Panel refreshed")
        return
    
    elif callback_data == "admin_close":
        await query.message.delete()
        return
    
    elif callback_data == "admin_add_bulk":
        await query.message.edit_text(
            "📦 <b>Add Bulk Accounts</b>\n\n"
            "🤖 Country is <b>auto-detected</b> from each phone number.\n"
            "💰 Price is pulled from your <b>Default Prices</b> settings.\n\n"
            "📅 Step 1: Enter the <b>year</b> for these accounts (e.g. 2020, 2021):",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "add_bulk_year")

    elif callback_data == "admin_add_account":
        await query.message.edit_text("📱 Send the phone number with country code (e.g. +1234567890):")
        set_state(user_id, "add_account_phone")
    
    elif callback_data == "admin_add_balance":
        await query.message.edit_text("Enter the user ID to add balance to:")
        set_state(user_id, "add_balance_user")
    
    elif callback_data == "admin_add_crypto":
        await query.message.edit_text("Enter the cryptocurrency name (e.g. BTC, ETH, USDT):")
        set_state(user_id, "add_crypto_name")
    
    elif callback_data == "admin_view_stats":
        users = await db.users.find().sort("id", 1).to_list(2000)

        if not users:
            await query.message.edit_text("📊 <b>Stats</b>\n\nNo users yet.", parse_mode=ParseMode.HTML)
        else:
            lines = [f"📊 <b>Stats</b> — {len(users)} users\n"]
            for u in users:
                username = f"@{u['username']}" if u.get("username") else "no username"
                joined = "—"
                oid = u.get("_id")
                if oid is not None and hasattr(oid, "generation_time"):
                    joined = oid.generation_time.strftime("%Y-%m-%d")

                lines.append(
                    f"🆔 <b>{u.get('id')}</b> ({username})\n"
                    f"💰 {format_balance_dual_currency(u.get('balance', 0))}\n"
                    f"✅ {u.get('purchases', 0)} • ❌ {u.get('failed_purchases', 0)}\n"
                    f"📅 {joined}\n"
                )

            # Telegram messages are capped at 4096 chars — chunk the list
            first_chunk = True
            chunk = ""
            for entry in lines:
                if len(chunk) + len(entry) > 3800:
                    if first_chunk:
                        await query.message.edit_text(chunk, parse_mode=ParseMode.HTML)
                        first_chunk = False
                    else:
                        await context.bot.send_message(query.message.chat_id, chunk, parse_mode=ParseMode.HTML)
                    chunk = ""
                chunk += entry + "\n"
            if chunk:
                if first_chunk:
                    await query.message.edit_text(chunk, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_message(query.message.chat_id, chunk, parse_mode=ParseMode.HTML)
    
    elif callback_data == "admin_user_details":
        await query.message.edit_text("Enter the user ID to view details:")
        set_state(user_id, "view_user_details")
    
    elif callback_data == "admin_all_accounts":
        accounts = await db.accounts.find().sort("id", 1).limit(20).to_list(20)

        if not accounts:
            await query.message.edit_text("📂 <b>All Accounts</b>\n\nNo accounts added yet.", parse_mode=ParseMode.HTML)
        else:
            await query.message.edit_text(f"📂 <b>All Accounts</b> (First {len(accounts)})", parse_mode=ParseMode.HTML)
            for acc in accounts:
                status = "✅ Available" if acc.get("available") else "❌ Sold"
                price_usd = acc.get("price_usd", 0)
                price_inr = price_usd * USD_TO_INR_RATE
                text = (
                    f"🆔 <b>{acc['id']}</b> • {acc.get('country', 'Unknown')}\n"
                    f"💵 <b>${price_usd:.2f}</b> (₹{price_inr:.0f}) • {status}"
                )
                await context.bot.send_message(
                    query.message.chat_id,
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✏️ Edit Price", callback_data=f"editprice_{acc['id']}")]
                    ]),
                    parse_mode=ParseMode.HTML
                )
    
    elif callback_data == "admin_sold_accounts":
        sold = await db.accounts.find({"available": False}).limit(20).to_list(20)
        message = "💳 <b>Sold Accounts (First 20)</b>\n\n"
        for acc in sold:
            message += f"ID: {acc['id']} | {acc['country']} | Sold to: {acc.get('sold_to', 'N/A')}\n"
        await query.message.edit_text(message, parse_mode=ParseMode.HTML)
    
    elif callback_data == "admin_sales_report":
        total_sales = await db.transactions.count_documents({"type": "account_purchase"})
        pipeline = [
            {"$match": {"type": "account_purchase"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
        ]
        result = await db.transactions.aggregate(pipeline).to_list(1)
        total_revenue = result[0]["total"] if result else 0
        
        await query.message.edit_text(
            f"📊 <b>Sales Report</b>\n\n"
            f"Total Sales: {total_sales}\n"
            f"Total Revenue: ${total_revenue:.2f} USD (₹{total_revenue * USD_TO_INR_RATE:.2f} INR)",
            parse_mode=ParseMode.HTML
        )
    
    elif callback_data == "admin_broadcast":
        await query.message.edit_text("📢 Enter the broadcast message:")
        set_state(user_id, "broadcast_message")
    
    elif callback_data == "admin_set_welcome":
        await query.message.edit_text("📝 Send a photo for the welcome message:")
        set_state(user_id, "welcome_photo")
    
    elif callback_data == "admin_user_manual":
        await query.message.edit_text("🎹 Send a video for the user manual:")
        set_state(user_id, "user_manual_video")
    
    elif callback_data == "admin_set_upi":
        await query.message.edit_text("🇮🇳 Enter the UPI ID (e.g., username@paytm):")
        set_state(user_id, "set_upi_id")
    
    elif callback_data == "admin_set_mid":
        await query.message.edit_text("🔑 Enter the MID (Merchant ID):")
        set_state(user_id, "set_mid")
    
    elif callback_data == "admin_set_welcome_bonus":
        current_bonus = await get_welcome_bonus()
        await query.message.edit_text(
            f"🎁 <b>Set Welcome Bonus</b>\n\n"
            f"Current bonus: <b>${current_bonus:.2f} USD</b> (₹{current_bonus * USD_TO_INR_RATE:.2f} INR)\n\n"
            f"Enter new welcome bonus amount in USD (e.g., 1 or 2.5):",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "set_welcome_bonus")
    
    elif callback_data == "admin_set_commission_rate":
        current_rate = await get_referral_commission_rate()
        await query.message.edit_text(
            f"📈 <b>Set Referral Commission Rate</b>\n\n"
            f"Current rate: <b>{current_rate}%</b>\n\n"
            f"Enter new commission rate (e.g., 10 for 10%):",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "set_commission_rate")

    elif callback_data == "admin_default_prices":
        prices = await get_all_default_prices()
        if prices:
            lines = "\n".join(
                f"  {p['country']}  |  {p['year']}  →  ${p['price_usd']:.2f}"
                for p in prices
            )
            msg = f"💰 <b>Default Prices</b>\n\n<pre>{lines}</pre>\n\nTap a button to add or update a price:"
        else:
            msg = "💰 <b>Default Prices</b>\n\nNo default prices set yet.\n\nTap a button to add one:"

        kb = [
            [InlineKeyboardButton("🇺🇸 Set USA Price",    callback_data="admin_dp_country_🇺🇸 USA")],
            [InlineKeyboardButton("🇮🇳 Set IND Price",    callback_data="admin_dp_country_🇮🇳 IND")],
            [InlineKeyboardButton("🇬🇧 Set UK Price",     callback_data="admin_dp_country_🇬🇧 UK")],
            [InlineKeyboardButton("🇦🇺 Set AUS Price",    callback_data="admin_dp_country_🇦🇺 AUS")],
            [InlineKeyboardButton("🇦🇪 Set UAE Price",    callback_data="admin_dp_country_🇦🇪 UAE")],
            [InlineKeyboardButton("🌐 Other Country",     callback_data="admin_dp_country_other")],
            [InlineKeyboardButton("◀️ Back to Panel",     callback_data="admin_refresh")],
        ]
        await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

    elif callback_data.startswith("admin_dp_country_"):
        chosen = callback_data.replace("admin_dp_country_", "", 1)
        if chosen == "other":
            await query.message.edit_text(
                "🌐 <b>Set Default Price – Other Country</b>\n\n"
                "Enter the country label exactly as the bot shows it (e.g. <code>🇷🇺 RUS</code>):",
                parse_mode=ParseMode.HTML
            )
            set_state(user_id, "add_dp_country_manual")
        else:
            update_data(user_id, dp_country=chosen)
            await query.message.edit_text(
                f"💰 <b>Default Price for {chosen}</b>\n\n"
                f"📅 Enter the <b>year</b> (e.g. 2020, 2021):",
                parse_mode=ParseMode.HTML
            )
            set_state(user_id, "add_dp_year")

    elif callback_data == "admin_default_2fa":
        current = await get_default_2fa_password()
        await query.message.edit_text(
            f"🔐 <b>Default 2FA Password</b>\n\n"
            f"Current: <code>{current}</code>\n\n"
            f"This password is set on <b>every account</b> when a ZIP is processed.\n\n"
            f"Send the new default 2FA password (min 8 chars), or type <code>none</code> to disable auto-change:",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "set_default_2fa")

async def handle_edit_price_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin tapped ✏️ Edit Price under an account listing."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    try:
        acc_id = int(query.data.replace("editprice_", ""))
    except ValueError:
        return

    account = await db.accounts.find_one({"id": acc_id})
    if not account:
        await query.message.edit_text("❌ Account not found.")
        return

    current_usd = account.get("price_usd", 0)
    current_inr = current_usd * USD_TO_INR_RATE

    update_data(admin_id, edit_price_account_id=acc_id)
    set_state(admin_id, "edit_price_amount")

    await query.message.edit_text(
        f"✏️ <b>Edit Price</b> — {account.get('country', 'Unknown')} (ID {acc_id})\n\n"
        f"Current: ₹{current_inr:.0f} (${current_usd:.2f})\n\n"
        f"Send the new price in rupees (e.g. 80):",
        parse_mode=ParseMode.HTML
    )

# ======================== STATE MESSAGE HANDLERS ========================
async def handle_state_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_state = get_state(user_id)

    if not current_state:
        return False

    # Commission rate setting
    if current_state == "set_commission_rate":
        try:
            rate = float(update.message.text.strip())
            
            if rate < 0 or rate > 100:
                await update.message.reply_text("❌ Rate must be between 0 and 100. Please try again:")
                return True
            
            existing = await db.config.find_one({"key": "referral_commission_rate"})
            if existing:
                await db.config.update_one(
                    {"key": "referral_commission_rate"},
                    {"$set": {"value": rate, "updated_at": datetime.now()}}
                )
            else:
                await db.config.insert_one({
                    "key": "referral_commission_rate",
                    "value": rate,
                    "created_at": datetime.now()
                })
            
            await update.message.reply_text(
                f"✅ <b>Referral Commission Rate Updated!</b>\n\n"
                f"New rate: <b>{rate}%</b>\n\n"
                f"Referrers will now earn {rate}% commission on ALL purchases.",
                parse_mode=ParseMode.HTML
            )
            clear_state(user_id)
            return True
        
        except ValueError:
            await update.message.reply_text("❌ Invalid rate. Please enter a number (e.g., 10 or 15.5):")
            return True


    # Admin add account flow
    if current_state == "add_account_phone":
        phone = update.message.text.strip()
        if not (phone.startswith("+") and phone[1:].replace(' ', '').replace('-', '').isdigit()):
            await update.message.reply_text("❌ Invalid phone number. Must include country code and start with '+'.")
            return True

        update_data(user_id, phone=phone)

        # Create telethon client and send code with enhanced error handling
        try:
            await update.message.reply_text("📱 Connecting to Telegram... Please wait.")

            telethon_client = await create_telethon_client()

            await update.message.reply_text("📄 Sending verification code...")

            # Send code with retry logic
            for attempt in range(3):
                try:
                    sent = await asyncio.wait_for(
                        telethon_client.send_code_request(phone),
                        timeout=30
                    )
                    break
                except (FloodWaitError, asyncio.TimeoutError) as e:
                    if attempt < 2:
                        wait_time = 5 * (attempt + 1)
                        await update.message.reply_text(f"⏳ Rate limited. Waiting {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise e
                except PhoneNumberInvalidError:
                    await update.message.reply_text("❌ Invalid phone number format. Please try again.")
                    clear_state(user_id)
                    return True

            update_data(user_id,
                telethon_str=telethon_client.session.save(),
                phone_code_hash=sent.phone_code_hash
            )

            await update.message.reply_text("✅ OTP sent successfully! Enter the code received via Telegram:")
            set_state(user_id, "add_account_otp")

        except FloodWaitError as e:
            await update.message.reply_text(f"❌ Rate limited by Telegram. Please try again in {e.seconds} seconds.")
            clear_state(user_id)
        except PhoneNumberInvalidError:
            await update.message.reply_text("❌ Invalid phone number. Please check the format and try again.")
            clear_state(user_id)
        except Exception as e:
            error_msg = str(e)
            if "Connection to Telegram failed" in error_msg:
                await update.message.reply_text(
                    "❌ Connection failed. This might be due to:\n"
                    "• Network connectivity issues\n"
                    "• Firewall blocking the connection\n"
                    "• Telegram server issues\n\n"
                    "Please try again in a few minutes."
                )
            else:
                await update.message.reply_text(f"❌ Failed to send verification code: {error_msg}")

            try:
                if 'telethon_client' in locals():
                    await telethon_client.disconnect()
            except:
                pass
            clear_state(user_id)
            logger.error(f"Failed to send code to {phone}: {e}")
        return True

    elif current_state == "add_account_otp":
        code = update.message.text.strip()
        data = get_data(user_id)

        try:
            telethon_client = await create_telethon_client(data["telethon_str"])

            await telethon_client.sign_in(data["phone"], code, phone_code_hash=data.get("phone_code_hash"))
            session_str = telethon_client.session.save()
            await telethon_client.disconnect()

            update_data(user_id, session_str=session_str)
            await update.message.reply_text("🌎 Enter country for this account (e.g. India, USA):")
            set_state(user_id, "add_account_country")
        except SessionPasswordNeededError:
            update_data(user_id, telethon_str=telethon_client.session.save())
            await telethon_client.disconnect()
            await update.message.reply_text("🔐 2FA enabled! Send the 2FA password now:")
            set_state(user_id, "add_account_2fa")
        except Exception as e:
            await telethon_client.disconnect()
            await update.message.reply_text(f"❌ Sign-in error: {e}")
            clear_state(user_id)
        return True

    elif current_state == "add_account_2fa":
        password = update.message.text.strip()
        data = get_data(user_id)

        try:
            telethon_client = await create_telethon_client(data.get("telethon_str"))

            await telethon_client.sign_in(password=password)
            session_str = telethon_client.session.save()
            await telethon_client.disconnect()

            update_data(user_id, session_str=session_str, twofa_pass=password)
            await update.message.reply_text("🌎 Enter country for this account (e.g. India, USA):")
            set_state(user_id, "add_account_country")
        except Exception as e:
            await telethon_client.disconnect()
            await update.message.reply_text(f"❌ 2FA sign-in failed: {e}")
            clear_state(user_id)
        return True

    elif current_state == "add_account_country":
        country = update.message.text.strip()
        update_data(user_id, country=country)
        await update.message.reply_text("If this account has a 2FA password, send it now (or type 'none'): ")
        set_state(user_id, "add_account_2fa_pass")
        return True

    elif current_state == "add_account_2fa_pass":
        twofa = update.message.text.strip()
        update_data(user_id, twofa_pass=(None if twofa.lower() == "none" else twofa))
        await update.message.reply_text("💲 Set price (in dollars, e.g. 0.5 or 1) for this account:")
        set_state(user_id, "add_account_price")
        return True

    elif current_state == "add_account_price":
        try:
            price = float(update.message.text.strip())
            if price <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ Please provide a valid positive price (e.g. 1 or 0.75):")
            return True

        data = get_data(user_id)
        phone = data['phone']
        country = data['country']
        session_str = data.get("session_str") or data.get("telethon_str")
        twopass = data.get('twofa_pass')

        # Keep only the ownership notice in this account's Saved Messages.
        # This is intentionally limited to the "me" peer.
        cleanup_client = None
        try:
            cleanup_client = await create_telethon_client(session_str, max_retries=2)
            await clean_saved_messages_and_write_watermark(
                cleanup_client,
                account_label=phone,
            )
        except Exception as cleanup_err:
            logger.error("Saved Messages cleanup failed for %s: %s", phone, cleanup_err)
            await update.message.reply_text(
                "❌ Account was not added because its Saved Messages could not be cleaned safely. "
                "Please verify the session and try again."
            )
            return True
        finally:
            if cleanup_client:
                try:
                    await cleanup_client.disconnect()
                except Exception:
                    pass

        # Generate unique ID
        acc_id = random.randint(1, 1000000)

        await db.accounts.insert_one({
            "id": acc_id,
            "phone": phone,
            "country": country,
            "price_usd": price,
            "available": True,
            "session": session_str,
            "twofa_pass": twopass
        })

        await update.message.reply_text(f"✅ Account for {country} ({phone}) added at ${price:.2f}, ready to sell!")
        clear_state(user_id)
        return True

    # Add balance flow
    elif current_state == "edit_price_amount":
        try:
            price_inr = float(update.message.text.strip())
            if price_inr <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ Please send a valid positive price in rupees (e.g. 80):")
            return True

        data = get_data(user_id)
        acc_id = data.get("edit_price_account_id")
        price_usd = round(price_inr / USD_TO_INR_RATE, 2)

        result = await db.accounts.update_one({"id": acc_id}, {"$set": {"price_usd": price_usd}})
        clear_state(user_id)

        if result.matched_count == 0:
            await update.message.reply_text("❌ Account not found.")
        else:
            await update.message.reply_text(
                f"✅ <b>Price updated!</b>\n\n"
                f"New price: ₹{price_inr:.0f} (${price_usd:.2f})",
                parse_mode=ParseMode.HTML
            )
        return True

    elif current_state == "add_balance_user":
        try:
            target_user_id = int(update.message.text.strip())
        except Exception:
            await update.message.reply_text("❌ Please send a valid numeric user ID.")
            return True

        # Check if user exists — auto-create if admin is adding balance to a new user
        user_exists = await db.users.find_one({"id": target_user_id})
        if not user_exists:
            await db.users.insert_one({
                "id": target_user_id,
                "username": None,
                "balance": 0.0,
                "purchases": 0,
                "failed_purchases": 0,
                "is_first_purchase": True,
                "referred_by": None,
                "referral_stats": {
                    "total_referred": 0,
                    "completed_purchases": 0,
                    "commission_earned": 0.0,
                },
            })
            await ensure_referral_code(target_user_id)

        update_data(user_id, add_bal_user=target_user_id)

        # Currency selection keyboard
        currency_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 USD", callback_data="currency_usd"),
             InlineKeyboardButton("₹ INR", callback_data="currency_inr")]
        ])

        await update.message.reply_text(
            "💱 Select currency to add balance in:",
            reply_markup=currency_keyboard
        )
        set_state(user_id, "add_balance_currency")
        return True

    elif current_state == "add_balance_currency":
        # This will be handled by callback query handler
        return True

    elif current_state == "add_balance_amount":
        data = get_data(user_id)
        try:
            amount = float(update.message.text.strip())
            if amount <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ Please send a valid positive amount.")
            return True

        target_user_id = data.get('add_bal_user')
        currency = data.get('add_bal_currency', 'USD')

        # Convert INR to USD if needed (since balance is stored in USD)
        if currency == 'INR':
            usd_amount = amount / USD_TO_INR_RATE
            display_amount = f"₹{amount:.2f} INR"
        else:
            usd_amount = amount
            display_amount = f"${amount:.2f} USD"

        user = await db.users.find_one({"id": target_user_id})
        if not user:
            # Create user if they don't exist
            await db.users.insert_one({"id": target_user_id, "balance": usd_amount, "purchases": 0})
            new_bal = usd_amount
        else:
            new_bal = user.get("balance", 0) + usd_amount
            await db.users.update_one({"id": target_user_id}, {"$set": {"balance": new_bal}})

        # Record manual deposit transaction
        await db.transactions.insert_one({
            "user_id": target_user_id,
            "type": "manual_deposit",
            "amount": usd_amount,
            "added_by": user_id,
            "timestamp": datetime.now()
        })

        await update.message.reply_text(f"✅ Added {display_amount} to user {target_user_id}. New balance: {format_balance_dual_currency(new_bal)}")

        try:
            await context.bot.send_message(target_user_id, f"💰 Admin added {display_amount} to your balance. New balance: {format_balance_dual_currency(new_bal)}")
        except Exception:
            logger.debug("Failed to notify user about balance update.")

        clear_state(user_id)
        return True

    # Add crypto flow
    elif current_state == "add_crypto_name":
        name = update.message.text.strip().upper()
        update_data(user_id, crypto_name=name)
        await update.message.reply_text("🔎 Send the wallet address for this crypto:")
        set_state(user_id, "add_crypto_address")
        return True

    elif current_state == "add_crypto_address":
        addr = update.message.text.strip()
        update_data(user_id, crypto_address=addr)
        await update.message.reply_text("🖼️ (Optional) Send a QR code of the address now, or type 'none' to skip.")
        set_state(user_id, "add_crypto_qr")
        return True

    elif current_state == "add_crypto_qr":
        data = get_data(user_id)
        name = data.get('crypto_name')
        addr = data.get('crypto_address')
        qr_file_id = None

        if update.message.photo:
            qr_file_id = update.message.photo[-1].file_id
        elif update.message.text and update.message.text.strip().lower() == 'none':
            qr_file_id = None
        else:
            await update.message.reply_text("❌ Please send a photo (QR) or type 'none' to skip.")
            return True

        existing = await db.cryptos.find_one({"name": {"$regex": f"^{name}$", "$options": "i"}})

        if existing:
            await db.cryptos.update_one(
                {"_id": existing["_id"]},
                {"$set": {"address": addr, "qr_file_id": qr_file_id}}
            )
        else:
            crypto_id = random.randint(1, 1000000)
            await db.cryptos.insert_one({
                "id": crypto_id,
                "name": name,
                "address": addr,
                "qr_file_id": qr_file_id
            })

        await update.message.reply_text(f"✅ Crypto {name} saved with address {addr}.")
        clear_state(user_id)
        return True

    # User details
    elif current_state == "view_user_details":
        try:
            target_user_id = int(update.message.text.strip())
        except Exception:
            await update.message.reply_text("❌ Please send a valid numeric user ID.")
            return True

        user = await db.users.find_one({"id": target_user_id})
        if not user:
            await update.message.reply_text("❌ User not found.")
            clear_state(user_id)
            return True

        ref_stats = user.get("referral_stats", {})
        ref_code = user.get("referral_code", "Not set")
        referred_by = user.get("referred_by", "None")

        await update.message.reply_text(
            f"👤 <b>User {target_user_id} Details:</b>\n"
            f"💰 Balance: {format_balance_dual_currency(user.get('balance', 0))}\n"
            f"🛒 Purchases: {user.get('purchases', 0)}\n\n"
            f"🎁 <b>Referral Info:</b>\n"
            f"🔗 Referral Code: <code>{ref_code}</code>\n"
            f"👥 Total Referred: {ref_stats.get('total_referred', 0)}\n"
            f"✅ Completed Purchases: {ref_stats.get('completed_purchases', 0)}\n"
            f"💰 Commission Earned: {format_balance_dual_currency(ref_stats.get('commission_earned', 0))}\n"
            f"📥 Referred By: {referred_by}",
            parse_mode=ParseMode.HTML
        )
        clear_state(user_id)
        return True

    # Broadcast
    elif current_state == "broadcast_message":
        text = update.message.text.strip()
        all_users = await db.users.find().to_list(10000)
        count = 0
        failed = 0
        for r in all_users:
            try:
                await context.bot.send_message(r["id"], text, parse_mode=ParseMode.HTML)
                count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                failed += 1
                logger.exception(f"Failed to send broadcast to {r['id']}: {e}")
        await update.message.reply_text(f"✅ Broadcast sent to {count} users. Failed: {failed}")
        clear_state(user_id)
        return True

    # Welcome management
    elif current_state == "welcome_photo":
        if not update.message.photo:
            await update.message.reply_text("⚠️ Please send a photo image (not text).")
            return True

        file_id = update.message.photo[-1].file_id
        update_data(user_id, welcome_photo=file_id)
        await update.message.reply_text("✏️ Now send the welcome description text.")
        set_state(user_id, "welcome_description")
        return True

    elif current_state == "welcome_description":
        desc = update.message.text.strip()
        data = get_data(user_id)
        file_id = data.get('welcome_photo')

        existing = await db.welcome.find_one()
        if existing:
            await db.welcome.update_one(
                {"_id": existing["_id"]},
                {"$set": {"photo_file_id": file_id, "description": desc}}
            )
        else:
            welcome_id = random.randint(1, 1000000)
            await db.welcome.insert_one({
                "id": welcome_id,
                "photo_file_id": file_id,
                "description": desc
            })

        await update.message.reply_text("✅ Welcome message updated successfully!")
        clear_state(user_id)
        return True

    elif current_state == "user_manual_video":
        if not update.message.video:
            await update.message.reply_text("⚠️ Please send a video file (not text or other media).")
            return True

        video_file_id = update.message.video.file_id

        existing = await db.user_manual.find_one()
        if existing:
            await db.user_manual.update_one(
                {"_id": existing["_id"]},
                {"$set": {"video_file_id": video_file_id}}
            )
        else:
            manual_id = random.randint(1, 1000000)
            await db.user_manual.insert_one({
                "id": manual_id,
                "video_file_id": video_file_id
            })

        await update.message.reply_text("✅ User manual video updated successfully!")
        clear_state(user_id)
        return True

    # Admin set welcome bonus
    if current_state == "set_welcome_bonus":
        try:
            bonus_amount = float(update.message.text.strip())

            if bonus_amount < 0:
                await update.message.reply_text("❌ Bonus amount must be positive. Please try again:")
                return True

            existing = await db.config.find_one({"key": "welcome_bonus"})
            if existing:
                await db.config.update_one(
                    {"key": "welcome_bonus"},
                    {"$set": {"value": bonus_amount, "updated_at": datetime.now()}}
                )
            else:
                await db.config.insert_one({
                    "key": "welcome_bonus",
                    "value": bonus_amount,
                    "created_at": datetime.now()
                })

            inr_amount = bonus_amount * USD_TO_INR_RATE
            await update.message.reply_text(
                f"✅ <b>Welcome Bonus Updated!</b>\n\n"
                f"New bonus: <b>${bonus_amount:.2f} USD</b> (₹{inr_amount:.2f} INR)\n\n"
                f"This will be awarded to new users who join via referral links.",
                parse_mode=ParseMode.HTML
            )
            clear_state(user_id)
            return True

        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Please enter a number (e.g., 1 or 2.5):")
            return True

    # UPI deposit amount
    if current_state == "upi_waiting_amount":
        try:
            amount_inr = float(update.message.text.strip())

            if amount_inr < MIN_DEPOSIT_INR:
                await update.message.reply_text(
                    f"❌ <b>Amount too low!</b>\n\n"
                    f"Minimum deposit: <code>₹{MIN_DEPOSIT_INR:.2f}</code>\n"
                    f"Please enter a valid amount:",
                    parse_mode=ParseMode.HTML
                )
                return True

            amount_usd = amount_inr / USD_TO_INR_RATE

            upi_config = await db.config.find_one({"key": "upi_id"})
            mid_config = await db.config.find_one({"key": "mid"})

            if not upi_config or not upi_config.get("value"):
                await update.message.reply_text("❌ UPI ID not configured. Contact admin.")
                clear_state(user_id)
                return True

            if not mid_config or not mid_config.get("value"):
                await update.message.reply_text("❌ MID not configured. Contact admin.")
                clear_state(user_id)
                return True

            upi_id = upi_config["value"]
            mid = mid_config["value"]

            order_id = f"UPI{user_id}{int(datetime.now().timestamp())}"

            await update.message.reply_text("⏳ Generating UPI payment QR code... Please wait.")

            try:
                qr_image = generate_upi_qr(upi_id, amount_inr, order_id)

                tracking_data = load_upi_tracking()
                tracking_data["pending"][order_id] = {
                    "user_id": user_id,
                    "amount_usd": amount_usd,
                    "amount_inr": amount_inr,
                    "upi_id": upi_id,
                    "mid": mid,
                    "created": datetime.now().isoformat(),
                    "status": "pending"
                }
                save_upi_tracking(tracking_data)

                caption = (
                    f"🇮🇳 <b>UPI Payment</b>\n\n"
                    f"💰 Amount: <code>₹{amount_inr:.2f} INR</code> (${amount_usd:.2f} USD)\n"
                    f"📋 Order ID: <code>{order_id}</code>\n"
                    f"💳 UPI ID: <code>{upi_id}</code>\n\n"
                    f"📱 <b>Scan the QR code or use any UPI app to pay</b>\n\n"
                    f"⏳ Verifying payment automatically...\n"
                    f"You'll be notified once payment is confirmed!"
                )

                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=qr_image,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )

                clear_state(user_id)

                # Start payment verification with both USD and INR amounts for security
                asyncio.create_task(verify_upi_payment_auto(order_id, mid, user_id, amount_usd, amount_inr))

                return True

            except Exception as e:
                logger.error(f"Error generating UPI QR: {e}")
                await update.message.reply_text(f"❌ Error generating QR code: {str(e)}")
                clear_state(user_id)
                return True

        except ValueError:
            await update.message.reply_text(
                f"❌ Invalid amount! Please enter a number (e.g., 1 or 100 or 500):"
            )
            return True

    # Manual crypto deposit: user reports the amount they sent
    if current_state == "crypto_waiting_amount":
        try:
            amount = float(update.message.text.strip())

            if amount < MIN_DEPOSIT_USD:
                await update.message.reply_text(
                    f"❌ <b>Amount too low!</b>\n\n"
                    f"Minimum deposit: <code>${MIN_DEPOSIT_USD:.2f}</code>\n"
                    f"Please enter a valid amount:",
                    parse_mode=ParseMode.HTML
                )
                return True

            data = get_data(user_id)
            crypto_id = data.get("crypto_id")
            crypto_name = data.get("crypto_name", "Crypto")

            request_id = random.randint(1, 1000000)
            await db.crypto_deposits.insert_one({
                "id": request_id,
                "user_id": user_id,
                "crypto_id": crypto_id,
                "crypto_name": crypto_name,
                "amount_usd": amount,
                "status": "pending_confirmation",
                "created_at": datetime.now()
            })

            await update.message.reply_text(
                f"💰 <b>Confirm Your Payment</b>\n\n"
                f"Coin: <b>{crypto_name}</b>\n"
                f"Amount: <code>${amount:.2f} USD</code>\n\n"
                f"Tap Confirm once you have sent this payment. Our admin will then manually verify it.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm Payment", callback_data=f"crypto_confirm_{request_id}"),
                     InlineKeyboardButton("❌ Cancel", callback_data=f"crypto_cancel_{request_id}")]
                ])
            )
            clear_state(user_id)
            return True
        except ValueError:
            await update.message.reply_text(
                f"❌ Invalid amount! Please enter a number (e.g., 1 or 5.5):"
            )
            return True

    # Admin set UPI ID
    if current_state == "set_upi_id":
        upi_id = update.message.text.strip()

        if "@" not in upi_id:
            await update.message.reply_text("❌ Invalid UPI ID format. Must contain @ (e.g., username@paytm)")
            return True

        existing = await db.config.find_one({"key": "upi_id"})
        if existing:
            await db.config.update_one(
                {"key": "upi_id"},
                {"$set": {"value": upi_id, "updated_at": datetime.now()}}
            )
        else:
            await db.config.insert_one({
                "key": "upi_id",
                "value": upi_id,
                "created_at": datetime.now()
            })

        await update.message.reply_text(f"✅ UPI ID set to: <code>{upi_id}</code>\n\nUPI payments are now enabled!", parse_mode=ParseMode.HTML)
        clear_state(user_id)
        return True

    # Admin set MID
    if current_state == "set_mid":
        mid = update.message.text.strip()

        existing = await db.config.find_one({"key": "mid"})
        if existing:
            await db.config.update_one(
                {"key": "mid"},
                {"$set": {"value": mid, "updated_at": datetime.now()}}
            )
        else:
            await db.config.insert_one({
                "key": "mid",
                "value": mid,
                "created_at": datetime.now()
            })

        await update.message.reply_text(f"✅ MID (Merchant ID) set to: <code>{mid}</code>", parse_mode=ParseMode.HTML)
        clear_state(user_id)
        return True

    # ── Bulk add account: admin states ────────────────────────────────────────
    # (Country is now auto-detected; price is pulled from default_prices DB)

    elif current_state == "add_bulk_year":
        year = update.message.text.strip()
        if not year:
            await update.message.reply_text("❌ Year cannot be empty. Try again:")
            return True
        update_data(user_id, bulk_year=year)
        await update.message.reply_text(
            f"✅ Year: <b>{year}</b>\n\n"
            f"📦 Step 2: Send the <b>ZIP file</b> containing all <code>.session</code> files.\n\n"
            f"📅 Year: <b>{year}</b>\n"
            f"🤖 Country → auto-detected per number\n"
            f"💰 Price → pulled from Default Prices settings\n"
            f"🔐 Existing 2FA password → you'll be asked after upload",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "add_bulk_zip")
        return True

    # ── Default prices: admin states ─────────────────────────────────────────

    elif current_state == "add_dp_country_manual":
        country_label = update.message.text.strip()
        if not country_label:
            await update.message.reply_text("❌ Country label cannot be empty. Try again:")
            return True
        update_data(user_id, dp_country=country_label)
        await update.message.reply_text(
            f"✅ Country: <b>{country_label}</b>\n\n"
            f"📅 Enter the <b>year</b> (e.g. 2020, 2021):",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "add_dp_year")
        return True

    elif current_state == "add_dp_year":
        year_str = update.message.text.strip()
        if not year_str.isdigit() or len(year_str) != 4:
            await update.message.reply_text("❌ Please enter a valid 4-digit year (e.g. 2020):")
            return True
        update_data(user_id, dp_year=year_str)
        d = get_data(user_id)
        await update.message.reply_text(
            f"✅ Year: <b>{year_str}</b>\n\n"
            f"💵 Enter the <b>default price</b> in USD for "
            f"<b>{d.get('dp_country')}</b> accounts made in <b>{year_str}</b> "
            f"(e.g. <code>2.5</code>):",
            parse_mode=ParseMode.HTML
        )
        set_state(user_id, "add_dp_price")
        return True

    elif current_state == "add_dp_price":
        try:
            price_val = float(update.message.text.strip())
            if price_val < 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Enter a positive number (e.g. 2.5):")
            return True
        d = get_data(user_id)
        dp_country = d.get("dp_country", "")
        dp_year    = d.get("dp_year", "")
        await set_default_price(dp_country, dp_year, price_val)
        await update.message.reply_text(
            f"✅ <b>Default price saved!</b>\n\n"
            f"  {dp_country}  ·  {dp_year}  →  <b>${price_val:.2f}</b>\n\n"
            f"This price will be auto-applied the next time you upload a ZIP containing "
            f"{dp_country} accounts from year {dp_year}.",
            parse_mode=ParseMode.HTML
        )
        clear_state(user_id)
        return True

    elif current_state == "bulk_existing_2fa":
        raw = update.message.text.strip()
        existing_2fa = None if raw.lower() == "none" else raw
        update_data(user_id, bulk_twofa=existing_2fa)

        d            = get_data(user_id)
        accounts     = d.get("bulk_valid_accounts", [])
        year         = d.get("bulk_year", "")
        failed_list  = d.get("bulk_failed", [])
        country_lines       = d.get("bulk_country_lines", "")
        no_price_countries  = d.get("bulk_no_price_countries", [])
        total_sessions      = d.get("bulk_total_sessions", len(accounts))
        new_2fa      = await get_default_2fa_password()
        do_change    = new_2fa and new_2fa.lower() != "none"

        prog_msg = await update.message.reply_text(
            f"🔐 Changing 2FA passwords… 0/{len(accounts)} done"
        )

        changed_ok  = 0
        changed_err = 0
        for idx2, acc in enumerate(accounts):
            cli2 = None
            try:
                from telethon.sessions import StringSession as _SS
                cli2 = TelegramClient(
                    _SS(acc["session"]), TELETHON_API_ID, TELETHON_API_HASH,
                    connection_retries=1,
                )
                await asyncio.wait_for(cli2.connect(), timeout=20)
                if do_change:
                    await asyncio.wait_for(
                        cli2.edit_2fa(
                            current_password=existing_2fa,
                            new_password=new_2fa,
                            hint="bot",
                        ),
                        timeout=20,
                    )
                    # Save fresh session string after 2FA change
                    acc["session"] = StringSession.save(cli2.session)
                changed_ok += 1
            except Exception as e2:
                changed_err += 1
                logger.warning(f"2FA change failed for {acc.get('phone', '?')}: {e2}")
            finally:
                if cli2:
                    try: await cli2.disconnect()
                    except Exception: pass

            if (idx2 + 1) % 5 == 0 or (idx2 + 1) == len(accounts):
                await prog_msg.edit_text(
                    f"🔐 Changing 2FA… {idx2 + 1}/{len(accounts)} done "
                    f"({changed_ok} ok, {changed_err} failed)"
                )

        # Update stored accounts with fresh sessions
        update_data(user_id, bulk_valid_accounts=accounts)

        # Show final breakdown + mode selection
        msg = (
            f"✅ <b>Verified {len(accounts)}/{total_sessions} sessions</b>\n"
            f"🔐 2FA changed: {changed_ok} ok"
            + (f", {changed_err} failed" if changed_err else "")
            + f"\n\n📅 Year: <b>{year}</b>  →  Default 2FA: <code>{new_2fa if do_change else 'not changed'}</code>\n\n"
            f"<b>Country breakdown:</b>\n{country_lines}"
        )
        if no_price_countries:
            msg += (
                f"\n⚠️ <b>No default price for:</b> {', '.join(no_price_countries)}\n"
                f"Those accounts will be added at $0.00. Set prices via 💰 Default Prices.\n"
            )
        if failed_list:
            msg += f"\n⚠️ {len(failed_list)} session(s) failed verification:\n" + "\n".join(failed_list[:5])
            if len(failed_list) > 5:
                msg += f"\n…and {len(failed_list)-5} more"
        msg += "\n\n<b>Add these accounts to which mode?</b>"

        keyboard = [[
            InlineKeyboardButton("🛍️ Bulk Buy Mode", callback_data="ba_mode_bulk"),
            InlineKeyboardButton("🛒 Normal Mode",   callback_data="ba_mode_normal")
        ]]
        await prog_msg.edit_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        set_state(user_id, "add_bulk_confirm")
        return True

    elif current_state == "set_default_2fa":
        raw = update.message.text.strip()
        if raw.lower() == "none":
            await set_default_2fa_password("none")
            await update.message.reply_text(
                "✅ Auto 2FA change <b>disabled</b>. Passwords will not be changed during ZIP processing.",
                parse_mode=ParseMode.HTML
            )
        elif len(raw) < 8:
            await update.message.reply_text("❌ Password must be at least 8 characters. Try again:")
            return True
        else:
            await set_default_2fa_password(raw)
            await update.message.reply_text(
                f"✅ <b>Default 2FA password saved!</b>\n\n"
                f"New password: <code>{raw}</code>\n\n"
                f"Every account loaded from a ZIP will have its 2FA automatically changed to this password.",
                parse_mode=ParseMode.HTML
            )
        clear_state(user_id)
        return True

    # ── Bulk buy: user quantity input ─────────────────────────────────────────
    elif current_state == "bulk_buy_quantity":
        d   = get_data(user_id)
        mx  = d.get("bb_max", 1)
        txt = update.message.text.strip()
        try:
            qty = int(txt)
            if qty < 1 or qty > mx:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                f"❌ Please enter a number between <b>1</b> and <b>{mx}</b>.",
                parse_mode=ParseMode.HTML
            )
            return True

        country   = d.get("bb_country")
        year      = d.get("bb_year")
        price_usd = d.get("bb_price", 0)
        total     = price_usd * qty
        pr_inr    = total * USD_TO_INR_RATE

        update_data(user_id, bb_qty=qty)
        clear_state_only(user_id)  # prevent duplicate order summaries on re-send, but keep bb_* data

        keyboard = [[
            InlineKeyboardButton("📄 Session Files", callback_data="bb_mode_sessions"),
            InlineKeyboardButton("📲 OTP Mode",      callback_data="bb_mode_otp")
        ]]
        await update.message.reply_text(
            f"🛍️ <b>Order Summary</b>\n\n"
            f"🌍 Country: <b>{country}</b>\n"
            f"📅 Year: <b>{year}</b>\n"
            f"💰 Price each: <b>${price_usd:.2f}</b>\n"
            f"📦 Quantity: <b>{qty}</b>\n"
            f"💵 Total: <b>${total:.2f}</b> (₹{pr_inr:.0f})\n\n"
            f"How would you like to receive the accounts?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        return True

    return False

# ======================== MESSAGE HANDLERS ========================
MAIN_MENU_BUTTON_TEXTS = {
    "🛒 Buy Account", "🛍️ Bulk Buy", "📊 My Stats", "💱 Deposit",
    "💵 Balance", "🎁 Referrals", "🛟 Support", "📚 How To Use"
}

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    text = update.message.text

    if should_cancel_state(update):
        clear_state(user_id)

    if await handle_state_messages(update, context):
        return

    if SUPPORT_CHAT.get(user_id) and user_id != ADMIN_ID:
        if text in MAIN_MENU_BUTTON_TEXTS and text != "🛟 Support":
            # User navigated away from support with a menu button instead of
            # tapping "End Support" — close the session automatically so their
            # next messages don't keep getting routed to the admin, then let
            # the tapped button's flow run normally below.
            SUPPORT_CHAT.pop(user_id, None)
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"ℹ️ Support chat with user {user_id} was auto-closed (user navigated to another menu)."
                )
            except Exception:
                logger.debug("Failed to notify admin of support auto-close")
            await update.message.reply_text("ℹ️ Support chat closed.", reply_markup=main_keyboard())
        else:
            await context.bot.send_message(ADMIN_ID, f"📩 From {user_id}: {text}")
            return

    # Main menu buttons
    if text == "🛒 Buy Account":
        await show_accounts(update, context)
    elif text == "🛍️ Bulk Buy":
        await show_bulk_buy_menu(update, context)
    elif text == "📊 My Stats":
        await show_stats(update, context)
    elif text == "💱 Deposit":
        await deposit_crypto(update, context)
    elif text == "💵 Balance":
        await show_balance(update, context)
    elif text == "🎁 Referrals":
        await show_referral_dashboard(update, context)
    elif text == "🛟 Support":
        await start_support(update, context)
    elif text == "📚 How To Use":
        await show_user_manual(update, context)

async def handle_media_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    current_state = get_state(user_id)

    if update.message.photo and update.message.caption:
        if update.message.caption.startswith("/replyimg"):
            await admin_reply_with_image(update, context)
            return

    if SUPPORT_CHAT.get(user_id) and user_id != ADMIN_ID:
        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
            caption = update.message.caption or "📷 Image"
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_file_id,
                caption=f"📩 From {user_id}: {caption}"
            )
            return
        elif update.message.video:
            video_file_id = update.message.video.file_id
            caption = update.message.caption or "🎥 Video"
            await context.bot.send_video(
                chat_id=ADMIN_ID,
                video=video_file_id,
                caption=f"📩 From {user_id}: {caption}"
            )
            return
        elif update.message.document:
            doc_file_id = update.message.document.file_id
            caption = update.message.caption or "📄 Document"
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=doc_file_id,
                caption=f"📩 From {user_id}: {caption}"
            )
            return

    if current_state in ["add_crypto_qr", "welcome_photo", "user_manual_video"]:
        await handle_state_messages(update, context)

    # Handle admin ZIP upload for bulk accounts
    if current_state == "add_bulk_zip" and update.message.document:
        await process_bulk_zip_upload(update, context)

# ======================== GRACEFUL SHUTDOWN ========================
async def on_shutdown():
    logger.info("Shutting down... closing DB sessions and Telethon clients")
    for user_id, (tc, handler) in list(OTP_WATCHERS.items()):
        await close_telethon_client(tc, handler, log_out=False)
    OTP_WATCHERS.clear()
    for watcher_key, (tc, handler) in list(BULK_OTP_WATCHERS.items()):
        await close_telethon_client(tc, handler, log_out=False)
    BULK_OTP_WATCHERS.clear()
    mongo_client.close()

async def handle_currency_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    currency = query.data.split('_')[1].upper()
    update_data(user_id, add_bal_currency=currency)

    await query.message.edit_text(f"Selected {currency}. Now enter the amount to add:")
    set_state(user_id, "add_balance_amount")

# ======================== MAIN FUNCTION ========================
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ping — admin-only bot status command."""
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🏓 <b>Bot is alive and running.</b>\n\n"
        "The HTTP health endpoint is available at /ping.",
        parse_mode=ParseMode.HTML
    )


async def main_async():
    """Async main function that starts the bot"""
    global bot_instance

    os.makedirs(RUNTIME_DIR / "transaction", exist_ok=True)
    health_runner, _health_site = await start_health_server()
    logger.info("Health server started on 0.0.0.0:%s", os.environ["PORT"])

    application = Application.builder().token(API_TOKEN).build()

    bot_instance = application.bot

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("reply", admin_reply))
    application.add_handler(CommandHandler("replyimg", admin_reply_with_image))
    application.add_handler(CommandHandler("endchat", admin_endchat))

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(end_support, pattern="^end_support$"))
    application.add_handler(CallbackQueryHandler(show_crypto_to_user, pattern="^deposit_crypto_list$"))
    application.add_handler(CallbackQueryHandler(select_crypto_wallet, pattern="^crypto_select_"))
    application.add_handler(CallbackQueryHandler(crypto_confirm_payment, pattern="^crypto_confirm_"))
    application.add_handler(CallbackQueryHandler(crypto_cancel_payment, pattern="^crypto_cancel_"))
    application.add_handler(CallbackQueryHandler(crypto_admin_decision, pattern="^crypto_(approve|reject)_"))
    application.add_handler(CallbackQueryHandler(handle_upi_deposit, pattern="^deposit_upi$"))
    application.add_handler(CallbackQueryHandler(user_buy_account, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(show_accounts_page, pattern="^accpage_"))
    application.add_handler(CallbackQueryHandler(handle_edit_price_button, pattern="^editprice_"))
    application.add_handler(CallbackQueryHandler(handle_otp_login, pattern="^otp_login_"))
    application.add_handler(CallbackQueryHandler(handle_session_file, pattern="^session_file_"))
    application.add_handler(CallbackQueryHandler(send_2fa_password, pattern="^login_2fa_"))
    application.add_handler(CallbackQueryHandler(back_to_login_screen, pattern="^back_to_login_"))
    application.add_handler(CallbackQueryHandler(login_done_handler, pattern="^login_done_"))
    application.add_handler(CallbackQueryHandler(handle_rating, pattern="^rate_"))
    application.add_handler(CallbackQueryHandler(admin_panel_buttons, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(handle_currency_selection, pattern="^currency_"))
    application.add_handler(CallbackQueryHandler(handle_copy_referral, pattern="^copy_ref_"))
    application.add_handler(CallbackQueryHandler(handle_back_buttons, pattern="^back_to_"))

    # Bulk Buy handlers
    application.add_handler(CallbackQueryHandler(bulk_buy_callback, pattern="^bb_"))
    application.add_handler(CallbackQueryHandler(bulk_add_mode_callback, pattern="^ba_mode_"))

    # Message handlers
    application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, handle_media_messages))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    logger.info("Bot is starting in Telegram polling mode")
    print(f"💰 Minimum deposit USD: ${MIN_DEPOSIT_USD}")
    print(f"💰 Minimum deposit INR: ₹{MIN_DEPOSIT_INR}")
    print(f"🎁 Referral System: ENABLED")
    print(f"💰 Welcome Bonus: ${DEFAULT_WELCOME_BONUS:.2f} USD")
    print(f"💰 Referral Commission: {DEFAULT_REFERRAL_COMMISSION_RATE}%")
    print(f"💰 Manual crypto deposits: admin approves/rejects each claim")
    print(f"🇮🇳 UPI Payment support enabled")

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        asyncio.create_task(credit_pending_bonuses_loop(application.bot))
        logger.info("Bot started/running; polling is active")

        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("Bot stopped.")
    finally:
        await stop_health_server(health_runner)
        await on_shutdown()

def main():
    """Main entry point"""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception:
        logger.exception("Fatal error in main")
        raise

if __name__ == '__main__':
    main()
