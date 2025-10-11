"""
HOMBA Recruit Bot — Phase 1: Core & Startup (commented)

What this chunk includes
- Imports & env setup
- Async SQLAlchemy session hook (uses AsyncSessionLocal from your db.py)
- Role constants and current_user() helper
- Safe decorators: require_login (DM-only), require_role (DM-only)
- Utilities: /health, /chatid, /help (role-aware)
- App builder + Render-safe startup (nest_asyncio)
- SAFE_GROUP_MODE kill switch foundation

Assumptions
- Python 3.12.x
- python-telegram-bot >= 21
- db.py defines: engine, AsyncSessionLocal, Base (you showed me it does)
- crud.py provides user/company helpers we’ll call in later phases
"""

from __future__ import annotations

import os
import asyncio
from functools import wraps
from typing import Optional, Callable, Awaitable, Any, Iterable

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ---- Project modules (must exist in your repo) ----
# db.py: you provided AsyncSessionLocal definition already
from db import AsyncSessionLocal
# We'll import crud later phases where needed
# import crud

# =========================
#  Environment & Constants
# =========================

# IMPORTANT: your Render env var is TELEGRAM_BOT_TOKEN (verified from screenshot)
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment.")

# Emergency kill-switch to completely silence group processing (we'll wire listeners later)
SAFE_GROUP_MODE: bool = os.getenv("SAFE_GROUP_MODE", "0") == "1"
ARCHIVE_CHAT_ID_RAW = os.getenv("ARCHIVE_CHAT_ID", "").strip()
AUDIT_CHAT_ID_RAW   = os.getenv("AUDIT_CHAT_ID", "").strip()

def _to_int_or_none(v: str):
    try:
        return int(v) if v else None
    except Exception:
        return None

ARCHIVE_CHAT_ID = _to_int_or_none(ARCHIVE_CHAT_ID_RAW)
AUDIT_CHAT_ID   = _to_int_or_none(AUDIT_CHAT_ID_RAW)


# Role names we’ll use everywhere
ROLE_ADMIN = "admin"
ROLE_HR = "hr_manager"
ROLE_RECRUITER = "recruiter"
ALL_ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_HR, ROLE_RECRUITER)


# =========================
#   User lookup helper
# =========================
# We keep this Phase-1 implementation simple and defer to `crud` later if present.
# To avoid circular imports right now, we query via SQLAlchemy session directly only when needed.
# In later phases we’ll replace internals with crud.* calls.

from sqlalchemy import select
from db_models import User  # assumes your model class is named `User` with fields: id, username, role, telegram_id


async def current_user(update: Update) -> Optional[User]:
    """
    Returns the logged-in User (by matching Telegram user id), or None.
    We deliberately DO NOT enforce roles here; decorators below will.
    """
    tg_id = str(update.effective_user.id) if update.effective_user else None
    if not tg_id:
        return None
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(User).where(User.telegram_id == tg_id))
        return res.scalars().first()


# =========================
#   Decorators (safe)
# =========================

def require_login(func: Callable[..., Awaitable[Any]]):
    """
    Enforce login/session ONLY in PRIVATE chats.
    In groups/supergroups we skip login checks entirely to prevent spam.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        # Never gate group messages here – we’ll build specific group listeners later.
        if chat and chat.type != ChatType.PRIVATE:
            return

        user = await current_user(update)
        if not user:
            # Keep this message minimal; recruiters/HRs will DM /start to log in
            if update.effective_message:
                await update.effective_message.reply_text("Please /start and log in first.")
            return
        # Attach for downstream handlers if they want it
        context.user_data["me"] = user
        return await func(update, context, *args, **kwargs)
    
    return wrapper


def require_role(*allowed_roles: str):
    """
    Role-gated command usable ONLY in PRIVATE chats.
    Example:
        @require_role(ROLE_ADMIN)
        async def cmd_only_admin(...): ...
    """
    def deco(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            chat = update.effective_chat
            if chat and chat.type != ChatType.PRIVATE:
                # Don’t enforce roles in groups – our group listeners are separate & narrow
                return

            user = await current_user(update)
            if not user:
                if update.effective_message:
                    await update.effective_message.reply_text("Please /start and log in first.")
                return
            if user.role not in allowed_roles:
                if update.effective_message:
                    await update.effective_message.reply_text("You don’t have permission for this command.")
                return
            context.user_data["me"] = user
            return await func(update, context, *args, **kwargs)
        return wrapper
    return deco


# =========================
#   Utility Commands
# =========================

async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple liveness probe."""
    if update.effective_message:
        await update.effective_message.reply_text("OK")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echo current chat id (useful for mapping companies)."""
    cid = update.effective_chat.id if update.effective_chat else "unknown"
    if update.effective_message:
        await update.effective_message.reply_text(f"Chat ID: <code>{cid}</code>", parse_mode=ParseMode.HTML)


def _help_text_for(role: Optional[str]) -> str:
    """Role-aware help text. In later phases we’ll expand this dynamically."""
    if role == ROLE_ADMIN:
        return (
            "<b>Admin</b>\n"
            "/help – show commands\n"
            "/health – bot status\n"
            "/chatid – current chat id\n"
            "User/Company/Driver/Reports commands will appear after phases 2–7.\n"
        )
    if role == ROLE_HR:
        return (
            "<b>HR Manager</b>\n"
            "/help – show commands\n"
            "/health – bot status\n"
            "/chatid – current chat id\n"
            "Team/Driver tools arrive in later phases.\n"
        )
    if role == ROLE_RECRUITER:
        return (
            "<b>Recruiter</b>\n"
            "/help – show commands\n"
            "/health – bot status\n"
            "/chatid – current chat id\n"
            "Submission & status tools arrive in later phases.\n"
        )
    return (
        "<b>General</b>\n"
        "/help – this message\n"
        "/health – bot status\n"
        "/chatid – current chat id\n"
        "Use /start to log in when the login flow arrives (Phase 2).\n"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows minimal help for now; expands as phases land."""
    user = await current_user(update)
    text = _help_text_for(user.role if user else None)
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
# ---------- Audit & Archive helpers ----------
async def send_audit(context, text: str):
    if not AUDIT_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=AUDIT_CHAT_ID, text=text)
    except Exception:
        pass

