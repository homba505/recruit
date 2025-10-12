# ==============================================================
# HOMBA Recruit Bot — 2025 Rebuild
# Part 1: Core setup, logging, DB helpers, auth decorators,
#         role-based /menu UI, stable Render-safe startup
# ==============================================================

import os
import asyncio
import logging
from typing import Optional, Callable, Awaitable, Any, Coroutine
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

# ---- Internal DB layer (reuse your existing async stack)
from db import AsyncSessionLocal
from db_models import User

# ==============================================================
# Configuration & Logging
# ==============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN (or BOT_TOKEN) is not set")

# Minimal, production-friendly logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("homba-bot")

# ==============================================================
# Role constants
# ==============================================================

ROLE_ADMIN = "admin"
ROLE_HR = "hr_manager"
ROLE_RECRUITER = "recruiter"

# ==============================================================
# DB helper: find current user by Telegram ID
# ==============================================================

async def find_user_by_telegram_id(tg_id: int) -> Optional[User]:
    """Return the User bound to this Telegram user id, or None."""
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        res = await s.execute(select(User).where(User.telegram_id == str(tg_id)))
        return res.scalar_one_or_none()

async def unlink_telegram_id(user_id: int) -> None:
    """Set telegram_id = NULL for the given user."""
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return
        u.telegram_id = None
        await s.commit()

# ==============================================================
# Auth decorators
# ==============================================================

def require_login(handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]):
    """Ensure the caller is a logged-in user (telegram_id linked)."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        tg_user = update.effective_user
        if not tg_user:
            return
        # Cache in context.user_data to avoid repeated DB hits
        user: Optional[User] = context.user_data.get("_current_user")
        if user is None:
            user = await find_user_by_telegram_id(tg_user.id)
            context.user_data["_current_user"] = user

        if not user:
            await (update.callback_query or update.effective_message).reply_text(
                "🔒 You are not logged in yet.\n"
                "Please use /start to log in."
            )
            return
        return await handler(update, context, *args, **kwargs)
    return wrapper

def require_role(*allowed_roles: str):
    """Ensure the caller has one of the allowed roles."""
    def deco(handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]):
        @wraps(handler)
        @require_login
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user: Optional[User] = context.user_data.get("_current_user")
            if not user or user.role not in allowed_roles:
                await (update.callback_query or update.effective_message).reply_text(
                    "⛔️ You do not have permission to do that."
                )
                return
            return await handler(update, context, *args, **kwargs)
        return wrapper
    return deco

# ==============================================================
# Role-based main menu UI
# ==============================================================

def build_main_menu(role: Optional[str]) -> InlineKeyboardMarkup:
    """Return an InlineKeyboardMarkup tailored to the user's role."""
    if role == ROLE_ADMIN:
        rows = [
            [InlineKeyboardButton("👥 Manage Users", callback_data="menu:users")],
            [InlineKeyboardButton("🏢 Manage Companies", callback_data="menu:companies")],
            [InlineKeyboardButton("📊 Reports", callback_data="menu:reports")],
            [InlineKeyboardButton("📄 Export CSV", callback_data="menu:export_csv")],
            [InlineKeyboardButton("🚪 Logout", callback_data="menu:logout")],
        ]
    elif role == ROLE_HR:
        rows = [
            [InlineKeyboardButton("👥 My Team", callback_data="menu:team")],
            [InlineKeyboardButton("➕ New Recruiter", callback_data="menu:new_recruiter")],
            [InlineKeyboardButton("📊 Weekly Report", callback_data="menu:weekly_my")],
            [InlineKeyboardButton("🏢 Companies", callback_data="menu:companies")],
            [InlineKeyboardButton("🚪 Logout", callback_data="menu:logout")],
        ]
    else:
        # Default (not logged in or recruiter)
        rows = [
            [InlineKeyboardButton("➕ New Driver", callback_data="menu:new_driver")],
            [InlineKeyboardButton("🚚 My Drivers", callback_data="menu:my_drivers")],
            [InlineKeyboardButton("📊 Weekly Report", callback_data="menu:weekly_my")],
            [InlineKeyboardButton("🚪 Logout", callback_data="menu:logout")],
        ]
    return InlineKeyboardMarkup(rows)

