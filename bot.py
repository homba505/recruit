"""
recruiter_driver_bot_final.py
=================================

Fully integrated, cleaned, and feature-complete Telegram bot for CDL driver recruiting.
Implements ALL requirements you specified:

1. Recruiter login (username + 5-letter password).
2. Guided driver submission form (Name, Phone, Experience, Escrow?, Ready Date, Media).
3. Send to selected company group (or Waiting Line group).
4. Reply routing: replies in the company group are forwarded to the submitting recruiter.
5. Admin (HOMBA) notified on every submission.
6. Per-driver **Ask for Update** ("Any Updates?") reminder posted in the company group.
7. Recruiter name included in every submitted driver message.
8. /menu inline dashboard: Submit, My Drivers, Check Updates, Logout.
9. Admin status marking (Hired / Rejected / Waiting) reflected in recruiter views.
10. Smart reply keyword parser ("call", "interested", "reject", etc.) -> recruiter alert & optional status change.
11. Recruiter My Drivers listing with status + date + View Details button.
12. Repost request (from Waiting Line driver to another company) -> admin approval workflow.
13. Driver status timeline in View Details.
14. 30‑minute session timeout; /logout ends session.

---------------------------------
SECURITY NOTES
---------------------------------
• DO **NOT** leave your real bot token in public code. Use env var TELEGRAM_BOT_TOKEN or paste privately.
• Passwords shown here are from your spec. Change if exposed.
• In-memory storage only (volatile). Use DB for production durability.

---------------------------------
DEPENDENCIES
---------------------------------
python-telegram-bot >= 21.6

Install/update:
    pip install --upgrade python-telegram-bot

Run:
    export TELEGRAM_BOT_TOKEN="123456:ABC..."   # or set in Windows env
    python recruiter_driver_bot_final.py

---------------------------------
CONFIG FROM YOUR SPEC (edit below if needed)
---------------------------------
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    constants,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
# ---- DB init: create tables if missing ----
from db import engine
from db_models import Base

async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# run once at startup
asyncio.run(_init_db())
# -------------------------------------------

# ---------------------------------------------------------------------------
# CONFIG (edit to match your environment) ----------------------------------
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("⚠️ TELEGRAM_BOT_TOKEN is not set in environment")


# Recruiters + passwords (5 letters)
RECRUITERS: Dict[str, str] = {
    "Tamik": "flame",
    "Hall": "boost",
    "Martin": "slice",
    "Oliver": "grape",
}

# Admin credential
ADMIN_USERNAME = "HOMBA"
ADMIN_PASSWORD = "fayzo2008"  # <-- change if compromised

# Chat IDs (negative for supergroups)
WAITING_LINE_GROUP_ID = -1002700129917  # your recruiter/waiting line group
COMPANY_GROUPS: Dict[str, int] = {
    "KURS LLC": -4988473775,
    "BRIAR ROSE": -4823075497,
    "Bravo transportation": -4818943980,
    "Waiting Line": WAITING_LINE_GROUP_ID,  # keep last so menu shows it last (optional)
}

# Which chat receives admin notifications? Using Waiting Line group per spec.
ADMIN_NOTIFY_CHAT_ID = WAITING_LINE_GROUP_ID

# Default statuses admin can set manually
ADMIN_STATUS_CHOICES = ["Hired", "Rejected", "Waiting"]

# Keywords -> status hints (smart parser)
SMART_KEYWORDS = {
    "call": "Replied",
    "call me": "Replied",
    "interested": "Interested",
    "hire": "Hired",
    "hired": "Hired",
    "reject": "Rejected",
    "rejected": "Rejected",
    "no": "Rejected",
    "pass": "Rejected",
}

# Session timeout seconds
SESSION_TIMEOUT = 1800  # 30 minutes

# ---------------------------------------------------------------------------
# STATE CONSTANTS (Conversation steps) --------------------------------------
# ---------------------------------------------------------------------------

LOGIN_NAME, LOGIN_PASS, FORM_NAME, FORM_PHONE, FORM_EXP, FORM_ESCROW, FORM_READY, FORM_MEDIA, FORM_COMPANY = range(9)

# ---------------------------------------------------------------------------
# STORAGE STRUCTURES --------------------------------------------------------
# ---------------------------------------------------------------------------



logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HELPERS -------------------------------------------------------------------
# ---------------------------------------------------------------------------

def is_logged_in(user_id: int) -> bool:
    info = sessions.get(user_id)
    if not info:
        return False
    return (time.time() - info["last_active"]) < SESSION_TIMEOUT


def logout_user(user_id: int) -> None:
    sessions.pop(user_id, None)


def update_activity(user_id: int) -> None:
    if user_id in sessions:
        sessions[user_id]["last_active"] = time.time()


def _new_driver_record(**kwargs) -> Dict[str, Any]:
    """Create a new driver record dict with standard fields."""
    now = datetime.now()
    rec = {
        "id": kwargs.get("id"),
        "name": kwargs.get("name", ""),
        "phone": kwargs.get("phone", ""),
        "experience": kwargs.get("experience", ""),
        "escrow": kwargs.get("escrow", ""),
        "ready": kwargs.get("ready", ""),
        "file_type": kwargs.get("file_type"),
        "file_id": kwargs.get("file_id"),
        "recruiter_id": kwargs.get("recruiter_id"),
        "recruiter_name": kwargs.get("recruiter_name"),
        "company_name": kwargs.get("company_name", ""),
        "group_id": kwargs.get("group_id"),
        "group_msg_id": kwargs.get("group_msg_id"),
        "parent_id": kwargs.get("parent_id"),
        "status": kwargs.get("status", "Sent"),
        # timeline: list of dicts {ts, status, actor}
        "history": kwargs.get("history", [{"ts": now, "status": "Sent", "actor": kwargs.get("recruiter_name")}]),
        # replies: {from, text, ts, read: bool}
        "replies": [],
        # unread reply count for recruiter
        "unread": 0,
    }
    return rec


def _fmt_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def _driver_summary_line(rec: Dict[str, Any]) -> str:
    return f"{rec['name']} ({rec['phone']}) — {rec['status']} [{rec['company_name']}] on {_fmt_ts(rec['history'][0]['ts'])}"


def _driver_timeline_md(rec: Dict[str, Any]) -> str:
    lines = [f"**{rec['name']}**", f"Phone: {rec['phone']}", f"Company: {rec['company_name']}", "", "**Status Timeline:**"]
    for item in rec["history"]:
        lines.append(f"• {_fmt_ts(item['ts'])}: {item['status']} (by {item['actor']})")
    if rec["replies"]:
        lines.append("\n**Replies:**")
        for r in rec["replies"][-10:]:
            lines.append(f"• {_fmt_ts(r['ts'])} {r['from']}: {r['text']}")
    return "\n".join(lines)


def _build_driver_detail_keyboard(driver_id: int, can_repost: bool = True) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔄 Any Updates?", callback_data=f"askupd|{driver_id}")],
    ]
    if can_repost:
        buttons.append([InlineKeyboardButton("📤 Repost", callback_data=f"repost|{driver_id}")])
    buttons.append([InlineKeyboardButton("❌ Close", callback_data=f"close|{driver_id}")])
    return InlineKeyboardMarkup(buttons)


def _build_company_choice_keyboard(prefix: str, exclude_group_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = []
    for name, gid in COMPANY_GROUPS.items():
        if exclude_group_id is not None and gid == exclude_group_id:
            continue
        rows.append([InlineKeyboardButton(name, callback_data=f"{prefix}|{name}")])
    return InlineKeyboardMarkup(rows)


def _build_admin_status_keyboard(driver_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(s, callback_data=f"admstat|{driver_id}|{s}")] for s in ADMIN_STATUS_CHOICES]
    return InlineKeyboardMarkup(rows)


def _smart_parse_status(text: str) -> Optional[str]:
    t = text.lower()
    for k, status in SMART_KEYWORDS.items():
        if k in t:
            return status
    return None


# ---------------------------------------------------------------------------
# AUTH FLOW -----------------------------------------------------------------
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Welcome. Please enter your username:")
    return LOGIN_NAME


async def login_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    context.user_data["login_name"] = name
    await update.message.reply_text("Enter password:")
    return LOGIN_PASS


async def login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    name = context.user_data.get("login_name", "")
    user_id = update.effective_user.id

    # Admin
    if name == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        sessions[user_id] = {"name": name, "admin": True, "last_active": time.time()}
        await update.message.reply_text("Welcome ADMIN. Type /panel to view admin panel.")
        return ConversationHandler.END

    # Recruiter
    if name in RECRUITERS and password == RECRUITERS[name]:
        sessions[user_id] = {"name": name, "admin": False, "last_active": time.time()}
        await update.message.reply_text(f"Welcome {name}! Type /menu to continue.")
        return ConversationHandler.END

    # Fail
    await update.message.reply_text("Invalid credentials. Use /start to try again.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# MENU ----------------------------------------------------------------------
# ---------------------------------------------------------------------------

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_logged_in(user_id):
        await update.message.reply_text("Session expired. Use /start to login again.")
        return
    update_activity(user_id)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Submit Driver", callback_data="menu|submit")],
        [InlineKeyboardButton("📋 My Drivers", callback_data="menu|my")],
        [InlineKeyboardButton("🔄 Check Updates", callback_data="menu|updates")],
        [InlineKeyboardButton("🚪 Logout", callback_data="menu|logout")],
    ])
    await update.message.reply_text("Choose an option:", reply_markup=kb)


async def menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not is_logged_in(user_id):
        await query.message.reply_text("Session expired. Use /start.")
        return ConversationHandler.END
    update_activity(user_id)

    action = query.data.split("|", 1)[1]

    if action == "submit":
        form_buffers[user_id] = {}
        await query.message.reply_text("Enter driver's full name:")
        return FORM_NAME

    if action == "my":
        await show_my_drivers(query, context)
        return ConversationHandler.END

    if action == "updates":
        await check_updates_for_recruiter(query, context)
        return ConversationHandler.END

    if action == "logout":
        logout_user(user_id)
        await query.message.reply_text("You’ve been logged out. Use /start to log in again.")
        return ConversationHandler.END

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# CHECK UPDATES (aggregated unread replies across all drivers) --------------
# ---------------------------------------------------------------------------

async def check_updates_for_recruiter(src, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unread replies for this recruiter across all drivers; mark them read."""
    user_id = src.from_user.id if hasattr(src, "from_user") else src.effective_user.id
    has = False
    lines: List[str] = []

    for driver_id in recruiter_drivers.get(user_id, []):
        rec = driver_records.get(driver_id)
        if not rec:
            continue
        # collect unread
        unread_items = [r for r in rec["replies"] if not r["read"]]
        if unread_items:
            has = True
            lines.append(f"Driver {rec['name']} ({rec['company_name']}):")
            for r in unread_items:
                lines.append(f"  • {_fmt_ts(r['ts'])} {r['from']}: {r['text']}")
            # mark read
            for r in unread_items:
                r["read"] = True
            rec["unread"] = 0

    if not has:
        await src.message.reply_text("No new replies yet.")
    else:
        await src.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# FORM STEPS ----------------------------------------------------------------