async def archive_copy_message(context, from_chat_id: int | str, message_id: int):
    if not ARCHIVE_CHAT_ID:
        return
    try:
        await context.bot.copy_message(
            chat_id=ARCHIVE_CHAT_ID,
            from_chat_id=int(from_chat_id),
            message_id=int(message_id),
        )
    except Exception:
        pass

async def archive_send_document(context, fileobj, filename: str, caption: str | None = None):
    if not ARCHIVE_CHAT_ID:
        return
    try:
        await context.bot.send_document(
            chat_id=ARCHIVE_CHAT_ID,
            document=fileobj,
            filename=filename,
            caption=caption,
        )
    except Exception:
        pass


# =========================
#   Application Builder
# =========================

def build_app() -> Application:
    """
    Creates the PTB Application and wires Phase-1 utilities.
    Later phases will register additional handlers on this same app.
    """
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Utility commands available everywhere
    app.add_handler(CommandHandler("health", cmd_health), group=0)
    app.add_handler(CommandHandler("chatid", cmd_chatid), group=0)
    app.add_handler(CommandHandler("help", cmd_help), group=0)

    # NOTE: We intentionally DO NOT add any broad MessageHandler here.
    # Group listeners will be added in a strict form later (replies only).
    return app


# =========================
#   Render-Safe Startup
# =========================

def main() -> Application:
    """
    Build & wire the app with all phases.
    """
    app = build_app()
    wire_phase2_auth(app)
    wire_phase3_users(app)
    wire_phase4_companies(app)
    wire_phase5_drivers(app)
    wire_phase6_reports(app)
    wire_phase7_groups(app)
    wire_phase8_polish(app)
    return app


# =========================
#   PHASE 2 — Auth & Login
# =========================
# Features:
# - /start → username → password (DM only)
# - /logout
# - Role-aware /help (already wired in Phase 1)
#
# Assumptions:
# - crud.py exposes:
#     get_user_by_username(username) -> User | None
#     check_pw(plain, password_hash) -> bool
#     set_user_telegram_id(user_id: int, tg_id: str | None) -> Awaitable[None]
# - db_models.User has fields: id, username, role, password_hash, telegram_id

import crud  # now safe to import since Phase 1 is in place

S_LOGIN_USERNAME, S_LOGIN_PASSWORD = range(2)

async def _already_logged_in(update: Update) -> bool:
    """Return True if current Telegram user is already linked to a User record."""
    u = await current_user(update)
    if u and update.effective_message:
        await update.effective_message.reply_text(
            f"You're already logged in as <b>{u.username}</b> ({u.role}).",
            parse_mode=ParseMode.HTML,
        )
        return True
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    DM-only login entrypoint. If the user is already linked, show help.
    Otherwise ask for username and proceed to password step.
    """
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        # In groups, /start does nothing (prevents noise).
        return

    if await _already_logged_in(update):
        # Show role-aware help
        await cmd_help(update, context)
        return ConversationHandler.END

    if update.effective_message:
        await update.effective_message.reply_text(
            "Welcome! Please send your <b>username</b> to log in:",
            parse_mode=ParseMode.HTML,
        )
    return S_LOGIN_USERNAME


async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect username and move to password step."""
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        return ConversationHandler.END

    username = (update.effective_message.text or "").strip()
    if not username:
        await update.effective_message.reply_text("Please send a valid username, or /start again.")
        return ConversationHandler.END

    context.user_data["login_username"] = username
    await update.effective_message.reply_text(
        "Now send your <b>password</b>:",
        parse_mode=ParseMode.HTML,
    )
    return S_LOGIN_PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate credentials, link Telegram ID, and finish."""
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        return ConversationHandler.END

    username = context.user_data.get("login_username")
    password = (update.effective_message.text or "").strip()
    if not username:
        await update.effective_message.reply_text("Session expired. Please /start again.")
        return ConversationHandler.END

    # Lookup user
    try:
        u = await crud.get_user_by_username(username)
    except Exception as e:
        await update.effective_message.reply_text(f"Login error: {e}")
        return ConversationHandler.END

    if not u or not crud.check_pw(password, u.password_hash):
        await update.effective_message.reply_text("❌ Invalid username or password. Try /start again.")
        return ConversationHandler.END

    # Link Telegram ID to this account
    try:
        await crud.set_user_telegram_id(u.id, str(update.effective_user.id))
    except Exception as e:
        await update.effective_message.reply_text(f"Could not link account: {e}")
        return ConversationHandler.END

    await update.effective_message.reply_text(
        f"✅ Logged in as <b>{u.username}</b> ({u.role}).",
        parse_mode=ParseMode.HTML,
    )
        # audit log
    try:
        me = await current_user(update)
        if me:
            await send_audit(context, f"🔐 Login: @{me.username} (id={me.id}, tg={update.effective_user.id})")
    except Exception:
        pass

    # Show role-specific help next
    await cmd_help(update, context)
    return ConversationHandler.END


@require_login
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unlink the Telegram ID from the current account."""
    me = context.user_data.get("me")
    if not me:
        await update.effective_message.reply_text("You're not logged in.")
        return
    try:
        await crud.set_user_telegram_id(me.id, None)
    except Exception as e:
        await update.effective_message.reply_text(f"Logout error: {e}")
        return
    await update.effective_message.reply_text("✅ Logged out. Use /start to log in again.")
    # audit log
    try:
        me = context.user_data.get("me")
        if me:
            await send_audit(context, f"🔓 Logout: @{me.username} (id={me.id}, tg={update.effective_user.id})")
    except Exception:
        pass


def wire_phase2_auth(app: Application) -> None:
    """Register Phase 2 handlers on the Application."""
    # /start conversation (DM only)
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            S_LOGIN_USERNAME: [
                MessageHandler(
                    filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                    login_username
                )
            ],
            S_LOGIN_PASSWORD: [
                MessageHandler(
                    filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                    login_password
                )
            ],
        },
        fallbacks=[],
        per_message=False,
    ))

    # /logout (works only in DM because of @require_login)
    app.add_handler(CommandHandler("logout", cmd_logout))