# ==============================================================
# Basic commands
# ==============================================================

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.message or update.callback_query.message).reply_text("OK")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    role = None
    if tg_user:
        user = await find_user_by_telegram_id(tg_user.id)
        if user:
            role = user.role

    text = [
        "🤖 *HOMBA Recruit Bot* — Quick Help",
        "",
        "• /menu — open the main menu",
        "• /logout — unlink your account on this Telegram",
        "• /health — service ping",
    ]
    if role == ROLE_ADMIN:
        text += [
            "",
            "*Admin:* manage users & companies, reports & exports.",
        ]
    elif role == ROLE_HR:
        text += [
            "",
            "*HR Manager:* manage your recruiters and weekly stats.",
        ]
    elif role == ROLE_RECRUITER:
        text += [
            "",
            "*Recruiter:* add drivers and track your submissions.",
        ]
    else:
        text += [
            "",
            "_You are not logged in yet. Use /start to log in._",
        ]

    await update.message.reply_text("\n".join(text), parse_mode=ParseMode.MARKDOWN)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If logged in, open menu. If not, Part 2 will start the login wizard."""
    tg_user = update.effective_user
    if not tg_user:
        return

    user = await find_user_by_telegram_id(tg_user.id)
    context.user_data["_current_user"] = user

    if user:
        await update.message.reply_text(
            f"👋 Welcome back, *{user.username}*!\nChoose an option:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(user.role),
        )
    else:
        await update.message.reply_text(
            "👋 Welcome! You are not logged in yet.\n"
            "Please wait — the login flow will be enabled in the next step.\n"
            "_Admin can link your Telegram after you log in._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(None),
        )

@require_login
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user: User = context.user_data["_current_user"]
    await update.message.reply_text(
        "Choose an option:",
        reply_markup=build_main_menu(user.role),
    )

@require_login
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user: User = context.user_data["_current_user"]
    await unlink_telegram_id(user.id)
    context.user_data["_current_user"] = None
    await update.message.reply_text("✅ Logged out on this Telegram. Use /start to log in again.")

# ==============================================================
# Callback router (for top-level menu buttons only, for now)
# ==============================================================

@require_login
async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user: User = context.user_data["_current_user"]
    data = q.data or ""

    if data == "menu:logout":
        await unlink_telegram_id(user.id)
        context.user_data["_current_user"] = None
        await q.edit_message_text("✅ Logged out on this Telegram. Use /start to log in again.")
        return

    # Placeholders — actual handlers will be added in Parts 2–3
    if data == "menu:users" and user.role == ROLE_ADMIN:
        await q.edit_message_text("👥 Users panel (coming up next)…")
    elif data == "menu:companies" and user.role in (ROLE_ADMIN, ROLE_HR):
        await q.edit_message_text("🏢 Companies panel (coming up next)…")
    elif data == "menu:reports":
        await q.edit_message_text("📊 Reports (coming up next)…")
    elif data == "menu:export_csv":
        await q.edit_message_text("📄 CSV export (coming up next)…")
    elif data == "menu:team" and user.role == ROLE_HR:
        await q.edit_message_text("👥 My Team (coming up next)…")
    elif data == "menu:new_recruiter" and user.role == ROLE_HR:
        await q.edit_message_text("➕ New recruiter flow (coming up next)…")
    elif data == "menu:weekly_my":
        await q.edit_message_text("📊 Your weekly report (coming up next)…")
    elif data == "menu:new_driver":
        await q.edit_message_text("➕ New driver form (coming up next)…")
    elif data == "menu:my_drivers":
        await q.edit_message_text("🚚 Your recent drivers (coming up next)…")
    else:
        await q.edit_message_text("ℹ️ This section will be enabled in the next part.")

# ==============================================================
# Global error handler (keeps polling alive)
# ==============================================================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update):
            target = update.effective_message or update.callback_query and update.callback_query.message
            if target:
                await target.reply_text("⚠️ Unexpected error occurred. It was logged and will be reviewed.")
    except Exception:
        pass

# ================================
# Application factory & entrypoint
# ================================
def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("logout", cmd_logout))

    # Menu button router
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern="^menu:"))

    # Errors
    app.add_error_handler(on_error)

    # Load all parts after they are defined
    init_all_parts(app)

    return app


# --- combine all register_partX here (added just above main) ---
def init_all_parts(app):
    register_part2(app)
    register_part3(app)
    register_part4(app)
    register_part5(app)
    register_part6(app)
# --- end of init_all_parts ---


def main() -> None:
    log.info("Starting HOMBA Recruit Bot…")
    app = build_application()
    app.run_polling(allowed_updates=None)
    log.info("Bot stopped.")


if __name__ == "__main__":
    main()

# ==============================================================
# Part 2: Login flow + Admin & HR user management UI
# ==============================================================

from telegram.ext import (
    ConversationHandler,
    MessageHandler,
    filters,
)
from sqlalchemy import select

# Reuse CRUD helpers
from crud import (
    get_user_by_username,
    check_pw,
    set_user_telegram_id,
    list_users,
    list_team,
    update_user_password,
    disable_user,
    enable_user,
)

# ---------- Small DB utilities (local to bot.py) ----------

async def get_user_by_id(user_id: int) -> Optional[User]:
    async with AsyncSessionLocal() as s:
        return await s.get(User, user_id)

async def delete_user_by_id(user_id: int) -> bool:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        await s.delete(u)
        await s.commit()
        return True

async def change_user_manager(user_id: int, new_manager_id: Optional[int]) -> bool:
    """Move a recruiter under a different HR manager (or None)."""
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        u.manager_id = new_manager_id
        await s.commit()
        return True

# ==============================================================
# Login wizard (/login)
# ==============================================================

LOGIN_USERNAME, LOGIN_PASSWORD = range(120, 122)

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 *Login*\n\nPlease send your *username*:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return LOGIN_USERNAME

async def login_got_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login_username"] = (update.message.text or "").strip()
    await update.message.reply_text("Now send your *password*:", parse_mode=ParseMode.MARKDOWN)
    return LOGIN_PASSWORD

async def login_got_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = context.user_data.get("login_username")
    password = (update.message.text or "").strip()
    tg_user = update.effective_user
    if not tg_user or not username:
        await update.message.reply_text("Login aborted. Use /login again.")
        return ConversationHandler.END

    # Find user by username
    u = await get_user_by_username(username)
    if not u:
        await update.message.reply_text("❌ Username not found. Try /login again.")
        return ConversationHandler.END

    # Verify password
    if not check_pw(password, u.password_hash):
        await update.message.reply_text("❌ Wrong password. Try /login again.")
        return ConversationHandler.END

    # Link Telegram ID
    await set_user_telegram_id(u.id, str(tg_user.id))

    # Cache fresh user in context
    linked = await find_user_by_telegram_id(tg_user.id)
    context.user_data["_current_user"] = linked

    await update.message.reply_text(
        f"✅ Logged in as *{linked.username}* ({linked.role}).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_main_menu(linked.role),
    )
    return ConversationHandler.END

async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Login cancelled.")
    return ConversationHandler.END

# Patch /start to recommend /login if not linked (override Part 1 definition cleanly)
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):  # redefined
    tg_user = update.effective_user
    if not tg_user:
        return
    user = await find_user_by_telegram_id(tg_user.id)
    context.user_data["_current_user"] = user
    if user:
        await update.message.reply_text(
            f"👋 Welcome back, *{user.username}*!\nChoose an option:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_main_menu(user.role),
        )
    else:
        await update.message.reply_text(
            "👋 Welcome! You’re not linked yet.\nUse /login to sign in.",
            parse_mode=ParseMode.MARKDOWN,
        )

# ==============================================================
# Admin & HR: Users / Team management UIs
# ==============================================================

def _user_row_buttons(u: User, viewer_role: str) -> list[InlineKeyboardButton]:
    """Return per-user action buttons based on viewer role."""
    btns = []
    # Change password
    btns.append(InlineKeyboardButton("🔑 PW", callback_data=f"user:pw:{u.id}"))
    # Enable/Disable (not for admins)
    if u.role != ROLE_ADMIN:
        toggle = "disable" if u.is_active else "enable"
        label = "⏻ Disable" if u.is_active else "⏻ Enable"
        btns.append(InlineKeyboardButton(label, callback_data=f"user:{toggle}:{u.id}"))
    # Delete user (admins can delete anyone except admins; HR can delete recruiters only)
    if (viewer_role == ROLE_ADMIN and u.role != ROLE_ADMIN) or (viewer_role == ROLE_HR and u.role == ROLE_RECRUITER):
        btns.append(InlineKeyboardButton("🗑️ Delete", callback_data=f"user:del:{u.id}"))
    # Move recruiter (Admin only, recruiters only)
    if viewer_role == ROLE_ADMIN and u.role == ROLE_RECRUITER:
        btns.append(InlineKeyboardButton("↔ Move", callback_data=f"user:move:{u.id}"))
    return btns

def _format_user_line(u: User) -> str:
    mark = "🟢" if u.is_active else "🔴"
    mgr = f"  (HR id:{u.manager_id})" if u.manager_id else ""
    return f"{mark} [{u.id}] {u.username} — *{u.role}*{mgr}"

@require_role(ROLE_ADMIN)
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: list all users with inline controls."""
    users = await list_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return
    chunks = []
    for u in users:
        chunks.append(_format_user_line(u))
    text = "👥 *All users:*\n" + "\n".join(chunks)
    # Build a grid of per-user rows
    rows = []
    for u in users:
        btns = _user_row_buttons(u, viewer_role=ROLE_ADMIN)
        if btns:
            rows.append(btns)
    kb = InlineKeyboardMarkup(rows) if rows else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@require_role(ROLE_HR)
async def cmd_my_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """HR: list only your recruiters with inline controls."""
    me: User = context.user_data["_current_user"]
    team = await list_team(me.id)
    if not team:
        await update.message.reply_text("👥 Your team is empty. Use the menu to add a recruiter.")
        return
    chunks = [ _format_user_line(u) for u in team ]
    text = "👥 *Your team:*\n" + "\n".join(chunks)
    rows = []
    for u in team:
        btns = _user_row_buttons(u, viewer_role=ROLE_HR)
        if btns:
            rows.append(btns)
    kb = InlineKeyboardMarkup(rows) if rows else None
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ---------- Password change (conversation) ----------

CHANGE_PW_WAITING = 200

@require_login
async def on_user_pw(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    q = update.callback_query
    await q.answer()
    # permissions: admin can change anyone; HR can change only their team’s recruiters
    viewer: User = context.user_data["_current_user"]
    target = await get_user_by_id(target_id)
    if not target:
        await q.edit_message_text("❌ User not found.")
        return
    if viewer.role == ROLE_HR:
        # HR can only affect recruiters they own
        if target.role != ROLE_RECRUITER or target.manager_id != viewer.id:
            await q.edit_message_text("⛔️ You can only change your recruiters’ passwords.")
            return
    # store target id for next message
    context.user_data["change_pw_target"] = target.id
    await q.edit_message_text(f"🔑 Send a *new password* for user [{target.id}] {target.username}:", parse_mode=ParseMode.MARKDOWN)
    # switch to message-mode
    context.user_data["change_pw_chat_id"] = q.message.chat_id
    return CHANGE_PW_WAITING

@require_login
async def change_pw_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.user_data.get("change_pw_target")
    if not target_id:
        await update.message.reply_text("❌ No password change in progress.")
        return ConversationHandler.END
    new_pw = (update.message.text or "").strip()
    if len(new_pw) < 4:
        await update.message.reply_text("Password too short. Send 4+ characters.")
        return CHANGE_PW_WAITING
    ok, err = await update_user_password(target_id, new_pw)
    if ok:
        await update.message.reply_text("✅ Password changed.")
    else:
        await update.message.reply_text(f"❌ Failed to change password: {err or 'unknown error'}")
    context.user_data.pop("change_pw_target", None)
    return ConversationHandler.END

async def change_pw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("change_pw_target", None)
    await update.message.reply_text("❎ Password change cancelled.")
    return ConversationHandler.END

# ---------- Enable/Disable ----------

@require_login
async def on_user_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int, enable: bool):
    q = update.callback_query
    await q.answer()
    viewer: User = context.user_data["_current_user"]
    target = await get_user_by_id(target_id)
    if not target:
        await q.edit_message_text("❌ User not found.")
        return
    if target.role == ROLE_ADMIN:
        await q.edit_message_text("⛔️ Cannot change admin state.")
        return
    # HR can only manage their recruiters
    if viewer.role == ROLE_HR:
        if target.role != ROLE_RECRUITER or target.manager_id != viewer.id:
            await q.edit_message_text("⛔️ You can only manage your recruiters.")
            return
    if enable:
        await enable_user(target.id)
        await q.edit_message_text(f"✅ Enabled user [{target.id}] {target.username}.")
    else:
        await disable_user(target.id)
        await q.edit_message_text(f"✅ Disabled user [{target.id}] {target.username}.")