# ---------------------------------------------------------------------------

async def form_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    form_buffers.setdefault(user_id, {})["name"] = update.message.text.strip()
    await update.message.reply_text("Phone number:")
    return FORM_PHONE


async def form_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    form_buffers[user_id]["phone"] = update.message.text.strip()
    await update.message.reply_text("Experience:")
    return FORM_EXP


async def form_exp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    form_buffers[user_id]["experience"] = update.message.text.strip()
    await update.message.reply_text("Escrow option (Yes/No):")
    return FORM_ESCROW


async def form_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    form_buffers[user_id]["escrow"] = update.message.text.strip()
    await update.message.reply_text("Ready Date:")
    return FORM_READY


async def form_ready(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    form_buffers[user_id]["ready"] = update.message.text.strip()
    await update.message.reply_text("Send driver photo or file:")
    return FORM_MEDIA


async def form_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in form_buffers:
        await update.message.reply_text("Form session lost. Use /menu -> Submit Driver.")
        return ConversationHandler.END

    file_id = None
    file_type = None

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"

    if not file_id:
        await update.message.reply_text("Please send a valid photo or document.")
        return FORM_MEDIA

    form_buffers[user_id]["file_id"] = file_id
    form_buffers[user_id]["file_type"] = file_type

    await update.message.reply_text(
        "Select a company:",
        reply_markup=_build_company_choice_keyboard("cmp"),
    )
    return FORM_COMPANY


async def form_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    company = query.data.split("|", 1)[1]
    await _finalize_submission(user_id, company, query, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# FINALIZE SUBMISSION -------------------------------------------------------
# ---------------------------------------------------------------------------

async def _finalize_submission(user_id: int, company: str, src, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = form_buffers.get(user_id)
    if not data:
        await src.message.reply_text("No form data found.")
        return

    recruiter_name = sessions[user_id]["name"] if user_id in sessions else "?"

    msg_text = (
        f"👤 Name: {data['name']}\n"
        f"📞 Phone: {data['phone']}\n"
        f"💼 Experience: {data['experience']}\n"
        f"💵 Escrow: {data['escrow']}\n"
        f"📅 Ready: {data['ready']}\n"
        f"🧑‍💼 Recruiter: {recruiter_name}"
    )

    group_id = COMPANY_GROUPS[company]
    file_type = data.get("file_type")
    file_id = data.get("file_id")

    sent_msg: Optional[Message] = None
    try:
        if file_type == "photo":
            sent_msg = await context.bot.send_photo(chat_id=group_id, photo=file_id, caption=msg_text)
        elif file_type == "document":
            sent_msg = await context.bot.send_document(chat_id=group_id, document=file_id, caption=msg_text)
        else:
            sent_msg = await context.bot.send_message(chat_id=group_id, text=msg_text)
    except Exception as e:
        logger.exception("Failed to send driver to %s: %s", company, e)
        await src.message.reply_text("❌ Failed to send driver.")
        form_buffers.pop(user_id, None)
        return

    if not sent_msg:
        await src.message.reply_text("❌ Failed to send driver.")
        form_buffers.pop(user_id, None)
        return

    # Create driver record
    driver_id = len(driver_records) + 1
    rec = _new_driver_record(
        id=driver_id,
        name=data["name"],
        phone=data["phone"],
        experience=data["experience"],
        escrow=data["escrow"],
        ready=data["ready"],
        file_type=file_type,
        file_id=file_id,
        recruiter_id=user_id,
        recruiter_name=recruiter_name,
        company_name=company,
        group_id=group_id,
        group_msg_id=sent_msg.message_id,
        status="Sent",
    )
    driver_records[driver_id] = rec
    recruiter_drivers.setdefault(user_id, []).append(driver_id)
    group_message_index[(group_id, sent_msg.message_id)] = driver_id

    # Notify admin group (with status buttons for admin)
    admin_text = f"📤 New driver from {recruiter_name} to {company}:\n{data['name']} ({data['phone']})"
    try:
        await context.bot.send_message(
            chat_id=ADMIN_NOTIFY_CHAT_ID,
            text=admin_text,
            reply_markup=_build_admin_status_keyboard(driver_id),
        )
    except Exception as e:
        logger.warning("Admin notify failed: %s", e)

    await src.message.reply_text("✅ Driver submitted.")
    form_buffers.pop(user_id, None)


# ---------------------------------------------------------------------------
# RECRUITER: MY DRIVERS -----------------------------------------------------
# ---------------------------------------------------------------------------

async def show_my_drivers(src, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = src.from_user.id if hasattr(src, "from_user") else src.effective_user.id
    ids = recruiter_drivers.get(user_id, [])
    if not ids:
        await src.message.reply_text("You have not submitted any drivers yet.")
        return

    # Build list text and inline "View" buttons (paged if large?)
    lines = ["📋 Your Drivers:"]
    buttons = []
    for driver_id in ids[-25:]:  # last 25
        rec = driver_records.get(driver_id)
        if not rec:
            continue
        lines.append(f"• {_driver_summary_line(rec)}")
        buttons.append([InlineKeyboardButton(f"View {rec['name']}", callback_data=f"view|{driver_id}")])

    await src.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


# ---------------------------------------------------------------------------
# VIEW DRIVER DETAILS -------------------------------------------------------
# ---------------------------------------------------------------------------

async def view_driver_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, driver_id_s = query.data.split("|", 1)
        driver_id = int(driver_id_s)
    except Exception:
        return

    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Driver not found.")
        return

    md = _driver_timeline_md(rec)
    can_repost = rec["company_name"] == "Waiting Line"  # only from Waiting Line per spec
    await query.message.reply_text(
        md,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=_build_driver_detail_keyboard(driver_id, can_repost=can_repost),
    )


# ---------------------------------------------------------------------------
# ASK FOR UPDATE (per driver) -----------------------------------------------
# ---------------------------------------------------------------------------

async def ask_update_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, driver_id_s = query.data.split("|", 1)
        driver_id = int(driver_id_s)
    except Exception:
        return

    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Driver not found.")
        return

    # Post polite reminder in company group replying to original driver post
    text = f"🔄 Recruiter {rec['recruiter_name']} is asking for an update on this driver."
    try:
        await context.bot.send_message(
            chat_id=rec["group_id"],
            text=text,
            reply_to_message_id=rec["group_msg_id"],
            allow_sending_without_reply=True,
        )
    except Exception as e:
        logger.warning("Ask update send failed: %s", e)

    await query.message.reply_text("Update request sent.")


# ---------------------------------------------------------------------------
# REPOST FLOW ----------------------------------------------------------------
# ---------------------------------------------------------------------------

async def repost_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, driver_id_s = query.data.split("|", 1)
        driver_id = int(driver_id_s)
    except Exception:
        return

    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Driver not found.")
        return

    # Only allow repost from Waiting Line (enforced also at keyboard build)
    await query.message.reply_text(
        "Choose company to repost to:",
        reply_markup=_build_company_choice_keyboard(prefix=f"repostto|{driver_id}", exclude_group_id=rec["group_id"]),
    )


async def repost_choose_company(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, driver_id_s, company = query.data.split("|", 2)
        driver_id = int(driver_id_s)
    except Exception:
        return

    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Driver not found.")
        return

    # Create repost request entry for admin approval
    global _repost_counter
    _repost_counter += 1
    req_id = f"R{_repost_counter}"

    repost_requests[req_id] = {
        "req_id": req_id,
        "driver_id": driver_id,
        "target_company": company,
        "recruiter_id": rec["recruiter_id"],
        "recruiter_name": rec["recruiter_name"],
    }

    await query.message.reply_text("Repost request sent to admin.")

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=ADMIN_NOTIFY_CHAT_ID,
            text=(
                f"Repost Request {req_id}:\n"
                f"Recruiter: {rec['recruiter_name']}\n"
                f"Driver: {rec['name']}\n"
                f"From: {rec['company_name']}\n"
                f"To: {company}"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"rpa|{req_id}"),
                 InlineKeyboardButton("❌ Deny", callback_data=f"rpd|{req_id}")],
            ]),
        )
    except Exception as e:
        logger.warning("Repost admin notify failed: %s", e)


async def repost_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Approved.")
    try:
        _, req_id = query.data.split("|", 1)
    except Exception:
        return

    info = repost_requests.pop(req_id, None)
    if not info:
        await query.message.reply_text("Request missing or handled.")
        return

    driver_id = info["driver_id"]
    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Original driver missing.")
        return

    target_company = info["target_company"]
    group_id = COMPANY_GROUPS[target_company]

    # send driver to new group
    text = (
        f"👤 Name: {rec['name']}\n"
        f"📞 Phone: {rec['phone']}\n"
        f"💼 Experience: {rec['experience']}\n"
        f"💵 Escrow: {rec['escrow']}\n"
        f"📅 Ready: {rec['ready']}\n"
        f"🧑‍💼 Recruiter: {rec['recruiter_name']}\n"
        f"(Repost from Waiting Line)"
    )

    sent_msg: Optional[Message] = None
    try:
        if rec["file_type"] == "photo":
            sent_msg = await context.bot.send_photo(chat_id=group_id, photo=rec["file_id"], caption=text)
        elif rec["file_type"] == "document":
            sent_msg = await context.bot.send_document(chat_id=group_id, document=rec["file_id"], caption=text)
        else:
            sent_msg = await context.bot.send_message(chat_id=group_id, text=text)
    except Exception as e:
        logger.warning("Repost send failed: %s", e)
        await query.message.reply_text("Send failed.")
        return

    if not sent_msg:
        await query.message.reply_text("Send failed.")
        return

    # create new driver record (child) referencing parent
    new_id = len(driver_records) + 1
    new_rec = _new_driver_record(
        id=new_id,
        name=rec["name"],
        phone=rec["phone"],
        experience=rec["experience"],
        escrow=rec["escrow"],
        ready=rec["ready"],
        file_type=rec["file_type"],
        file_id=rec["file_id"],
        recruiter_id=rec["recruiter_id"],
        recruiter_name=rec["recruiter_name"],
        company_name=target_company,
        group_id=group_id,
        group_msg_id=sent_msg.message_id,
        parent_id=driver_id,
    )
    driver_records[new_id] = new_rec
    recruiter_drivers.setdefault(rec["recruiter_id"], []).append(new_id)
    group_message_index[(group_id, sent_msg.message_id)] = new_id

    # timeline entry on parent
    rec["history"].append({"ts": datetime.now(), "status": f"Reposted to {target_company}", "actor": ADMIN_USERNAME})

    # notify recruiter
    try:
        await context.bot.send_message(
            chat_id=rec["recruiter_id"],
            text=f"✅ Your repost to {target_company} was approved and sent.",
        )
    except Exception as e:
        logger.warning("Failed to notify recruiter after repost approve: %s", e)


async def repost_admin_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Denied.")
    try:
        _, req_id = query.data.split("|", 1)
    except Exception:
        return

    info = repost_requests.pop(req_id, None)
    if not info:
        await query.message.reply_text("Request missing or handled.")
        return

    recruiter_id = info["recruiter_id"]
    try:
        await context.bot.send_message(chat_id=recruiter_id, text="❌ Your repost request was denied.")
    except Exception as e:
        logger.warning("Failed to notify recruiter after repost deny: %s", e)


# ---------------------------------------------------------------------------
# ADMIN STATUS CHANGE -------------------------------------------------------
# ---------------------------------------------------------------------------

async def admin_status_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, driver_id_s, status = query.data.split("|", 2)
        driver_id = int(driver_id_s)
    except Exception:
        return

    # only admin allowed
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not (session and session.get("admin")):
        await query.answer("Admin only.", show_alert=True)
        return

    rec = driver_records.get(driver_id)
    if not rec:
        await query.message.reply_text("Driver not found.")
        return

    rec["status"] = status
    rec["history"].append({"ts": datetime.now(), "status": status, "actor": ADMIN_USERNAME})

    # notify recruiter
    try:
        await context.bot.send_message(
            chat_id=rec["recruiter_id"],
            text=f"ℹ️ Admin marked {rec['name']} as {status}.",
        )
    except Exception as e:
        logger.warning("Admin status notify fail: %s", e)

    # remove buttons so not double-tapped
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLOSE DETAIL MSG (UI nicety) ----------------------------------------------
# ---------------------------------------------------------------------------

async def close_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GROUP REPLY ROUTER + SMART PARSER -----------------------------------------
# ---------------------------------------------------------------------------

async def group_reply_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    chat_id = msg.chat_id
    replied_id = msg.reply_to_message.message_id
    key = (chat_id, replied_id)
    driver_id = group_message_index.get(key)
    if not driver_id:
        return  # not tracked

    rec = driver_records.get(driver_id)
    if not rec:
        return

    text = msg.text or "(non-text reply)"
    from_name = msg.from_user.full_name if msg.from_user else "Unknown"
    now = datetime.now()

    # save reply & mark unread
    rec["replies"].append({"from": from_name, "text": text, "ts": now, "read": False})
    rec["unread"] += 1

    # forward copy to recruiter
    try:
        await msg.copy(chat_id=rec["recruiter_id"])
    except Exception:
        await context.bot.send_message(chat_id=rec["recruiter_id"], text=f"Reply in {rec['company_name']}: {text}")

    # smart keyword parse -> optional status change / recruiter ping
    guess = _smart_parse_status(text)
    if guess:
        # notify recruiter with highlight
        try:
            await context.bot.send_message(
                chat_id=rec["recruiter_id"],
                text=f"⚡ Detected reply regarding {rec['name']}: '{text}' → suggested status: {guess}",
            )
        except Exception:
            pass
        # record in history as reply but do not auto-change main status except Replied/Interested (safe)
        safe_statuses = {"Replied", "Interested"}
        if guess in safe_statuses:
            rec["status"] = guess
            rec["history"].append({"ts": now, "status": guess, "actor": from_name})


# ---------------------------------------------------------------------------
# ADMIN PANEL ----------------------------------------------------------------
# ---------------------------------------------------------------------------

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_logged_in(user_id) or not sessions[user_id]["admin"]:
        await update.message.reply_text("Unauthorized.")
        return
    update_activity(user_id)

    active = [s['name'] for s in sessions.values()]
    driver_total = len(driver_records)
    logs = f"Active users: {', '.join(active)}\nTotal drivers submitted: {driver_total}"
    await update.message.reply_text(logs)

    # Show pending repost requests (if any)
    if repost_requests:
        for req_id, info in repost_requests.items():
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"rpa|{req_id}"),
                 InlineKeyboardButton("❌ Deny", callback_data=f"rpd|{req_id}")],
            ])
            await update.message.reply_text(
                f"Repost Req {req_id}: {info['recruiter_name']} → {info['target_company']} (Driver ID {info['driver_id']})",
                reply_markup=kb,
            )