# 🔌 Call the wiring function after build_app()
# (If you kept Phase 1 exactly, just add these two lines right after `app = build_app()` in main() or under if __name__ == "__main__":)
#
# app = build_app()
# wire_phase2_auth(app)
# ==========================================
#   PHASE 3 — User & Team Management
# ==========================================
# Features
# Admin:
#   /add_user <username> <password> <role> [hr_username_if_recruiter]
#   /rename_user <old_username> <new_username>
#   /change_password <username> <new_password>
#   /delete_user <username>
#   /move_recruiter <recruiter_username> <new_hr_username>
#   /list_users
#
# HR Manager:
#   /add_recruiter <username> <password>
#   /change_recruiter_password <username> <new_password>
#   /delete_recruiter <username>
#   /my_team
#
# All commands are DM-only due to decorators.
#
# Assumed crud API (async):
#   - create_user(username, password, role, manager_id=None) -> tuple[bool, str|None]
#   - update_user_username(old_username, new_username) -> tuple[bool, str|None]
#   - update_user_password(username, new_password) -> tuple[bool, str|None]
#   - delete_user(username) -> tuple[bool, str|None]
#   - list_users() -> list[User]
#   - get_user_by_username(username) -> User|None
#   - list_team(hr_user_id: int) -> list[User]

# ---------- Admin commands ----------

@require_role(ROLE_ADMIN)
async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """Admin: add user (admin/hr_manager/recruiter). Recruiter requires HR username as 4th arg."""
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=4)
    if len(parts) < 4:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/add_user <username> <password> <role> [hr_username_if_recruiter]\n"
            "Roles: admin | hr_manager | recruiter"
        )
        return
    _, username, password, role, *rest = parts
    role = role.lower()
    if role not in ALL_ROLES:
        await update.effective_message.reply_text("Role must be one of: admin | hr_manager | recruiter")
        return

    manager_id = None
    if role == ROLE_RECRUITER:
        if not rest:
            await update.effective_message.reply_text("For recruiter you must pass HR username as 4th argument.")
            return
        hr_u = await crud.get_user_by_username(rest[0])
        if not hr_u or hr_u.role != ROLE_HR:
            await update.effective_message.reply_text("HR username not found or not an HR manager.")
            return
        manager_id = hr_u.id

    ok, err = await crud.create_user(username=username, password=password, role=role, manager_id=manager_id)
    await update.effective_message.reply_text("✅ User created." if ok else f"❌ Failed: {err or 'unknown error'}")


@require_role(ROLE_ADMIN)
async def cmd_rename_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /rename_user <old_username> <new_username>")
        return
    _, old_u, new_u = parts
    ok, err = await crud.update_user_username(old_u, new_u)
    await update.effective_message.reply_text("✅ Renamed." if ok else f"❌ Failed: {err or 'unknown error'}")


@require_role(ROLE_ADMIN)
async def cmd_change_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /change_password <username> <new_password>")
        return
    _, username, new_pw = parts
    u = await crud.get_user_by_username(username)
    if not u:
        await update.effective_message.reply_text("User not found.")
        return
    ok = await crud.update_user_password(u.id, new_pw)
    await update.effective_message.reply_text("✅ Password changed." if ok else "❌ Failed to change password.")



@require_role(ROLE_ADMIN)
async def cmd_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.effective_message.reply_text("Usage: /delete_user <username>")
        return
    _, username = parts
    u = await crud.get_user_by_username(username)
    if not u:
        await update.effective_message.reply_text("User not found.")
        return
    await crud.delete_user(u.id)
    await update.effective_message.reply_text("✅ User deleted.")



@require_role(ROLE_ADMIN)
async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    users = await crud.list_users()
    if not users:
        await update.effective_message.reply_text("No users yet.")
        return
    lines = []
    for u in users:
        extra = ""
        if u.role == ROLE_RECRUITER and getattr(u, "manager_id", None):
            extra = f" (HR id={u.manager_id})"
        lines.append(f"{u.id}. {u.username} — {u.role}{extra}")
    await update.effective_message.reply_text("\n".join(lines))