# ---------- Delete (with confirm) ----------

@require_login
async def on_user_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    q = update.callback_query
    await q.answer()
    viewer: User = context.user_data["_current_user"]
    target = await get_user_by_id(target_id)
    if not target:
        await q.edit_message_text("❌ User not found.")
        return
    if target.role == ROLE_ADMIN:
        await q.edit_message_text("⛔️ Cannot delete admin.")
        return
    # HR can only delete their recruiters
    if viewer.role == ROLE_HR:
        if target.role != ROLE_RECRUITER or target.manager_id != viewer.id:
            await q.edit_message_text("⛔️ You can only delete your recruiters.")
            return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"user:del_ok:{target.id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="user:noop"),
        ]
    ])
    await q.edit_message_text(f"⚠️ Delete user [{target.id}] {target.username}?", reply_markup=kb)

@require_login
async def on_user_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    q = update.callback_query
    await q.answer()
    ok = await delete_user_by_id(target_id)
    if ok:
        await q.edit_message_text("🗑️ User deleted.")
    else:
        await q.edit_message_text("❌ Delete failed (user may not exist).")

# ---------- Move recruiter (Admin) ----------

@require_login
async def on_user_move_begin(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int):
    q = update.callback_query
    await q.answer()
    viewer: User = context.user_data["_current_user"]
    if viewer.role != ROLE_ADMIN:
        await q.edit_message_text("⛔️ Admins only.")
        return
    target = await get_user_by_id(target_id)
    if not target or target.role != ROLE_RECRUITER:
        await q.edit_message_text("❌ Recruiter not found.")
        return
    # list all HR managers
    all_users = await list_users()
    hrs = [u for u in all_users if u.role == ROLE_HR]
    if not hrs:
        await q.edit_message_text("No HR managers to assign.")
        return
    rows = []
    for hr in hrs:
        rows.append([InlineKeyboardButton(f"➡ {hr.username} (id:{hr.id})", callback_data=f"user:move_to:{target.id}:{hr.id}")])
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data="user:noop")])
    await q.edit_message_text(f"↔ Select new HR for [{target.id}] {target.username}:", reply_markup=InlineKeyboardMarkup(rows))

@require_login
async def on_user_move_apply(update: Update, context: ContextTypes.DEFAULT_TYPE, target_id: int, new_hr_id: int):
    q = update.callback_query
    await q.answer()
    viewer: User = context.user_data["_current_user"]
    if viewer.role != ROLE_ADMIN:
        await q.edit_message_text("⛔️ Admins only.")
        return
    ok = await change_user_manager(target_id, new_hr_id)
    if ok:
        await q.edit_message_text(f"✅ Moved recruiter id:{target_id} to HR id:{new_hr_id}.")
    else:
        await q.edit_message_text("❌ Move failed.")

# ---------- Callback router for user actions ----------

@require_login
async def on_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    try:
        if data.startswith("user:pw:"):
            target_id = int(data.split(":")[2])
            # Start PW change flow
            return await on_user_pw(update, context, target_id)
        if data.startswith("user:disable:"):
            target_id = int(data.split(":")[2])
            return await on_user_toggle(update, context, target_id, enable=False)
        if data.startswith("user:enable:"):
            target_id = int(data.split(":")[2])
            return await on_user_toggle(update, context, target_id, enable=True)
        if data.startswith("user:del:"):
            target_id = int(data.split(":")[2])
            return await on_user_delete_confirm(update, context, target_id)
        if data.startswith("user:del_ok:"):
            target_id = int(data.split(":")[2])
            return await on_user_delete_do(update, context, target_id)
        if data == "user:noop":
            await q.answer("Cancelled", show_alert=False)
            return
        if data.startswith("user:move:"):
            target_id = int(data.split(":")[2])
            return await on_user_move_begin(update, context, target_id)
        if data.startswith("user:move_to:"):
            _, _, uid, hrid = data.split(":")
            return await on_user_move_apply(update, context, int(uid), int(hrid))
    except Exception as e:
        log.exception("User action error: %s", e)
        await q.edit_message_text("⚠️ Action failed due to an error.")

# ==============================================================
# Hook Part 2 into the application
# ==============================================================

def register_part2(app):
    # Login conversation
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_username)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_password)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
        name="login_conv",
        persistent=False,
    )
    app.add_handler(login_conv)

    # Override /start from Part 1 with the improved version (Python uses last defined)
    app.add_handler(CommandHandler("start", cmd_start))

    # Admin & HR lists
    app.add_handler(CommandHandler("users", cmd_users))      # Admin
    app.add_handler(CommandHandler("my_team", cmd_my_team))  # HR

    # Change password conversation (triggered by inline button)
    change_pw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_user_action, pattern=r"^user:pw:\d+$")],
        states={
            CHANGE_PW_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, change_pw_receive)],
        },
        fallbacks=[CommandHandler("cancel", change_pw_cancel)],
        map_to_parent={},
        name="change_pw_conv",
        persistent=False,
    )
    app.add_handler(change_pw_conv)

    # Other user actions (enable/disable/delete/move)
    app.add_handler(CallbackQueryHandler(on_user_action, pattern=r"^user:(disable|enable|del|del_ok|move|move_to|noop).+"))
    # ==============================================================
# Part 3: Companies panel + Driver flow + Replies + Reports
# ==============================================================

from datetime import datetime, timedelta
from telegram import InputFile
from telegram.ext import (
    ConversationHandler,
    MessageHandler,
)
from telegram import constants as TG_CONST
from sqlalchemy import select

# ---- CRUD imports for this part
from crud import (
    list_companies,
    create_company,
    rename_company,
    change_company_chat_id,
    delete_company,
    create_driver,
    find_driver_by_message_in_company_chat,
    create_driver_reply,
    list_my_drivers,
    export_all_drivers_csv,
    weekly_report_summary,
    weekly_report_summary_for_user,
    generate_driver_pdf,  # assumes logo + footer patch added in crud.py
)
from db_models import Company, Driver

# ==============================================================
# Companies Panel (Admin & HR)
# ==============================================================

# Companies menu actions via callback:
#   co:list, co:new, co:rename:<id>, co:setchat:<id>, co:delete:<id>, co:del_ok:<id>, co:noop
@require_role(ROLE_ADMIN, ROLE_HR)
async def cmd_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_companies_panel(update.effective_message, context)

async def show_companies_panel(msg, context: ContextTypes.DEFAULT_TYPE, notice: str = ""):
    comps = await list_companies()
    rows = []
    if comps:
        for c in comps:
            label = f"{c.name}  (chat: {c.telegram_chat_id or '—'})"
            rows.append([InlineKeyboardButton(label, callback_data=f"co:show:{c.id}")])
    rows += [
        [InlineKeyboardButton("➕ New company", callback_data="co:new")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="co:list")],
    ]
    text = "🏢 *Companies*\n" + (f"_{notice}_\n" if notice else "")
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN)