# ---------------------------------------------------------------------------
# LOGOUT --------------------------------------------------------------------
# ---------------------------------------------------------------------------

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    logout_user(user_id)
    await update.message.reply_text("You have been logged out. Use /start to login again.")


# ---------------------------------------------------------------------------
# CONVERSATION HANDLER ------------------------------------------------------
# ---------------------------------------------------------------------------
# --- start driver form from menu callback ----------------------------------
async def form_start_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point into the driver submission form when user taps 'Submit Driver' in /menu."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if not is_logged_in(user_id):
        await query.message.reply_text("Session expired. Use /start.")
        return ConversationHandler.END

    update_activity(user_id)
    form_buffers[user_id] = {}  # reset form data for this user

    await query.message.reply_text("Enter driver's full name:")
    return FORM_NAME
conv_handler = ConversationHandler(
    entry_points=[
    CommandHandler("start", start),
    CallbackQueryHandler(form_start_from_menu, pattern=r"^menu\|submit$"),
],
    states={
        LOGIN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_name)],
        LOGIN_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass)],
        FORM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, form_name)],
        FORM_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, form_phone)],
        FORM_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, form_exp)],
        FORM_ESCROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, form_escrow)],
        FORM_READY: [MessageHandler(filters.TEXT & ~filters.COMMAND, form_ready)],
        FORM_MEDIA: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, form_media)],
        FORM_COMPANY: [CallbackQueryHandler(form_company, pattern=r"^cmp\|")],
    },
    fallbacks=[],
    allow_reentry=True,
)