@require_role(ROLE_ADMIN)
async def cmd_move_recruiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    Admin: move a recruiter between HR teams.
    /move_recruiter <recruiter_username> <new_hr_username>
    """
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /move_recruiter <recruiter_username> <new_hr_username>")
        return
    _, rec_u, new_hr_u = parts

    rec = await crud.get_user_by_username(rec_u)
    if not rec or rec.role != ROLE_RECRUITER:
        await update.effective_message.reply_text("Recruiter not found.")
        return

    new_hr = await crud.get_user_by_username(new_hr_u)
    if not new_hr or new_hr.role != ROLE_HR:
        await update.effective_message.reply_text("New HR not found or not hr_manager.")
        return

    # Direct DB change via crud or session helper
    from sqlalchemy import update as sa_update
    from db import AsyncSessionLocal  # already imported in Phase 1
    from db_models import User

    async with AsyncSessionLocal() as s:
        await s.execute(
            sa_update(User).where(User.id == rec.id).values(manager_id=new_hr.id)
        )
        await s.commit()

    await update.effective_message.reply_text("✅ Recruiter moved.")


# ---------- HR Manager commands ----------

@require_role(ROLE_HR)
async def cmd_add_recruiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /add_recruiter <username> <password>")
        return
    _, username, password = parts
    ok, err = await crud.create_user(username=username, password=password, role=ROLE_RECRUITER, manager_id=me.id)
    await update.effective_message.reply_text("✅ Recruiter added." if ok else f"❌ Failed: {err or 'unknown error'}")


@require_role(ROLE_HR)
async def cmd_change_recruiter_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /change_recruiter_password <username> <new_password>")
        return
    _, username, new_pw = parts
    rec = await crud.get_user_by_username(username)
    if not rec or rec.role != ROLE_RECRUITER:
        await update.effective_message.reply_text("Recruiter not found.")
        return
    if getattr(rec, "manager_id", None) != me.id:
        await update.effective_message.reply_text("You can only change passwords for your own recruiters.")
        return
    ok = await crud.update_user_password(rec.id, new_pw)
    await update.effective_message.reply_text("✅ Password changed." if ok else "❌ Failed to change password.")



@require_role(ROLE_HR)
async def cmd_delete_recruiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.effective_message.reply_text("Usage: /delete_recruiter <username>")
        return
    _, username = parts
    rec = await crud.get_user_by_username(username)
    if not rec or rec.role != ROLE_RECRUITER:
        await update.effective_message.reply_text("Recruiter not found.")
        return
    if getattr(rec, "manager_id", None) != me.id:
        await update.effective_message.reply_text("You can only delete your own recruiters.")
        return
    await crud.delete_user(rec.id)
    await update.effective_message.reply_text("✅ Recruiter deleted.")



@require_role(ROLE_HR)
async def cmd_my_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    team = await crud.list_team(me.id)
    if not team:
        await update.effective_message.reply_text("No recruiters in your team yet.")
        return
    lines = [f"{u.id}. {u.username} — {u.role}" for u in team]
    await update.effective_message.reply_text("\n".join(lines))


def wire_phase3_users(app: Application) -> None:
    """Register Admin & HR management commands (DM-only via decorators)."""
    # Admin
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("rename_user", cmd_rename_user))
    app.add_handler(CommandHandler("change_password", cmd_change_password))
    app.add_handler(CommandHandler("delete_user", cmd_delete_user))
    app.add_handler(CommandHandler("move_recruiter", cmd_move_recruiter))
    app.add_handler(CommandHandler("list_users", cmd_list_users))

    # HR
    app.add_handler(CommandHandler("add_recruiter", cmd_add_recruiter))
    app.add_handler(CommandHandler("change_recruiter_password", cmd_change_recruiter_password))
    app.add_handler(CommandHandler("delete_recruiter", cmd_delete_recruiter))
    app.add_handler(CommandHandler("my_team", cmd_my_team))
# ==================================
#   PHASE 4 — Company Management
# ==================================
# Features (DM-only; safe via decorators):
#  - /add_company <name> [chat_id]
#  - /list_companies
#  - /rename_company <company_id> <new_name>        (admin)
#  - /set_company_chat <company_id> <chat_id>
#  - /delete_company <company_id>                   (admin)
#
# Assumed crud API (async):
#   create_company(name: str, chat_id: str|None) -> tuple[bool, str|None]
#   list_companies() -> list[Company]
#   rename_company(company_id: int, new_name: str) -> tuple[bool, str|None]
#   change_company_chat_id(company_id: int, chat_id: str|None) -> tuple[bool, str|None]
#   delete_company(company_id: int) -> tuple[bool, str|None]

from db_models import Company  # type: ignore

@require_login
async def cmd_add_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_company <name> [chat_id]
    - <name> can include spaces if you wrap it in quotes: /add_company "Majestic Trucking" -100123...
    - [chat_id] optional; you can link later with /set_company_chat
    """
    text = (update.effective_message.text or "").strip()
    # allow quoted names: /add_company "ACME Logistics" -1001...
    name: str = ""
    chat_id: str | None = None

    # Parse smartly: quoted name or simple tokens
    if '"' in text or "'" in text:
        # Extract quoted substring
        import shlex
        try:
            parts = shlex.split(text)
        except Exception:
            parts = text.split()
    else:
        parts = text.split()

    if len(parts) < 2:
        await update.effective_message.reply_text('Usage: /add_company <name> [chat_id]\nExample: /add_company "Majestic Trucking" -1001234567890')
        return

    if len(parts) == 2:
        # /add_company <name>
        name = parts[1]
    else:
        # /add_company <name> <chat_id>
        name = parts[1]
        chat_id = parts[2]

    ok, err = await crud.create_company(name, chat_id)
    await update.effective_message.reply_text("✅ Company added." if ok else f"❌ Failed: {err or 'unknown error'}")


@require_login
async def cmd_list_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies: list[Company] = await crud.list_companies()
    if not companies:
        await update.effective_message.reply_text("No companies yet.")
        return
    lines: list[str] = []
    for c in companies:
        lines.append(f"{c.id}. {c.name} — chat_id={c.telegram_chat_id or '-'}")
    await update.effective_message.reply_text("\n".join(lines))


@require_role(ROLE_ADMIN)
async def cmd_rename_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    /rename_company <company_id> <new_name>
    new_name may be quoted.
    """
    text = (update.effective_message.text or "").strip()
    import shlex
    try:
        parts = shlex.split(text)
    except Exception:
        parts = text.split()
    if len(parts) < 3:
        await update.effective_message.reply_text('Usage: /rename_company <company_id> <new_name>\nExample: /rename_company 3 "Majestic Trucking LLC"')
        return
    try:
        cid = int(parts[1])
    except:
        await update.effective_message.reply_text("company_id must be an integer.")
        return
    new_name = " ".join(parts[2:])
    ok, err = await crud.rename_company(cid, new_name)
    await update.effective_message.reply_text("✅ Renamed." if ok else f"❌ Failed: {err or 'unknown error'}")


@require_login
async def cmd_set_company_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /set_company_chat <company_id> <chat_id>
    To unlink, pass '-' as chat_id.
    """
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /set_company_chat <company_id> <chat_id>\nTo unlink: /set_company_chat <id> -")
        return
    _, cid_s, chat = parts
    try:
        cid = int(cid_s)
    except:
        await update.effective_message.reply_text("company_id must be an integer.")
        return
    chat_id = None if chat == "-" else chat
    await crud.change_company_chat_id(cid, chat_id)
    await update.effective_message.reply_text("✅ Chat updated.")

@require_role(ROLE_ADMIN)
async def cmd_delete_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.effective_message.reply_text("Usage: /delete_company <company_id>")
        return
    try:
        cid = int(parts[1])
    except:
        await update.effective_message.reply_text("company_id must be an integer.")
        return
    await crud.delete_company(cid)
    await update.effective_message.reply_text("✅ Company deleted.")

def wire_phase4_companies(app: Application) -> None:
    """Register company commands."""
    app.add_handler(CommandHandler("add_company", cmd_add_company))
    app.add_handler(CommandHandler("list_companies", cmd_list_companies))
    app.add_handler(CommandHandler("rename_company", cmd_rename_company))
    app.add_handler(CommandHandler("set_company_chat", cmd_set_company_chat))
    app.add_handler(CommandHandler("delete_company", cmd_delete_company))