@require_role(ROLE_ADMIN, ROLE_HR)
async def on_companies_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    if data == "co:list":
        return await q.edit_message_text("…", reply_markup=None) or await show_companies_panel(q.message, context)
    if data == "co:new":
        context.user_data["co_flow"] = {"mode": "new"}
        await q.edit_message_text("🏢 Enter *company name*:", parse_mode=ParseMode.MARKDOWN)
        context.user_data["co_state"] = "await_name"
        return
    if data.startswith("co:show:"):
        cid = int(data.split(":")[2])
        async with AsyncSessionLocal() as s:
            c = await s.get(Company, cid)
        if not c:
            return await q.edit_message_text("❌ Company not found.")
        rows = [
            [InlineKeyboardButton("✏️ Rename", callback_data=f"co:rename:{cid}")],
            [InlineKeyboardButton("💬 Set chat id", callback_data=f"co:setchat:{cid}")],
        ]
        # Deletion allowed for admin/HR. Safer to require confirm.
        rows.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"co:delete:{cid}")])
        rows.append([InlineKeyboardButton("⬅ Back", callback_data="co:list")])
        text = f"🏢 *{c.name}*\nchat_id: `{c.telegram_chat_id or '—'}`"
        return await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    if data.startswith("co:rename:"):
        cid = int(data.split(":")[2])
        context.user_data["co_flow"] = {"mode": "rename", "id": cid}
        await q.edit_message_text("✏️ Send *new name*:", parse_mode=ParseMode.MARKDOWN)
        context.user_data["co_state"] = "await_rename"
        return
    if data.startswith("co:setchat:"):
        cid = int(data.split(":")[2])
        context.user_data["co_flow"] = {"mode": "setchat", "id": cid}
        await q.edit_message_text("💬 Send *new chat id* (like `-1001234567890`) or `clear`:", parse_mode=ParseMode.MARKDOWN)
        context.user_data["co_state"] = "await_chatid"
        return
    if data.startswith("co:delete:"):
        cid = int(data.split(":")[2])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"co:del_ok:{cid}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="co:list")],
        ])
        return await q.edit_message_text("⚠️ Delete company and its links?", reply_markup=kb)
    if data.startswith("co:del_ok:"):
        cid = int(data.split(":")[2])
        await delete_company(cid)
        return await q.edit_message_text("🗑️ Company deleted.") or await show_companies_panel(q.message, context)

# Companies text input handler (name/chat id)
@require_role(ROLE_ADMIN, ROLE_HR)
async def on_companies_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("co_state")
    flow = context.user_data.get("co_flow") or {}
    if not state:
        return
    text = (update.message.text or "").strip()
    if state == "await_name" and flow.get("mode") == "new":
        ok, err = await create_company(text, None)
        if ok:
            await update.message.reply_text("✅ Company created.")
        else:
            await update.message.reply_text(f"❌ Failed: {err or 'unknown error'}")
        context.user_data.pop("co_state", None); context.user_data.pop("co_flow", None)
        return await show_companies_panel(update.message, context)
    if state == "await_rename" and flow.get("mode") == "rename":
        cid = int(flow["id"])
        ok, err = await rename_company(cid, text)
        if ok:
            await update.message.reply_text("✅ Company renamed.")
        else:
            await update.message.reply_text(f"❌ Failed: {err or 'unknown error'}")
        context.user_data.pop("co_state", None); context.user_data.pop("co_flow", None)
        return await show_companies_panel(update.message, context)
    if state == "await_chatid" and flow.get("mode") == "setchat":
        cid = int(flow["id"])
        if text.lower() == "clear":
            await change_company_chat_id(cid, None)
        else:
            await change_company_chat_id(cid, text)
        await update.message.reply_text("✅ Chat id updated.")
        context.user_data.pop("co_state", None); context.user_data.pop("co_flow", None)
        return await show_companies_panel(update.message, context)

# ==============================================================
# Recruiter: New Driver Flow
# ==============================================================

DRV_KIND, DRV_COMPANY, DRV_NAME, DRV_PHONE, DRV_EXP, DRV_ESCROW, DRV_READY, DRV_FILES, DRV_CONFIRM = range(400, 409)

def _driver_flow_init(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv"] = {
        "kind": None, "company_id": None, "company_chat_id": None,
        "name": None, "phone": None, "exp_months": None,
        "escrow": None, "ready_date": None,
        "file_types": [], "file_ids": [],
    }

def _driver_card_text(d: dict) -> str:
    return (
        "📝 *Driver Submission*\n"
        f"Type: *{d.get('kind') or '-'}*\n"
        f"Name: *{d.get('name') or '-'}*\n"
        f"Phone: `{d.get('phone') or '-'}`\n"
        f"Experience: *{d.get('exp_months') or '-'} months*\n"
        f"Escrow/Deposit: *{d.get('escrow') or '-'}*\n"
        f"Ready date: *{d.get('ready_date') or '-'}*"
    )

async def _set_driver_anchor(driver_id: int, chat_id: str, msg_id: int):
    async with AsyncSessionLocal() as s:
        drv = await s.get(Driver, driver_id)
        if not drv:
            return
        drv.company_chat_id = str(chat_id)
        drv.group_msg_id = msg_id
        await s.commit()

@require_role(ROLE_RECRUITER, ROLE_HR, ROLE_ADMIN)
async def cmd_new_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _driver_flow_init(context)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Solo", callback_data="drv:kind:solo"),
         InlineKeyboardButton("Team", callback_data="drv:kind:team"),
         InlineKeyboardButton("Owner-Op", callback_data="drv:kind:owner_op")],
    ])
    await update.message.reply_text("Select *driver type*:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return DRV_KIND

@require_login
async def drv_choose_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind = q.data.split(":")[2]
    context.user_data["drv"]["kind"] = kind

    comps = await list_companies()
    if not comps:
        await q.edit_message_text("⚠️ No companies created. Ask admin/HR to add one.")
        return ConversationHandler.END
    rows = []
    for c in comps:
        rows.append([InlineKeyboardButton(c.name, callback_data=f"drv:co:{c.id}")])
    await q.edit_message_text("Choose *company*:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))
    return DRV_COMPANY

@require_login
async def drv_choose_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split(":")[2])

    async with AsyncSessionLocal() as s:
        c = await s.get(Company, cid)
    if not c:
        await q.edit_message_text("❌ Company not found.")
        return ConversationHandler.END

    context.user_data["drv"]["company_id"] = c.id
    context.user_data["drv"]["company_chat_id"] = c.telegram_chat_id

    await q.edit_message_text("Enter *driver full name*:", parse_mode=ParseMode.MARKDOWN)
    return DRV_NAME

@require_login
async def drv_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv"]["name"] = (update.message.text or "").strip()
    await update.message.reply_text("Enter *phone number* (digits, +, - allowed):", parse_mode=ParseMode.MARKDOWN)
    return DRV_PHONE

@require_login
async def drv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.message.text or "").strip()
    context.user_data["drv"]["phone"] = phone
    await update.message.reply_text("Experience in *months* (e.g. 6):", parse_mode=ParseMode.MARKDOWN)
    return DRV_EXP

@require_login
async def drv_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = int((update.message.text or "").strip())
    except Exception:
        await update.message.reply_text("Send a number like `6`.", parse_mode=ParseMode.MARKDOWN)
        return DRV_EXP
    context.user_data["drv"]["exp_months"] = m
    await update.message.reply_text("Escrow/Deposit info (or `-`):")
    return DRV_ESCROW

@require_login
async def drv_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv"]["escrow"] = (update.message.text or "").strip()
    await update.message.reply_text("Ready date (e.g. `ASAP` or `2025-10-15`):")
    return DRV_READY

@require_login
async def drv_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["drv"]["ready_date"] = (update.message.text or "").strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Finish upload", callback_data="drv:files_done")]])
    await update.message.reply_text(
        "Send CDL *photos* now (you can send multiple). When done, press the button below.",
        reply_markup=kb,
    )
    return DRV_FILES

@require_login
async def drv_collect_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = update.message.photo or []
    if not photos:
        return DRV_FILES
    # take highest-res file_id
    fid = photos[-1].file_id
    context.user_data["drv"]["file_types"].append("photo")
    context.user_data["drv"]["file_ids"].append(fid)
    return DRV_FILES

@require_login
async def drv_files_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["drv"]
    text = _driver_card_text(d) + "\n\nConfirm submission?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="drv:confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="drv:cancel")],
    ])
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return DRV_CONFIRM