# ---------------------------------------------------------------------------
# MAIN ----------------------------------------------------------------------
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var or paste your real token in BOT_TOKEN.")

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Conversation (login + submit driver)
    application.add_handler(conv_handler)

    # Menu & menu actions
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CallbackQueryHandler(menu_buttons, pattern=r"^menu\|"))

    # View driver details
    application.add_handler(CallbackQueryHandler(view_driver_details, pattern=r"^view\|"))

    # Ask update
    application.add_handler(CallbackQueryHandler(ask_update_button, pattern=r"^askupd\|"))

    # Repost flow
    application.add_handler(CallbackQueryHandler(repost_button, pattern=r"^repost\|"))
    application.add_handler(CallbackQueryHandler(repost_choose_company, pattern=r"^repostto\|"))
    application.add_handler(CallbackQueryHandler(repost_admin_approve, pattern=r"^rpa\|"))
    application.add_handler(CallbackQueryHandler(repost_admin_deny, pattern=r"^rpd\|"))

    # Admin status change
    application.add_handler(CallbackQueryHandler(admin_status_change, pattern=r"^admstat\|"))

    # Close detail msg
    application.add_handler(CallbackQueryHandler(close_detail, pattern=r"^close\|"))

    # Admin panel
    application.add_handler(CommandHandler("panel", panel))

    # Logout
    application.add_handler(CommandHandler("logout", logout))

    # Group replies (must disable privacy mode or bot must be admin)
    application.add_handler(MessageHandler(filters.REPLY & filters.ChatType.GROUPS, group_reply_router))

    logger.info("Bot starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