# =======================================
#   PHASE 5 — Driver Submission & Tools
# =======================================
# Scope: DM-only forms for recruiters (and HR/Admin can also use if you want)
# Features:
#   /new_driver  → multi-step form:
#       - full name
#       - phone
#       - CDL class (A/B/C or None)
#       - experience (months/years free text)
#       - ready date (free text like "today", "10/12", "next week")
#       - choose company (from /list_companies)
#       - confirm → save as driver (Phase 7 will post to company chat)
#   /set_status <driver_id> <status>
#   /my_drivers  → list recent drivers for the current recruiter
#
# Assumed crud API (async):
#   create_driver(**fields) -> (driver, err)  | driver has id, recruiter_id, company_id, etc.
#   list_companies() -> list[Company]
#   list_my_drivers(recruiter_id: int, limit: int = 20) -> list[Driver]
#   update_driver_status(driver_id: int, status: str) -> (ok, err)
#
# Notes:
#   - Group posting + reply capture is added in Phase 7.
#   - This phase is DM-only via filters.ChatType.PRIVATE.

from dataclasses import dataclass

# ---------- Conversation state keys ----------
D_NAME, D_PHONE, D_CDL, D_EXP, D_READY, D_COMPANY, D_CONFIRM = range(7)

# ---------- Data model for the form (kept in context.user_data) ----------
@dataclass
class DriverForm:
    full_name: str = ""
    phone: str = ""
    cdl_class: str = ""
    experience: str = ""
    ready_date: str = ""
    company_id: int | None = None

def _reset_driver_form(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["driver_form"] = DriverForm()

def _get_driver_form(context: ContextTypes.DEFAULT_TYPE) -> DriverForm:
    return context.user_data.get("driver_form") or DriverForm()

# ---------- Helpers ----------
def _is_valid_phone(s: str) -> bool:
    # Minimal validation: at least 7 digits when non-digits removed
    import re
    digits = re.sub(r"\D", "", s)
    return len(digits) >= 7

async def _company_lines() -> list[str]:
    companies = await crud.list_companies()
    return [f"{c.id}. {c.name} — chat_id={c.telegram_chat_id or '-'}" for c in companies]

async def _send_companies(update: Update):
    lines = await _company_lines()
    if not lines:
        await update.effective_message.reply_text("No companies yet. Ask admin to /add_company first.")
    else:
        await update.effective_message.reply_text("Choose company by sending its ID:\n" + "\n".join(lines))

# ---------- Entry ----------
@require_login
async def cmd_new_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        return
    _reset_driver_form(context)
    await update.effective_message.reply_text("Driver form started.\nSend driver <b>Full Name</b>:", parse_mode=ParseMode.HTML)
    return D_NAME

# ---------- Steps ----------
async def step_d_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_message.text or "").strip()
    if len(name) < 3:
        await update.effective_message.reply_text("Name looks too short. Try again:")
        return D_NAME
    form = _get_driver_form(context)
    form.full_name = name
    context.user_data["driver_form"] = form
    await update.effective_message.reply_text("Send driver <b>Phone</b> (any format):", parse_mode=ParseMode.HTML)
    return D_PHONE

async def step_d_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.effective_message.text or "").strip()
    if not _is_valid_phone(phone):
        await update.effective_message.reply_text("Phone looks invalid. Try again (you can include +, spaces, dashes):")
        return D_PHONE
    form = _get_driver_form(context)
    form.phone = phone
    context.user_data["driver_form"] = form
    await update.effective_message.reply_text("Send <b>CDL class</b> (A/B/C or 'None'):", parse_mode=ParseMode.HTML)
    return D_CDL

async def step_d_cdl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cdl = (update.effective_message.text or "").strip().upper()
    if cdl not in {"A", "B", "C", "NONE"}:
        await update.effective_message.reply_text("Please send one of: A, B, C, None")
        return D_CDL
    form = _get_driver_form(context)
    form.cdl_class = "None" if cdl == "NONE" else cdl
    context.user_data["driver_form"] = form
    await update.effective_message.reply_text("Send <b>Experience</b> (e.g., '2 years', '8 months'):", parse_mode=ParseMode.HTML)
    return D_EXP

async def step_d_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    exp = (update.effective_message.text or "").strip()
    if len(exp) < 2:
        await update.effective_message.reply_text("Please describe experience (e.g., '2 years'):")
        return D_EXP
    form = _get_driver_form(context)
    form.experience = exp
    context.user_data["driver_form"] = form
    await update.effective_message.reply_text("Send <b>Ready date</b> (e.g., 'today', '10/12', 'next week'):", parse_mode=ParseMode.HTML)
    return D_READY

async def step_d_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ready = (update.effective_message.text or "").strip()
    if len(ready) < 2:
        await update.effective_message.reply_text("Please send a date or phrase like 'today', 'tomorrow', 'next week':")
        return D_READY
    form = _get_driver_form(context)
    form.ready_date = ready
    context.user_data["driver_form"] = form

    # Company picker
    await _send_companies(update)
    return D_COMPANY

async def step_d_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    try:
        cid = int(text)
    except:
        await update.effective_message.reply_text("Send a numeric company ID from the list above.")
        return D_COMPANY
    form = _get_driver_form(context)
    form.company_id = cid
    context.user_data["driver_form"] = form

    # Confirmation
    summary = (
        f"<b>Confirm driver:</b>\n"
        f"Name: {form.full_name}\n"
        f"Phone: {form.phone}\n"
        f"CDL: {form.cdl_class}\n"
        f"Experience: {form.experience}\n"
        f"Ready: {form.ready_date}\n"
        f"Company ID: {form.company_id}\n\n"
        f"Send <b>yes</b> to save, or <b>no</b> to cancel."
    )
    await update.effective_message.reply_text(summary, parse_mode=ParseMode.HTML)
    return D_CONFIRM

async def step_d_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.effective_message.text or "").strip().lower()
    if ans not in {"y", "yes", "n", "no"}:
        await update.effective_message.reply_text("Please send 'yes' or 'no'.")
        return D_CONFIRM
    if ans in {"n", "no"}:
        await update.effective_message.reply_text("Cancelled.")
        return ConversationHandler.END

    # Save to DB (posting to company group happens in Phase 7)
       # Save to DB and post
    me = context.user_data.get("me")
    form = _get_driver_form(context)
    try:
        # Look up company chat (optional)
        company = await crud.get_company(form.company_id) if form.company_id else None
        company_chat_id = company.telegram_chat_id if company else None

        # Best-effort convert "experience" to months
        def _parse_months(s: str | None) -> int | None:
            if not s:
                return None
            import re
            m = re.search(r"(\d+)", s)
            return int(m.group(1)) if m else None

        driver_id = await crud.create_driver(
            kind="solo",
            recruiter_id=me.id,
            name=form.full_name,
            phone=form.phone,
            exp_months=_parse_months(form.experience),
            escrow=(form.cdl_class or None),
            ready_date=(form.ready_date or None),
            file_types="",
            file_ids="",
            company_id=form.company_id,
            company_chat_id=company_chat_id,
        )
        driver = await crud.find_driver_by_ref(driver_id)
        err = None
    except Exception as e:
        driver, err = None, str(e)

    if not driver:
        await update.effective_message.reply_text(f"❌ Failed to save driver: {err or 'unknown error'}")
        return ConversationHandler.END

    # success path
    await post_driver_card(context, driver)
    await update.effective_message.reply_text(
        f"✅ Driver saved with ID: {driver.id}\n"
        f"Posted to company chat (if linked). Replies will be forwarded to you."
    )
    return ConversationHandler.END