@require_login
async def drv_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["drv"]
    user: User = context.user_data["_current_user"]

    file_types = ",".join(d["file_types"]) if d["file_types"] else None
    file_ids = "|".join(d["file_ids"]) if d["file_ids"] else None

    driver_id = await create_driver(
        recruiter_id=user.id,
        company_id=d["company_id"],
        company_chat_id=d["company_chat_id"],
        group_msg_id=None,
        kind=d["kind"],
        name=d["name"],
        phone=d["phone"],
        exp_months=d["exp_months"],
        escrow=d["escrow"],
        ready_date=d["ready_date"],
        file_types=file_types,
        file_ids=file_ids,
        status="new",
    )

    # Post to company chat if configured
    if d["company_chat_id"]:
        text = _driver_card_text(d) + f"\n\nref: #{driver_id}"
        try:
            sent = await context.bot.send_message(
                chat_id=d["company_chat_id"],
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            await _set_driver_anchor(driver_id, d["company_chat_id"], sent.message_id)
        except Exception as e:
            log.exception("Failed to post to company chat: %s", e)

    context.user_data.pop("drv", None)
    await q.edit_message_text(f"✅ Driver created with id *{driver_id}*.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@require_login
async def drv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("drv", None)
    await q.edit_message_text("❎ Driver submission cancelled.")
    return ConversationHandler.END

@require_role(ROLE_RECRUITER, ROLE_HR, ROLE_ADMIN)
async def cmd_my_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me: User = context.user_data["_current_user"]
    drivers = await list_my_drivers(me.id, limit=20)
    if not drivers:
        return await update.message.reply_text("No recent drivers.")
    lines, rows = [], []
    for d in drivers:
        lines.append(f"#{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}")
        rows.append([InlineKeyboardButton(f"📄 PDF #{d.id}", callback_data=f"drv:pdf:{d.id}")])
    text = "🚚 *Your recent drivers:*\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

@require_login
async def on_driver_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("drv:pdf:"):
        did = int(data.split(":")[2])
        pdf_bytes, fname = await generate_driver_pdf(did)
        if not pdf_bytes:
            return await q.edit_message_text("❌ Could not generate PDF (driver not found).")
        await context.bot.send_document(
            chat_id=q.message.chat_id,
            document=InputFile(bytes(pdf_bytes), filename=fname or "driver.pdf"),
            caption=f"📄 PDF for driver #{did}",
        )
        return

# ==============================================================
# Companies reply sync (company chats → recruiter DM)
# ==============================================================

async def on_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    When a message in a company chat is a reply to the bot's driver card, forward the text
    to the original recruiter and store in DB.
    """
    msg = update.message
    if not msg or not msg.chat or not msg.reply_to_message:
        return

    # Only react if reply is to a message posted by this bot (avoid loops)
    try:
        bot_id = context.bot.id
    except Exception:
        bot_id = None
    origin = msg.reply_to_message.from_user
    if not origin or (bot_id and origin.id != bot_id):
        return

    driver = await find_driver_by_message_in_company_chat(str(msg.chat.id), msg.reply_to_message.message_id)
    if not driver:
        return

    # Persist reply
    author = msg.from_user.full_name if msg.from_user else "Unknown"
    text = msg.text or (msg.caption or "")
    if not text:
        text = "(non-text reply)"
    await create_driver_reply(driver.id, author, text, msg.message_id)

    # Notify recruiter
    rec_user = await get_user_by_id(driver.recruiter_id)
    if rec_user and rec_user.telegram_id:
        try:
            await context.bot.send_message(
                chat_id=int(rec_user.telegram_id),
                text=f"💬 *Company reply* for driver #{driver.id}:\n_{text}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            log.exception("Failed to notify recruiter: %s", e)

# ==============================================================
# Reports & Exports
# ==============================================================

@require_role(ROLE_ADMIN)
async def cmd_weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, csv_bytes, fname = await weekly_report_summary()
    if csv_bytes and fname:
        await update.message.reply_document(
            document=InputFile(bytes(csv_bytes), filename=fname),
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

@require_login
async def cmd_weekly_report_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me: User = context.user_data["_current_user"]
    # If HR, show team report; else, personal report
    if me.role == ROLE_HR:
        txt = await _hr_team_weekly_report(me.id)
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    else:
        txt = await weekly_report_summary_for_user(me.id)
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def _hr_team_weekly_report(manager_id: int) -> str:
    since = datetime.utcnow() - timedelta(days=7)
    async with AsyncSessionLocal() as s:
        # Get team recruiters
        res = await s.execute(select(User).where(User.manager_id == manager_id))
        team = list(res.scalars().all())
        if not team:
            return "No activity in your team in the last 7 days."
        team_ids = [u.id for u in team]

        res2 = await s.execute(select(Driver).where(Driver.recruiter_id.in_(team_ids), Driver.created_at >= since))
        drivers = list(res2.scalars().all())

    if not drivers:
        return "No activity in your team in the last 7 days."

    by_status: dict[str, int] = {}
    by_recruiter: dict[int, int] = {}
    for d in drivers:
        st = (getattr(d, "status", "new") or "new").lower()
        by_status[st] = by_status.get(st, 0) + 1
        by_recruiter[d.recruiter_id] = by_recruiter.get(d.recruiter_id, 0) + 1

    status_lines = ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items()))
    top = ", ".join(
        f"id:{rid}({cnt})" for rid, cnt in sorted(by_recruiter.items(), key=lambda kv: kv[1], reverse=True)[:5]
    )
    return (
        f"📊 *Team weekly report* (since {since.date().isoformat()}):\n"
        f"Total drivers: {len(drivers)}\n"
        f"By status: {status_lines or '-'}\n"
        f"Top recruiters: {top or '-'}"
    )

@require_role(ROLE_ADMIN)
async def cmd_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    csv_bytes, fname = await export_all_drivers_csv()
    await update.message.reply_document(
        document=InputFile(bytes(csv_bytes), filename=fname),
        caption="📄 Full export",
    )

# ==============================================================
# Hook Part 3 into the application
# ==============================================================

def register_part3(app):
    # Companies panel
    app.add_handler(CommandHandler("companies", cmd_companies))
    app.add_handler(CallbackQueryHandler(on_companies_action, pattern=r"^co:(list|new|show|rename|setchat|delete|del_ok|noop).+|^co:(list|new)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_companies_text))

    # Driver flow
    drv_conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new_driver)],
        states={
            DRV_KIND: [CallbackQueryHandler(drv_choose_kind, pattern=r"^drv:kind:.+")],
            DRV_COMPANY: [CallbackQueryHandler(drv_choose_company, pattern=r"^drv:co:\d+$")],
            DRV_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, drv_name)],
            DRV_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, drv_phone)],
            DRV_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, drv_exp)],
            DRV_ESCROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, drv_escrow)],
            DRV_READY: [MessageHandler(filters.TEXT & ~filters.COMMAND, drv_ready)],
            DRV_FILES: [
                MessageHandler(filters.PHOTO, drv_collect_photo),
                CallbackQueryHandler(drv_files_done, pattern=r"^drv:files_done$"),
            ],
            DRV_CONFIRM: [
                CallbackQueryHandler(drv_confirm, pattern=r"^drv:confirm$"),
                CallbackQueryHandler(drv_cancel, pattern=r"^drv:cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", drv_cancel)],
        name="drv_conv",
        persistent=False,
    )
    app.add_handler(drv_conv)

    app.add_handler(CommandHandler("my_drivers", cmd_my_drivers))
    app.add_handler(CallbackQueryHandler(on_driver_action, pattern=r"^drv:(pdf):\d+$"))

    # Group replies → recruiter DM
    app.add_handler(MessageHandler(filters.REPLY & filters.ChatType.GROUPS, on_group_reply))

    # Reports & exports
    app.add_handler(CommandHandler("weekly_report", cmd_weekly_report))
    app.add_handler(CommandHandler("weekly_report_my", cmd_weekly_report_my))
    app.add_handler(CommandHandler("export_csv", cmd_export_csv))
# ==============================================================
# Part 4: Driver management — status updates, search, notify
# ==============================================================

from sqlalchemy import select, and_, or_

# ---------- Local DB helpers for Driver ----------

async def get_driver_by_id(did: int) -> Optional[Driver]:
    async with AsyncSessionLocal() as s:
        return await s.get(Driver, did)

async def update_driver_status_db(did: int, status: str) -> bool:
    status = status.lower().strip()
    if status not in ("new", "waiting", "approved", "rejected", "hired"):
        return False
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, did)
        if not d:
            return False
        d.status = status
        await s.commit()
        return True

async def search_drivers_scope(user: User, query: str, limit: int = 20) -> list[Driver]:
    """
    Scope rules:
      - Admin: search all
      - HR: search within their team (recruiter.manager_id == HR.id)
      - Recruiter: only own drivers
    """
    query = query.strip()
    async with AsyncSessionLocal() as s:
        stmt = select(Driver).order_by(Driver.id.desc()).limit(limit)
        # Filters by query (phone or name contains)
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(Driver.phone.ilike(like), Driver.name.ilike(like)))

        if user.role == ROLE_ADMIN:
            pass
        elif user.role == ROLE_HR:
            # join to User to restrict to team
            from db_models import User as U
            stmt = stmt.join(U, U.id == Driver.recruiter_id).where(U.manager_id == user.id)
        else:
            stmt = stmt.where(Driver.recruiter_id == user.id)

        res = await s.execute(stmt)
        return list(res.scalars().all())

async def list_recent_drivers_for(user: User, limit: int = 30) -> list[Driver]:
    async with AsyncSessionLocal() as s:
        stmt = select(Driver).order_by(Driver.id.desc()).limit(limit)
        if user.role == ROLE_ADMIN:
            pass
        elif user.role == ROLE_HR:
            from db_models import User as U
            stmt = stmt.join(U, U.id == Driver.recruiter_id).where(U.manager_id == user.id)
        else:
            stmt = stmt.where(Driver.recruiter_id == user.id)
        res = await s.execute(stmt)
        return list(res.scalars().all())

# ---------- Formatting helpers ----------

def _fmt_driver(d: Driver) -> str:
    return (
        f"#{d.id} — *{d.name or '-'}*\n"
        f"Type: *{d.kind or '-'}*   Status: *{d.status or 'new'}*\n"
        f"Phone: `{d.phone or '-'}`  Exp: *{d.exp_months or 0}m*\n"
        f"Escrow: *{d.escrow or '-'}*   Ready: *{d.ready_date or '-'}*\n"
        f"Company: *{getattr(d.company, 'name', '-') if hasattr(d, 'company') else '-'}*\n"
        f"Group anchor: chat `{d.company_chat_id or '-'}`, msg `{d.group_msg_id or '-'}`"
    )

def _driver_status_buttons(d: Driver, viewer_role: str) -> InlineKeyboardMarkup:
    rows = []
    # Status buttons visible only to Admin & HR
    if viewer_role in (ROLE_ADMIN, ROLE_HR):
        rows.append([
            InlineKeyboardButton("🟡 Waiting", callback_data=f"drv:s:{d.id}:waiting"),
            InlineKeyboardButton("🟢 Approved", callback_data=f"drv:s:{d.id}:approved"),
        ])
        rows.append([
            InlineKeyboardButton("🔴 Rejected", callback_data=f"drv:s:{d.id}:rejected"),
            InlineKeyboardButton("🏁 Hired", callback_data=f"drv:s:{d.id}:hired"),
        ])
        rows.append([InlineKeyboardButton("🔔 Notify recruiter", callback_data=f"drv:notify:{d.id}")])
    # PDF always allowed
    rows.append([InlineKeyboardButton("📄 PDF", callback_data=f"drv:pdf:{d.id}")])
    return InlineKeyboardMarkup(rows)

# ---------- /driver <id> ----------

@require_login
async def cmd_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        return await update.message.reply_text("Usage: /driver <id>")
    did = int(args[0])
    d = await get_driver_by_id(did)
    if not d:
        return await update.message.reply_text("❌ Driver not found.")
    # Scope check
    me: User = context.user_data["_current_user"]
    if me.role == ROLE_RECRUITER and d.recruiter_id != me.id:
        return await update.message.reply_text("⛔️ You can only view your own drivers.")
    if me.role == ROLE_HR:
        # ensure this recruiter is in my team
        rec = await get_user_by_id(d.recruiter_id)
        if not rec or rec.manager_id != me.id:
            return await update.message.reply_text("⛔️ This driver is not in your team.")
    text = _fmt_driver(d)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=_driver_status_buttons(d, me.role))

# ---------- /drivers_recent ----------

@require_login
async def cmd_drivers_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me: User = context.user_data["_current_user"]
    drivers = await list_recent_drivers_for(me, limit=30)
    if not drivers:
        return await update.message.reply_text("No recent drivers.")
    lines = []
    rows = []
    for d in drivers:
        lines.append(f"#{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}")
        rows.append([InlineKeyboardButton(f"Open #{d.id}", callback_data=f"drv:open:{d.id}")])
    txt = "📚 *Recent drivers:*\n" + "\n".join(lines)
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

# ---------- /find_driver <query> ----------

@require_login
async def cmd_find_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args or []).strip()
    if not q:
        return await update.message.reply_text("Usage: /find_driver <phone or name>")
    me: User = context.user_data["_current_user"]
    matches = await search_drivers_scope(me, q, limit=25)
    if not matches:
        return await update.message.reply_text("No matches.")
    lines, rows = [], []
    for d in matches:
        lines.append(f"#{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}")
        rows.append([InlineKeyboardButton(f"Open #{d.id}", callback_data=f"drv:open:{d.id}")])
    await update.message.reply_text("🔎 *Matches:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows))

# ---------- Callbacks: open driver, set status, notify ----------

@require_login
async def on_driver_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    me: User = context.user_data["_current_user"]

    # Open driver card from list
    if data.startswith("drv:open:"):
        did = int(data.split(":")[2])
        d = await get_driver_by_id(did)
        if not d:
            return await q.edit_message_text("❌ Driver not found.")
        # scope checks
        if me.role == ROLE_RECRUITER and d.recruiter_id != me.id:
            return await q.edit_message_text("⛔️ You can only view your own drivers.")
        if me.role == ROLE_HR:
            rec = await get_user_by_id(d.recruiter_id)
            if not rec or rec.manager_id != me.id:
                return await q.edit_message_text("⛔️ This driver is not in your team.")
        return await q.edit_message_text(_fmt_driver(d), parse_mode=ParseMode.MARKDOWN, reply_markup=_driver_status_buttons(d, me.role))

    # Set status — Admin & HR only
    if data.startswith("drv:s:"):
        _, _, did, status = data.split(":")
        did = int(did)
        if me.role not in (ROLE_ADMIN, ROLE_HR):
            return await q.edit_message_text("⛔️ Only Admin/HR can set status.")
        ok = await update_driver_status_db(did, status)
        if not ok:
            return await q.edit_message_text("❌ Failed to set status.")
        d = await get_driver_by_id(did)
        return await q.edit_message_text(f"✅ Status for driver #{did} set to *{status}*.", parse_mode=ParseMode.MARKDOWN, reply_markup=_driver_status_buttons(d, me.role))

    # Notify recruiter to update
    if data.startswith("drv:notify:"):
        did = int(data.split(":")[2])
        d = await get_driver_by_id(did)
        if not d:
            return await q.edit_message_text("❌ Driver not found.")
        rec = await get_user_by_id(d.recruiter_id)
        if not rec or not rec.telegram_id:
            return await q.edit_message_text("❌ Recruiter has no Telegram linked.")
        try:
            await context.bot.send_message(
                chat_id=int(rec.telegram_id),
                text=f"🔔 Please update status for driver #{d.id} — *{d.name or '-'}*.\n"
                     f"Current status: *{d.status or 'new'}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return await q.edit_message_text("✅ Recruiter notified.")
        except Exception as e:
            log.exception("Notify recruiter failed: %s", e)
            return await q.edit_message_text("❌ Failed to notify recruiter.")

# ==============================================================
# Hook Part 4 into the application
# ==============================================================

def register_part4(app):
    app.add_handler(CommandHandler("driver", cmd_driver))
    app.add_handler(CommandHandler("drivers_recent", cmd_drivers_recent))
    app.add_handler(CommandHandler("find_driver", cmd_find_driver))
    app.add_handler(CallbackQueryHandler(on_driver_manage, pattern=r"^drv:(open|s|notify):.+"))
# ==============================================================
# Part 5: Driver delete + date-range search + richer /help
# ==============================================================

from datetime import datetime, timedelta
from sqlalchemy import select, delete as sqldelete, and_, or_

from db_models import DriverReply  # for cascade-safe delete

# ---------- DB helpers ----------

async def delete_driver_cascade(did: int) -> bool:
    """Delete a driver and its replies (and any FK-dependent rows)."""
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, did)
        if not d:
            return False
        # Remove replies (defensive; if ondelete=cascade is set, this is redundant but safe)
        await s.execute(sqldelete(DriverReply).where(DriverReply.driver_id == did))
        # Delete the driver itself
        await s.delete(d)
        await s.commit()
        return True

async def list_drivers_between_scope(
    user: User,
    dt_from: datetime,
    dt_to: datetime,
    status: str | None = None,
    limit: int = 200,
) -> list[Driver]:
    async with AsyncSessionLocal() as s:
        stmt = select(Driver).where(
            and_(Driver.created_at >= dt_from, Driver.created_at < dt_to)
        ).order_by(Driver.id.desc()).limit(limit)

        if status:
            stmt = stmt.where(Driver.status == status.lower().strip())

        if user.role == ROLE_ADMIN:
            pass
        elif user.role == ROLE_HR:
            from db_models import User as U
            stmt = stmt.join(U, U.id == Driver.recruiter_id).where(U.manager_id == user.id)
        else:
            stmt = stmt.where(Driver.recruiter_id == user.id)

        res = await s.execute(stmt)
        return list(res.scalars().all())

# ---------- Extend driver action UI: add Delete button for Admin/HR ----------

def _driver_status_buttons(d: Driver, viewer_role: str) -> InlineKeyboardMarkup:  # re-define to extend buttons
    rows = []
    if viewer_role in (ROLE_ADMIN, ROLE_HR):
        rows.append([
            InlineKeyboardButton("🟡 Waiting",  callback_data=f"drv:s:{d.id}:waiting"),
            InlineKeyboardButton("🟢 Approved", callback_data=f"drv:s:{d.id}:approved"),
        ])
        rows.append([
            InlineKeyboardButton("🔴 Rejected", callback_data=f"drv:s:{d.id}:rejected"),
            InlineKeyboardButton("🏁 Hired",    callback_data=f"drv:s:{d.id}:hired"),
        ])
        rows.append([
            InlineKeyboardButton("🔔 Notify recruiter", callback_data=f"drv:notify:{d.id}"),
            InlineKeyboardButton("🗑️ Delete",           callback_data=f"drv:del:{d.id}"),
        ])
    else:
        rows.append([InlineKeyboardButton("🔔 Notify HR", callback_data=f"drv:notify:{d.id}")])
    rows.append([InlineKeyboardButton("📄 PDF", callback_data=f"drv:pdf:{d.id}")])
    return InlineKeyboardMarkup(rows)

# ---------- Delete driver (command + callbacks) ----------

@require_role(ROLE_ADMIN, ROLE_HR)
async def cmd_delete_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or not args[0].isdigit():
        return await update.message.reply_text("Usage: /delete_driver <id>")
    did = int(args[0])
    d = await get_driver_by_id(did)
    if not d:
        return await update.message.reply_text("❌ Driver not found.")
    me: User = context.user_data["_current_user"]
    if me.role == ROLE_HR:
        rec = await get_user_by_id(d.recruiter_id)
        if not rec or rec.manager_id != me.id:
            return await update.message.reply_text("⛔️ This driver is not in your team.")
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"drv:del_ok:{did}"),
            InlineKeyboardButton("❌ Cancel",      callback_data=f"drv:open:{did}"),
        ]
    ])
    await update.message.reply_text(f"⚠️ Delete driver #{did} — {d.name or '-'}?", reply_markup=kb)

@require_login
async def on_driver_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE, did: int, confirm: bool):
    q = update.callback_query
    await q.answer()
    me: User = context.user_data["_current_user"]
    d = await get_driver_by_id(did)
    if not d:
        return await q.edit_message_text("❌ Driver not found.")

    # Permission check
    if me.role == ROLE_HR:
        rec = await get_user_by_id(d.recruiter_id)
        if not rec or rec.manager_id != me.id:
            return await q.edit_message_text("⛔️ This driver is not in your team.")
    if me.role == ROLE_RECRUITER:
        return await q.edit_message_text("⛔️ Only Admin/HR can delete drivers.")

    if not confirm:
        return await q.edit_message_text("❎ Deletion cancelled.")

    ok = await delete_driver_cascade(did)
    if ok:
        await q.edit_message_text(f"🗑️ Driver #{did} deleted.")
    else:
        await q.edit_message_text("❌ Delete failed.")

# Extend on_driver_manage to capture delete/confirm
@require_login
async def on_driver_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):  # re-define to extend
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    me: User = context.user_data["_current_user"]

    # Open card
    if data.startswith("drv:open:"):
        did = int(data.split(":")[2])
        d = await get_driver_by_id(did)
        if not d:
            return await q.edit_message_text("❌ Driver not found.")
        if me.role == ROLE_RECRUITER and d.recruiter_id != me.id:
            return await q.edit_message_text("⛔️ You can only view your own drivers.")
        if me.role == ROLE_HR:
            rec = await get_user_by_id(d.recruiter_id)
            if not rec or rec.manager_id != me.id:
                return await q.edit_message_text("⛔️ This driver is not in your team.")
        return await q.edit_message_text(_fmt_driver(d), parse_mode=ParseMode.MARKDOWN, reply_markup=_driver_status_buttons(d, me.role))

    # Set status
    if data.startswith("drv:s:"):
        _, _, did, status = data.split(":")
        did = int(did)
        if me.role not in (ROLE_ADMIN, ROLE_HR):
            return await q.edit_message_text("⛔️ Only Admin/HR can set status.")
        ok = await update_driver_status_db(did, status)
        if not ok:
            return await q.edit_message_text("❌ Failed to set status.")
        d = await get_driver_by_id(did)
        return await q.edit_message_text(f"✅ Status for driver #{did} set to *{status}*.", parse_mode=ParseMode.MARKDOWN, reply_markup=_driver_status_buttons(d, me.role))

    # Notify recruiter
    if data.startswith("drv:notify:"):
        did = int(data.split(":")[2])
        d = await get_driver_by_id(did)
        if not d:
            return await q.edit_message_text("❌ Driver not found.")
        rec = await get_user_by_id(d.recruiter_id)
        if not rec or not rec.telegram_id:
            return await q.edit_message_text("❌ Recruiter has no Telegram linked.")
        try:
            await context.bot.send_message(
                chat_id=int(rec.telegram_id),
                text=f"🔔 Please update status for driver #{d.id} — *{d.name or '-'}*.\n"
                     f"Current status: *{d.status or 'new'}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return await q.edit_message_text("✅ Recruiter notified.")
        except Exception as e:
            log.exception("Notify recruiter failed: %s", e)
            return await q.edit_message_text("❌ Failed to notify recruiter.")

    # Delete (confirm flow)
    if data.startswith("drv:del_ok:"):
        did = int(data.split(":")[2])
        return await on_driver_delete_cb(update, context, did, confirm=True)
    if data.startswith("drv:del:"):
        did = int(data.split(":")[2])
        # ask confirm
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, delete", callback_data=f"drv:del_ok:{did}"),
             InlineKeyboardButton("❌ Cancel",      callback_data=f"drv:open:{did}")],
        ])
        return await q.edit_message_text(f"⚠️ Delete driver #{did}?", reply_markup=kb)

# ---------- /drivers_range <from YYYY-MM-DD> <to YYYY-MM-DD> [status] ----------

@require_login
async def cmd_drivers_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /drivers_range <from YYYY-MM-DD> <to YYYY-MM-DD> [status]")
    try:
        dt_from = datetime.strptime(args[0], "%Y-%m-%d")
        dt_to   = datetime.strptime(args[1], "%Y-%m-%d") + timedelta(days=1)  # inclusive end
    except ValueError:
        return await update.message.reply_text("❌ Bad date format. Use YYYY-MM-DD YYYY-MM-DD")
    status = args[2].lower() if len(args) >= 3 else None
    me: User = context.user_data["_current_user"]
    drivers = await list_drivers_between_scope(me, dt_from, dt_to, status=status, limit=300)
    if not drivers:
        return await update.message.reply_text("No drivers in that range.")
    lines, rows = [], []
    for d in drivers:
        lines.append(f"{d.created_at:%Y-%m-%d} • #{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}")
        rows.append([InlineKeyboardButton(f"Open #{d.id}", callback_data=f"drv:open:{d.id}")])
    txt = f"🗓️ *Drivers between* {args[0]} and {args[1]}{(' — ' + status) if status else ''}:\n" + "\n".join(lines[:60])
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(rows[:60]))

# ---------- Richer /help (override) ----------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):  # re-define to show all new tools
    tg_user = update.effective_user
    role = None
    if tg_user:
        u = await find_user_by_telegram_id(tg_user.id)
        if u:
            role = u.role

    lines = ["🤖 *HOMBA Recruit Bot* — Commands", ""]
    common = [
        "• /start — open session",
        "• /menu — open main menu",
        "• /help — this help",
        "• /logout — unlink this Telegram",
        "• /health — service ping",
        "",
        "• /my_drivers — your recent drivers",
        "• /drivers_recent — recent drivers in your scope",
        "• /find_driver <query> — search by phone/name",
        "• /drivers_range <from> <to> [status] — by date range",
    ]
    lines += common

    if role == ROLE_ADMIN:
        lines += [
            "",
            "*Admin:*",
            "• /users — list & manage users",
            "• /companies — manage companies",
            "• /weekly_report — global weekly stats (+CSV)",
            "• /export_csv — full CSV export",
            "• /driver <id> — open driver card",
            "• /delete_driver <id> — delete any driver",
        ]
    elif role == ROLE_HR:
        lines += [
            "",
            "*HR Manager:*",
            "• /my_team — manage your recruiters",
            "• /companies — manage companies",
            "• /weekly_report_my — team weekly summary",
            "• /driver <id> — open driver card",
            "• /delete_driver <id> — delete driver in your team",
        ]
    elif role == ROLE_RECRUITER:
        lines += [
            "",
            "*Recruiter:*",
            "• /new — create a driver",
            "• /weekly_report_my — your weekly summary",
            "• /driver <id> — open your driver card",
        ]
    else:
        lines += ["", "_You are not logged in. Use /login._"]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ==============================================================
# Hook Part 5 into the application
# ==============================================================

def register_part5(app):
    # Commands
    app.add_handler(CommandHandler("delete_driver", cmd_delete_driver))
    app.add_handler(CommandHandler("drivers_range", cmd_drivers_range))
    # Override help with richer version
    app.add_handler(CommandHandler("help", cmd_help))
    # Extended driver manage callbacks (delete/confirm handled here too)
    app.add_handler(CallbackQueryHandler(on_driver_manage, pattern=r"^drv:(open|s|notify|del|del_ok):.+"))
# ==============================================================
# Part 6: Scoped CSV export + paginated lists + rate-limit + version
# ==============================================================

import csv
import io
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from db_models import Company  # already imported Driver, User earlier

# ---------- Rate-limit decorator (simple per-user gate) ----------

def rate_limit(seconds: int, key: str):
    def deco(fn):
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            now = datetime.utcnow().timestamp()
            store = context.user_data.setdefault("_rl", {})
            last = store.get(key, 0.0)
            if now - last < seconds:
                # Silently ignore or send a gentle message
                await (update.effective_message or update.callback_query.message).reply_text("⏳ Slow down a little.")
                return
            store[key] = now
            return await fn(update, context, *args, **kwargs)
        return wrapper
    return deco

# ---------- Scoped CSV export (Admin=all, HR=team, Recruiter=own) ----------

async def export_drivers_csv_scope(user: User) -> tuple[bytes, str]:
    async with AsyncSessionLocal() as s:
        U = User
        C = Company
        stmt = (
            select(Driver, U.username, C.name)
            .join(U, U.id == Driver.recruiter_id)
            .outerjoin(C, C.id == Driver.company_id)
            .order_by(Driver.id.desc())
        )

        if user.role == ROLE_ADMIN:
            pass
        elif user.role == ROLE_HR:
            stmt = stmt.where(U.manager_id == user.id)
        else:
            stmt = stmt.where(Driver.recruiter_id == user.id)

        res = await s.execute(stmt)
        rows = list(res.all())

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "id", "created_at", "status", "kind", "name", "phone", "exp_months",
        "escrow", "ready_date", "recruiter_id", "recruiter_username",
        "company_id", "company_name",
    ])
    for drv, recruiter_username, company_name in rows:
        w.writerow([
            drv.id,
            getattr(drv, "created_at", None),
            drv.status or "",
            drv.kind or "",
            drv.name or "",
            drv.phone or "",
            drv.exp_months or 0,
            drv.escrow or "",
            drv.ready_date or "",
            drv.recruiter_id,
            recruiter_username or "",
            drv.company_id or "",
            company_name or "",
        ])
    data = out.getvalue().encode("utf-8")
    fname = f"drivers_scope_{datetime.utcnow():%Y%m%d_%H%M}.csv"
    return data, fname

@require_login
@rate_limit(5, "export_csv_my")
async def cmd_export_csv_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me: User = context.user_data["_current_user"]
    csv_bytes, fname = await export_drivers_csv_scope(me)
    await update.message.reply_document(
        document=InputFile(bytes(csv_bytes), filename=fname),
        caption="📄 Scoped export",
    )

# ---------- Paginated lists: helpers ----------

PAGE_SIZE = 8

def _paginate_rows(rows: list[str], page: int, key: str) -> tuple[str, InlineKeyboardMarkup]:
    total = len(rows)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    body = "\n".join(rows[start:end]) if total else "—"
    nav = []
    if pages > 1:
        left_disabled = page == 1
        right_disabled = page == pages
        nav_row = []
        nav_row.append(InlineKeyboardButton("« Prev", callback_data=f"pg:{key}:{page-1}" if not left_disabled else "pg:noop"))
        nav_row.append(InlineKeyboardButton(f"{page}/{pages}", callback_data="pg:noop"))
        nav_row.append(InlineKeyboardButton("Next »", callback_data=f"pg:{key}:{page+1}" if not right_disabled else "pg:noop"))
        nav.append(nav_row)
    return body, InlineKeyboardMarkup(nav) if nav else None

# ---------- /drivers_recent_paged ----------

@require_login
@rate_limit(2, "drivers_recent_paged")
async def cmd_drivers_recent_paged(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me: User = context.user_data["_current_user"]
    drivers = await list_recent_drivers_for(me, limit=200)
    rows = [f"#{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}" for d in drivers]
    context.user_data.setdefault("_pages", {})["drv_recent"] = rows
    body, kb = _paginate_rows(rows, 1, "drv_recent")
    await update.message.reply_text("📚 *Recent drivers (paged):*\n" + body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ---------- /find_driver_paged <query> ----------

@require_login
@rate_limit(2, "find_driver_paged")
async def cmd_find_driver_paged(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = " ".join(context.args or []).strip()
    if not q:
        return await update.message.reply_text("Usage: /find_driver_paged <phone or name>")
    me: User = context.user_data["_current_user"]
    matches = await search_drivers_scope(me, q, limit=200)
    rows = [f"#{d.id} • {d.name or '-'} • {d.phone or '-'} • {d.status or 'new'}" for d in matches]
    context.user_data.setdefault("_pages", {})["drv_find"] = rows
    body, kb = _paginate_rows(rows, 1, "drv_find")
    await update.message.reply_text(f"🔎 *Matches for* `{q}`:\n" + body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ---------- Pagination callbacks ----------

@require_login
async def on_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "pg:noop":
        return
    _, key, page_str = data.split(":")
    try:
        page = int(page_str)
    except Exception:
        page = 1
    store = context.user_data.get("_pages", {})
    rows = store.get(key) or []
    title = "📚 *Recent drivers (paged):*" if key == "drv_recent" else "🔎 *Matches:*"
    body, kb = _paginate_rows(rows, page, key)
    await q.edit_message_text(title + "\n" + body, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ---------- /version ----------

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("HOMBA Recruit Bot — build 2025-10-12 • Parts 1–6 active ✅")

# ==============================================================
# Hook Part 6 into the application
# ==============================================================

def register_part6(app):
    app.add_handler(CommandHandler("export_csv_my", cmd_export_csv_my))
    app.add_handler(CommandHandler("drivers_recent_paged", cmd_drivers_recent_paged))
    app.add_handler(CommandHandler("find_driver_paged", cmd_find_driver_paged))
    app.add_handler(CallbackQueryHandler(on_pagination, pattern=r"^pg:(drv_recent|drv_find):\d+$|^pg:noop$"))
    app.add_handler(CommandHandler("version", cmd_version))