# ---------- Recruiter Utilities ----------
@require_login
async def cmd_set_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /set_status <driver_id> <status>
    Free-text status like: Ready / Waiting / Placed / Rejected
    """
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) != 3:
        await update.effective_message.reply_text("Usage: /set_status <driver_id> <status>")
        return
    _, did_s, status = parts
    try:
        did = int(did_s)
    except:
        await update.effective_message.reply_text("driver_id must be an integer.")
        return
    ok = await crud.set_driver_status(did, status)
    await update.effective_message.reply_text("✅ Status updated." if ok else "❌ Failed to update status.")

@require_login
async def cmd_my_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent drivers for current recruiter."""
    me = context.user_data.get("me")
    drivers = await crud.list_my_drivers(me.id, limit=20)
    if not drivers:
        await update.effective_message.reply_text("You have no drivers yet.")
        return
    lines = []
    for d in drivers:
                lines.append(f"{d.id}. {getattr(d, 'full_name', getattr(d, 'name', '-'))} — {d.phone} — status={getattr(d, 'status', '-')}")
    await update.effective_message.reply_text("\n".join(lines))

def wire_phase5_drivers(app: Application) -> None:
    """Register Phase 5 handlers."""
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("new_driver", cmd_new_driver)],
        states={
            D_NAME:   [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_name)],
            D_PHONE:  [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_phone)],
            D_CDL:    [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_cdl)],
            D_EXP:    [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_exp)],
            D_READY:  [MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_ready)],
            D_COMPANY:[MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_company)],
            D_CONFIRM:[MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, step_d_confirm)],
        },
        fallbacks=[],
        per_message=False,
    ))
    app.add_handler(CommandHandler("set_status", cmd_set_status))
    app.add_handler(CommandHandler("my_drivers", cmd_my_drivers))
# =======================================
#   PHASE 6 — Reports & Exports
# =======================================
# Features:
#   Admin / HR:
#     /weekly_report                    → high-level KPI summary (text) + (optional) CSV attachment
#     /export_csv                       → export ALL drivers to CSV (file)
#     /export_pdf <driver_id>           → export a single driver as PDF (file)
#
#   Recruiter:
#     /weekly_report_my                 → KPI summary for self
#
# Assumed crud API (async; adapt to your real functions if names differ):
#   - weekly_report_summary() -> tuple[str, bytes|None, str|None]
#       returns (text_summary, csv_bytes_or_None, csv_filename_or_None)
#   - weekly_report_summary_for_user(user_id:int) -> str
#   - export_all_drivers_csv() -> tuple[bytes, str]     # (csv_bytes, filename)
#   - generate_driver_pdf(driver_id:int) -> tuple[bytes, str]   # (pdf_bytes, filename)
#
# Notes:
# - If your existing crud uses different names, just adjust the calls below.
# - We always handle exceptions and show a clean error to the user (DM-only via decorators).

from io import BytesIO

# --------- Admin/HR: /weekly_report ---------
@require_role(ROLE_ADMIN, ROLE_HR)
async def cmd_weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    Sends a concise KPI summary as text. If a CSV is available from the crud helper,
    we attach it as well.
    """
    try:
        # Expect either: (text, csv_bytes, csv_filename)
        result = await crud.weekly_report_summary()
        if isinstance(result, tuple):
            text, csv_bytes, csv_name = (result + (None, None,))[:3]
        else:
            text, csv_bytes, csv_name = str(result), None, None
    except Exception as e:
        await update.effective_message.reply_text(f"❌ weekly_report failed: {e}")
        return

    # Send text summary
    if text:
        await update.effective_message.reply_text(text[:4000])  # Telegram limit guard

    # Attach CSV if provided
    if csv_bytes and isinstance(csv_bytes, (bytes, bytearray)):
        bio = BytesIO(csv_bytes)
        bio.name = csv_name or "weekly_report.csv"
        bio.seek(0)
        await update.effective_message.reply_document(document=bio, filename=bio.name, caption="Weekly KPI CSV")

# --------- Recruiter: /weekly_report_my ---------
@require_role(ROLE_RECRUITER, ROLE_HR, ROLE_ADMIN)
async def cmd_weekly_report_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    Personal KPI summary (for recruiter, or for whoever calls it).
    """
    try:
        text = await crud.weekly_report_summary_for_user(me.id)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ weekly_report_my failed: {e}")
        return
    await update.effective_message.reply_text(text[:4000] if text else "No activity yet.")

# --------- Admin/HR: /export_csv ---------
@require_role(ROLE_ADMIN, ROLE_HR)
async def cmd_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    Export ALL drivers to CSV.
    """
    try:
        csv_bytes, filename = await crud.export_all_drivers_csv()
    except Exception as e:
        await update.effective_message.reply_text(f"❌ export_csv failed: {e}")
        return

    if not csv_bytes:
        await update.effective_message.reply_text("No CSV data returned.")
        return

    bio = BytesIO(csv_bytes if isinstance(csv_bytes, (bytes, bytearray)) else bytes(csv_bytes))
    bio.name = filename or "drivers_export.csv"
    bio.seek(0)
    await update.effective_message.reply_document(document=bio, filename=bio.name, caption="All drivers (CSV)")

# --------- Admin/HR: /export_pdf <driver_id> ---------
@require_role(ROLE_ADMIN, ROLE_HR)
async def cmd_export_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    Export a single driver submission to PDF.
    Usage: /export_pdf <driver_id>
    """
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.effective_message.reply_text("Usage: /export_pdf <driver_id>")
        return

    try:
        driver_id = int(parts[1])
    except:
        await update.effective_message.reply_text("driver_id must be an integer.")
        return

    try:
        pdf_bytes, filename = await crud.generate_driver_pdf(driver_id)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ export_pdf failed: {e}")
        return

    if not pdf_bytes:
        await update.effective_message.reply_text("No PDF generated.")
        return

    bio = BytesIO(pdf_bytes if isinstance(pdf_bytes, (bytes, bytearray)) else bytes(pdf_bytes))
    bio.name = filename or f"driver_{driver_id}.pdf"
    bio.seek(0)
    await update.effective_message.reply_document(document=bio, filename=bio.name, caption=f"Driver {driver_id} (PDF)")

def wire_phase6_reports(app: Application) -> None:
    """Register exports & weekly reports commands."""
    app.add_handler(CommandHandler("weekly_report", cmd_weekly_report))
    app.add_handler(CommandHandler("weekly_report_my", cmd_weekly_report_my))
    app.add_handler(CommandHandler("export_csv", cmd_export_csv))
    app.add_handler(CommandHandler("export_pdf", cmd_export_pdf))
# ======================================================
#   PHASE 7 — Group-safe posting & strict reply capture
# ======================================================
# What this adds
# 1) When a driver is created (Phase 5), post a "driver card" to the company's
#    Telegram group (if the company has chat_id linked).
# 2) Store an *anchor* on the driver: (company_chat_id, group_msg_id).
#    That lets us map replies back to the correct driver later.
# 3) Add ONE group listener that:
#    - Runs only on replies (filters.REPLY)
#    - Only if they replied to *our* message (bot’s message)
#    - Looks up the driver by (chat_id, replied_message_id)
#    - Saves the reply in DB and notifies the recruiter in DM
# 4) Obey SAFE_GROUP_MODE to silence group behavior instantly.

from datetime import datetime

# --------- Expected CRUD helpers (adapt names if your crud differs) ----------
# await crud.get_company_by_id(company_id) -> Company | None   (has .telegram_chat_id)
# await crud.update_driver_anchor(driver_id, company_chat_id: str, group_msg_id: int) -> (ok, err)
# await crud.find_driver_by_group_msg(chat_id: int, replied_message_id: int) -> Driver | None
# await crud.create_driver_reply(driver_id: int, author: str, text: str, msg_id: int) -> (ok, err)
# await crud.get_user_by_id(user_id: int) -> User | None
# await crud.update_driver_status(driver_id: int, status: str) -> (ok, err)

# ---------- Driver card rendering ----------
def _driver_card_text(driver) -> str:
    """
    Format the message posted to the company group.
    Edit fields to match your model names precisely if they differ.
    """
    name  = getattr(driver, "full_name", getattr(driver, "name", "-"))
    phone = getattr(driver, "phone", "-")
    cdl   = getattr(driver, "cdl_class", getattr(driver, "escrow", "-"))
    exp   = getattr(driver, "experience", getattr(driver, "exp_months", "-"))
    ready = getattr(driver, "ready_date", getattr(driver, "ready", getattr(driver, "available_from", "-")))
    recruiter_id = getattr(driver, "recruiter_id", None)

    lines = [
        f"<b>Driver #{driver.id}</b>",
        f"Name: {name}",
        f"Phone: {phone}",
        f"CDL: {cdl}",
        f"Experience: {exp}",
        f"Ready: {ready}",
    ]
    # If you want, add recruiter tag or internal note
    if recruiter_id:
        lines.append(f"Ref: recruiter_id={recruiter_id}")
    lines.append("\nReply to this message with your feedback. 👍/❌")
    lines.append("(Replies will be forwarded to the recruiter.)")
    return "\n".join(lines)

# ---------- Post driver card to company group & store anchor ----------
async def post_driver_card(context: ContextTypes.DEFAULT_TYPE, driver) -> None:
    """
    Posts the driver to its company's group (if linked) and stores the anchor (chat_id, message_id).
    If company has no chat_id set, we silently skip posting (recruiter can link later).
    """
    # 1) Load company
    company_id = getattr(driver, "company_id", None)
    if not company_id:
        return
    company = await crud.get_company(company_id)
    if not company or not getattr(company, "telegram_chat_id", None):
        return  # no linked chat, nothing to post

    chat_id = company.telegram_chat_id
    try:
        text = _driver_card_text(driver)
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        # 2) Store anchor
        await crud.set_driver_group_msg_id(driver.id, sent.message_id)
        # mirror the card into the Archive group (if configured)
        try:
            await archive_copy_message(context, from_chat_id=chat_id, message_id=sent.message_id)
        except Exception:
            pass

        # You may log ok/err; we keep silent in chat.
    except Exception:
        # Swallow errors to avoid breaking submission; you can log if you have logging.
        return

# ---------- Optional: keyword-to-status mapping on replies ----------
def _maybe_extract_status_from_text(text: str) -> str | None:
    """
    Minimal keyword mapping. If you don't want auto-status, return None always.
    """
    t = (text or "").lower()
    if any(w in t for w in ("approved", "approve", "ok", "okay", "good to go", "✅")):
        return "Approved"
    if any(w in t for w in ("reject", "pass", "no go", "❌", "decline")):
        return "Rejected"
    if any(w in t for w in ("interview", "call", "phone", "talk", "number")):
        return "Requested contact"
    return None

# ---------- Notify recruiter helper ----------
async def _notify_recruiter(context: ContextTypes.DEFAULT_TYPE, driver, author: str, text: str):
    recruiter_id = getattr(driver, "recruiter_id", None)
    if not recruiter_id:
        return
    rec = await crud.get_user(recruiter_id)
    tg_id = getattr(rec, "telegram_id", None)
    if not tg_id:
        return
    msg = (
        f"📩 <b>New reply</b> on Driver #{driver.id}\n"
        f"From: {author}\n"
        f"Message: {text[:1000]}\n"
    )
    try:
        await context.bot.send_message(chat_id=tg_id, text=msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ---------- Strict group inbox listener (replies only) ----------
async def company_inbox_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Runs only in groups, only for replies, and only if reply is to our bot's message.
    Then maps reply -> driver via (chat_id, replied_message_id), stores the reply, notifies recruiter,
    and optionally updates status if a keyword is detected.
    """
    if SAFE_GROUP_MODE:
        return

    chat = update.effective_chat
    msg = update.effective_message
    if not chat or chat.type not in ("group", "supergroup"):
        return
    if not msg or not msg.reply_to_message:
        return

    # Only process replies to OUR message
    try:
        me = await context.bot.get_me()
    except Exception:
        return
    if msg.reply_to_message.from_user and msg.reply_to_message.from_user.id != me.id:
        return

    # Find driver by (chat_id, replied_message_id)
    try:
        driver = await crud.find_driver_by_group_msg(chat.id, msg.reply_to_message.message_id)
    except Exception:
        driver = None
    if not driver:
        return

    # Record the reply
    author = (msg.from_user.full_name if msg.from_user else "Unknown")
    text = msg.text or (msg.caption or "")
    try:
        await crud.create_driver_reply(driver.id, author, text, msg.message_id)
    except Exception:
        pass

    # Optional status update from keywords
    status = _maybe_extract_status_from_text(text)
    if status:
        try:
            await crud.set_driver_status(driver.id, status)
        except Exception:
            pass

    # Notify recruiter in DM
    await _notify_recruiter(context, driver, author, text)

# ---------- Wiring ----------
def wire_phase7_groups(app: Application) -> None:
    """
    Register the single strict group listener.
    It fires only on replies (NOT on normal group chatter).
    """
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND,
            company_inbox_listener
        ),
        group=0
    )
# ==========================================
#   PHASE 8 — Polish, safety & conveniences
# ==========================================
# What this adds:
# 1) A *belt-and-suspenders* mute for any stray group chatter:
#       - silently swallows all non-reply, non-command group messages
# 2) /ask_update <driver_id> <message...>
#       - recruiter/HR/admin can ping the company group under the driver card
#       - companies reply under that thread; the Phase 7 listener captures it
# 3) Runtime mute toggles (admin only) without redeploy:
#       - /mute_groups   → forces all group processing OFF now
#       - /unmute_groups → re-enables strict reply processing
#
# Notes:
# - SAFE_GROUP_MODE (env) still exists as a hard kill-switch on deploy.
# - RUNTIME mute is an in-memory flag you can flip live.

from typing import cast

# --------- (1) Stray group chatter mute ---------
async def _mute_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally do nothing; this prevents any accidental handlers from firing.
    return

# --------- (2) Ask update utility ---------
@require_role(ROLE_RECRUITER, ROLE_HR, ROLE_ADMIN)
async def cmd_ask_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    """
    /ask_update <driver_id> <message...>
    Posts a prompt into the company's group as a reply to the driver card (if anchor exists).
    Companies can reply under that, and Phase 7 listener will route replies to the recruiter.
    """
    text = (update.effective_message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await update.effective_message.reply_text("Usage: /ask_update <driver_id> <message>")
        return

    _, driver_s, msg = parts
    try:
        driver_id = int(driver_s)
    except:
        await update.effective_message.reply_text("driver_id must be an integer.")
        return

    # Load driver + its company & anchor
    try:
        driver = await crud.find_driver_by_ref(driver_id)
    except Exception as e:
        await update.effective_message.reply_text(f"Could not load driver: {e}")
        return
    if not driver:
        await update.effective_message.reply_text("Driver not found.")
        return

    company = await crud.get_company(getattr(driver, "company_id", None))
    if not company or not getattr(company, "telegram_chat_id", None):
        await update.effective_message.reply_text("This driver’s company has no linked chat yet.")
        return

    chat_id = company.telegram_chat_id
    reply_to_msg_id = getattr(driver, "group_msg_id", None)  # anchor set in Phase 7

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=(f"📝 <b>Update request for Driver #{driver.id}</b>\n{msg}"),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_msg_id if reply_to_msg_id else None,
            allow_sending_without_reply=True,
        )
        # We DO NOT overwrite the driver anchor here; keep the original card as the primary anchor.
        await update.effective_message.reply_text("✅ Update request sent to the company group.")
    except Exception as e:
        await update.effective_message.reply_text(f"Failed to send: {e}")

# --------- (3) Runtime group mute toggles (admin only) ---------
RUNTIME_GROUP_MUTE = False  # in-memory flag

@require_role(ROLE_ADMIN)
async def cmd_mute_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    global RUNTIME_GROUP_MUTE
    RUNTIME_GROUP_MUTE = True
    await update.effective_message.reply_text("🔇 Runtime group processing muted (replies will NOT be processed).")

@require_role(ROLE_ADMIN)
async def cmd_unmute_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = context.user_data.get("me")
    global RUNTIME_GROUP_MUTE
    RUNTIME_GROUP_MUTE = False
    await update.effective_message.reply_text("🔊 Runtime group processing re-enabled (strict replies-only).")

# Patch the Phase 7 listener to honor RUNTIME_GROUP_MUTE:
_prev_company_inbox_listener = company_inbox_listener  # keep ref

async def company_inbox_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if SAFE_GROUP_MODE or RUNTIME_GROUP_MUTE:
        return
    # delegate to the original strict handler
    return await _prev_company_inbox_listener(update, context)

def wire_phase8_polish(app: Application) -> None:
    """
    Register polish handlers.
    1) Mute stray group messages (must be an early group to swallow noise).
    2) Wire /ask_update and runtime mute toggles.
    """
    # 1) Swallow every non-reply, non-command group message
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.REPLY & ~filters.COMMAND,
            _mute_groups
        ),
        group=-1  # earlier than anything else
    )

    # 2) Commands (DM-only via decorators)
    app.add_handler(CommandHandler("ask_update", cmd_ask_update))
    app.add_handler(CommandHandler("mute_groups", cmd_mute_groups))
    app.add_handler(CommandHandler("unmute_groups", cmd_unmute_groups))
    
if __name__ == "__main__":
    # Avoid "RuntimeError: This event loop is already running" on Render/Jupyter-like runtimes.
    import nest_asyncio  # make sure 'nest_asyncio' is in requirements.txt
    nest_asyncio.apply()

    app = main()
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(app.run_polling(allowed_updates=Update.ALL_TYPES))
    except RuntimeError:
        # If a loop is already running (some platforms), schedule and keep alive
        asyncio.get_event_loop().create_task(app.run_polling(allowed_updates=Update.ALL_TYPES))
        asyncio.get_event_loop().run_forever()
