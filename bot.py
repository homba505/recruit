# bot.py
import asyncio
import os
import textwrap
from typing import Dict, Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from sqlalchemy.ext.asyncio import AsyncSession

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

from db import Base, engine, AsyncSessionLocal
from db_models import User, Company
import crud


# =========================
# CONFIG / CONSTANTS
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "HOMBA")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "fayzo2008")

LOGO_PATH = os.getenv("LOGO_PATH", "homba_logo.png")  # optional logo beside bot.py
PDF_DIR = os.getenv("PDF_DIR", "pdf_out")             # where PDFs are written


# =========================
# STATE MACHINE
# =========================
(
    S_LOGIN_USERNAME,
    S_LOGIN_PASSWORD,
    S_NEW_KIND,
    S_NEW_NAME,
    S_NEW_PHONE,
    S_NEW_EXP,
    S_NEW_ESCROW,
    S_NEW_READYDATE,
    S_NEW_FILE1,
    S_NEW_FILE2,
    S_PICK_COMPANY,
) = range(11)


# sessions & temp buffers
SESSIONS: Dict[int, int] = {}               # chat_id -> user_id
NEW_APP: Dict[int, Dict] = {}               # per-chat draft application


# =========================
# UTIL & BOOTSTRAP
# =========================
async def get_db_session() -> AsyncSession:
    return AsyncSessionLocal()  # type: ignore


async def seed_bootstrap() -> None:
    """Create tables and ensure default admin exists."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    admin = await crud.get_user_by_username(DEFAULT_ADMIN_USERNAME)
    if not admin:
        await crud.create_user(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, role="admin")


def need_login() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="login")]])


def ensure_pdf_dir():
    os.makedirs(PDF_DIR, exist_ok=True)


def clean_text(s: Optional[str]) -> str:
    return s or "—"


async def require_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[User]:
    user_id = SESSIONS.get(chat_id)
    if not user_id:
        return None
    async with await get_db_session() as s:
        u = await s.get(User, user_id)
        return u


# =========================
# PDF GENERATION (2 pages)
# =========================
async def generate_driver_pdf(
    driver_id: int,
    kind: str,
    name: Optional[str],
    phone: Optional[str],
    exp_months: Optional[int],
    escrow: Optional[str],
    ready_date: Optional[str],
    file_ids: Tuple[Optional[str], Optional[str]],
    bot_context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """
    Page 1: summary header
    Page 2: CDL + Medical (full width)
    """
    ensure_pdf_dir()
    pdf_path = os.path.join(PDF_DIR, f"driver_{driver_id}.pdf")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    W, H = A4

    # Header bar
    c.setFillColorRGB(0.066, 0.285, 0.43)
    c.rect(0, H - 70, W, 70, fill=1, stroke=0)

    # Title
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(30, H - 40, "HOMBA — Driver Application")

    # Body
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 12)
    y = H - 110
    left = 40

    def line(lbl: str, val: str):
        nonlocal y
        c.setFont("Helvetica-Bold", 12)
        c.drawString(left, y, lbl)
        c.setFont("Helvetica", 12)
        c.drawString(left + 170, y, val)
        y -= 24

    line("Application Type:", (kind or "").upper())
    line("Driver Name:", clean_text(name))
    line("Phone:", clean_text(phone))
    line("Experience (months):", str(exp_months) if exp_months is not None else "—")
    line("Escrow:", clean_text(escrow))
    line("Ready Date:", clean_text(ready_date))
    y -= 6
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(left, y, "Generated automatically by HOMBA Recruit Bot")
    c.showPage()

    # Page 2 — docs
    # Download files to temp
    tmp_files: list[str] = []
    for fid in [file_ids[0], file_ids[1]]:
        if not fid:
            tmp_files.append("")
            continue
        try:
            f = await bot_context.bot.get_file(fid)
            tmp_path = os.path.join(PDF_DIR, f"tmp_{driver_id}_{len(tmp_files)}.bin")
            await f.download_to_drive(custom_path=tmp_path)
            tmp_files.append(tmp_path)
        except Exception:
            tmp_files.append("")

    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, H - 60, "Documents")
    c.setFont("Helvetica", 12)
    c.drawString(40, H - 80, "CDL and Medical Card")

    def draw_image_center(img_path: str, y_top: float, height: float):
        try:
            c.drawImage(img_path, 40, y_top - height, width=W - 80, height=height, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass

    top = H - 110
    each_height = (H - 160) / 2
    if tmp_files[0]:
        draw_image_center(tmp_files[0], top, each_height)
    if tmp_files[1]:
        draw_image_center(tmp_files[1], top - each_height - 20, each_height)

    c.showPage()
    c.save()

    # cleanup temp
    for p in tmp_files:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    return pdf_path


# =========================
# COMMANDS & FLOWS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await seed_bootstrap()
    chat_id = update.effective_chat.id
    u = await require_user(context, chat_id)
    if u:
        await update.effective_chat.send_message(
            f"Hi {u.username}! 👋\nUse /new to submit a driver.\nAdmins: /admin"
        )
    else:
        await update.effective_chat.send_message(
            "Welcome to HOMBA Recruit Bot!\nPlease log in to continue.",
            reply_markup=need_login(),
        )


# ---- LOGIN ----
async def cb_login_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Enter username:")
    return S_LOGIN_USERNAME


async def login_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["login_username"] = update.message.text.strip()
    await update.message.reply_text("Enter password:")
    return S_LOGIN_PASSWORD


async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uname = context.user_data.get("login_username")
    pwd = update.message.text.strip()

    user = await crud.get_user_by_username(uname)
    if not user or user.password_hash != pwd or not user.is_active:
        await update.message.reply_text("❌ Invalid credentials or inactive user.")
        return ConversationHandler.END

    # bind telegram id
    await crud.set_user_telegram_id(user.id, str(update.effective_user.id))
    SESSIONS[update.effective_chat.id] = user.id

    await update.message.reply_text(f"✅ Logged in as {user.username}. Use /new to submit a driver.")
    return ConversationHandler.END


# ---- NEW DRIVER ----
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u:
        await update.effective_chat.send_message("You need to log in.", reply_markup=need_login())
        return ConversationHandler.END

    NEW_APP[update.effective_chat.id] = {"kind": None, "file_ids": [None, None]}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("SOLO", callback_data="kind:solo"),
         InlineKeyboardButton("TEAM", callback_data="kind:team"),
         InlineKeyboardButton("OWNER-OP", callback_data="kind:owner_op")]
    ])
    await update.effective_chat.send_message("Choose application type:", reply_markup=kb)
    return S_NEW_KIND


async def cb_pick_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    kind = q.data.split(":")[1]
    NEW_APP[chat_id]["kind"] = kind
    await q.edit_message_text(f"Type selected: {kind.upper()}\n\nDriver name?")
    return S_NEW_NAME


async def take_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    NEW_APP[chat_id]["name"] = update.message.text.strip()
    await update.message.reply_text("Phone number?")
    return S_NEW_PHONE


async def take_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    NEW_APP[chat_id]["phone"] = update.message.text.strip()
    await update.message.reply_text("Experience in months? (send number or '-' )")
    return S_NEW_EXP


async def take_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t = update.message.text.strip()
    NEW_APP[chat_id]["exp_months"] = int(t) if t.isdigit() else None
    await update.message.reply_text("Escrow? (or '-')")
    return S_NEW_ESCROW


async def take_escrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t = update.message.text.strip()
    NEW_APP[chat_id]["escrow"] = None if t == "-" else t
    await update.message.reply_text("Ready date? (or '-')")
    return S_NEW_READYDATE


async def take_ready(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    t = update.message.text.strip()
    NEW_APP[chat_id]["ready_date"] = None if t == "-" else t
    await update.message.reply_text("Send CDL photo/document")
    return S_NEW_FILE1


async def take_file1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    fid = None
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        t = "photo"
    elif update.message.document:
        fid = update.message.document.file_id
        t = "document"
    else:
        await update.message.reply_text("Please send a photo or a document.")
        return S_NEW_FILE1

    NEW_APP[chat_id]["file_ids"][0] = fid
    NEW_APP[chat_id]["file_types_1"] = t
    await update.message.reply_text("Now send MEDICAL CARD photo/document")
    return S_NEW_FILE2


async def take_file2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    fid = None
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        t = "photo"
    elif update.message.document:
        fid = update.message.document.file_id
        t = "document"
    else:
        await update.message.reply_text("Please send a photo or a document.")
        return S_NEW_FILE2

    NEW_APP[chat_id]["file_ids"][1] = fid
    NEW_APP[chat_id]["file_types_2"] = t

    # pick company
    companies = await crud.list_companies()
    if not companies:
        await update.message.reply_text("No companies configured. Ask admin to add one in /admin.")
        return ConversationHandler.END

    rows = [[InlineKeyboardButton(f"{c.id}. {c.name}", callback_data=f"pickco:{c.id}")] for c in companies]
    await update.message.reply_text("Pick a company to post to:", reply_markup=InlineKeyboardMarkup(rows))
    return S_PICK_COMPANY


async def cb_pick_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    user = await require_user(context, chat_id)
    if not user:
        await q.edit_message_text("You need to log in.", reply_markup=need_login())
        return ConversationHandler.END

    company_id = int(q.data.split(":")[1])
    company = await crud.get_company(company_id)
    if not company:
        await q.edit_message_text("Company not found.")
        return ConversationHandler.END

    d = NEW_APP.get(chat_id, {})
    file_types = f"{d.get('file_types_1','')},{d.get('file_types_2','')}"
    file_ids = f"{d['file_ids'][0] or ''}|{d['file_ids'][1] or ''}"

    driver_id = await crud.create_driver(
        kind=d["kind"],
        recruiter_id=user.id,
        name=d.get("name"),
        phone=d.get("phone"),
        exp_months=d.get("exp_months"),
        escrow=d.get("escrow"),
        ready_date=d.get("ready_date"),
        file_types=file_types,
        file_ids=file_ids,
    )

    # compose & send
    text_msg = textwrap.dedent(f"""
        <b>New driver application</b>

        <b>Type:</b> {d['kind'].upper()}
        <b>Name:</b> {d.get('name') or '—'}
        <b>Phone:</b> {d.get('phone') or '—'}
        <b>Experience (months):</b> {d.get('exp_months') if d.get('exp_months') is not None else '—'}
        <b>Escrow:</b> {d.get('escrow') or '—'}
        <b>Ready date:</b> {d.get('ready_date') or '—'}
    """).strip()

    try:
        sent = await context.bot.send_message(
            chat_id=int(company.telegram_chat_id),
            text=text_msg,
            parse_mode=ParseMode.HTML,
        )
        await crud.set_driver_group_msg_id(driver_id, sent.message_id)
    except BadRequest as e:
        await q.edit_message_text(f"Failed to post to company group: {e.message}")
        return ConversationHandler.END

    # send media
    media = []
    fid1, fid2 = d["file_ids"]
    if fid1:
        media.append(InputMediaPhoto(media=fid1, caption="CDL"))
    if fid2:
        media.append(InputMediaPhoto(media=fid2, caption="Medical Card"))
    if media:
        try:
            await context.bot.send_media_group(chat_id=int(company.telegram_chat_id), media=media)
        except BadRequest:
            if fid1:
                try: await context.bot.send_photo(int(company.telegram_chat_id), fid1, caption="CDL")
                except Exception: pass
            if fid2:
                try: await context.bot.send_photo(int(company.telegram_chat_id), fid2, caption="Medical Card")
                except Exception: pass

    # send PDF
    pdf_path = await generate_driver_pdf(
        driver_id=driver_id,
        kind=d["kind"],
        name=d.get("name"),
        phone=d.get("phone"),
        exp_months=d.get("exp_months"),
        escrow=d.get("escrow"),
        ready_date=d.get("ready_date"),
        file_ids=(fid1, fid2),
        bot_context=context,
    )
    await crud.save_pdf(driver_id, pdf_path)

    try:
        await context.bot.send_document(
            chat_id=int(company.telegram_chat_id),
            document=open(pdf_path, "rb"),
            filename=os.path.basename(pdf_path),
            caption="Driver Summary (PDF)",
        )
    except Exception:
        pass

    await q.edit_message_text("✅ Submitted to company.")
    NEW_APP.pop(chat_id, None)
    return ConversationHandler.END


# ---- ADMIN PANEL ----
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        await update.effective_chat.send_message("Admins only.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Users", callback_data="admin:users"),
         InlineKeyboardButton("🏢 Companies", callback_data="admin:companies")],
        [InlineKeyboardButton("🏆 Send Leaderboard (now)", callback_data="admin:leaderboard"),
         InlineKeyboardButton("📊 Send Weekly Report (now)", callback_data="admin:report")],
    ])
    await update.effective_chat.send_message("Admin panel:", reply_markup=kb)


async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data               # e.g. "admin:users", "user:add", "co:chat"
    parts = data.split(":", 1)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    # MAIN ADMIN MENU
    if head == "admin":
        sub = tail
        if sub == "users":
            users = await crud.list_users()
            lines = [f"{u.id}. {u.username} ({u.role}) {'✅' if u.is_active else '❌'}" for u in users]
            text = "👤 <b>Users</b>\n" + ("\n".join(lines) or "No users")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add", callback_data="user:add"),
                 InlineKeyboardButton("♻️ Toggle Active", callback_data="user:toggle"),
                 InlineKeyboardButton("✏️ Rename", callback_data="user:rename")],
                [InlineKeyboardButton("🔑 Change Password", callback_data="user:pass"),
                 InlineKeyboardButton("🗑 Delete", callback_data="user:del")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]
            ])
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

        elif sub == "companies":
            companies = await crud.list_companies()
            lines = [f"{c.id}. {c.name} — <code>{c.telegram_chat_id}</code>" for c in companies]
            text = "🏢 <b>Companies</b>\n" + ("\n".join(lines) or "No companies")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add", callback_data="co:add"),
                 InlineKeyboardButton("✏️ Rename", callback_data="co:rename")],
                [InlineKeyboardButton("🔁 Change Chat ID", callback_data="co:chat"),
                 InlineKeyboardButton("🗑 Delete", callback_data="co:del")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]
            ])
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

        elif sub == "leaderboard":
            await send_leaderboard_now(update, context, edit=True)

        elif sub == "report":
            await send_report_now(update, context, edit=True)

        elif sub == "back":
            await admin_menu(update, context)

        return

    # USER ACTION BUTTONS -> reply with slash-command guide
    if head == "user":
        action = tail
        if action == "add":
            await q.edit_message_text(
                "➕ To add a user:\n<code>/add_user &lt;username&gt; &lt;password&gt; [role]</code>\n\n"
                "Example:\n<code>/add_user Ali secret123 recruiter</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "toggle":
            await q.edit_message_text(
                "♻️ Toggle active status:\n(no inline flow yet)\nUse SQL or add a quick command if needed.",
                parse_mode=ParseMode.HTML
            )
        elif action == "rename":
            await q.edit_message_text(
                "✏️ Rename user:\n<code>/rename_user &lt;user_id&gt; &lt;new_username&gt;</code>\n\n"
                "Example:\n<code>/rename_user 3 AliNew</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "pass":
            await q.edit_message_text(
                "🔑 Change password:\n<code>/set_pass &lt;user_id&gt; &lt;new_password&gt;</code>\n\n"
                "Example:\n<code>/set_pass 3 newP@ss</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "del":
            await q.edit_message_text(
                "🗑 Delete user:\n<code>/del_user &lt;user_id&gt;</code>\n\n"
                "Example:\n<code>/del_user 3</code>",
                parse_mode=ParseMode.HTML
            )
        return

    # COMPANY ACTION BUTTONS -> reply with slash-command guide
    if head == "co":
        action = tail
        if action == "add":
            await q.edit_message_text(
                "➕ Add company:\n<code>/add_company &lt;name&gt; &lt;chat_id&gt;</code>\n\n"
                "Example:\n<code>/add_company SwiftTrucking -1001234567890</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "rename":
            await q.edit_message_text(
                "✏️ Rename company:\n<code>/rename_company &lt;company_id&gt; &lt;new_name&gt;</code>\n\n"
                "Example:\n<code>/rename_company 2 UltraLogistics</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "chat":
            await q.edit_message_text(
                "🔁 Change company chat:\n<code>/set_company_chat &lt;company_id&gt; &lt;chat_id&gt;</code>\n\n"
                "Example:\n<code>/set_company_chat 2 -1001234567890</code>",
                parse_mode=ParseMode.HTML
            )
        elif action == "del":
            await q.edit_message_text(
                "🗑 Delete company:\n<code>/del_company &lt;company_id&gt;</code>\n\n"
                "Example:\n<code>/del_company 2</code>",
                parse_mode=ParseMode.HTML
            )
        return


# ---- ADMIN QUICK COMMANDS ----
async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /add_user <username> <password> [role]")
        return
    role = context.args[2] if len(context.args) >= 3 else "recruiter"
    ok, err = await crud.create_user(context.args[0], context.args[1], role=role)
    await update.effective_chat.send_message("✅ Created" if ok else f"❌ {err or 'Failed'}")


async def rename_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /rename_user <user_id> <new_username>")
        return
    ok, err = await crud.update_user_username(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("✅ Renamed" if ok else f"❌ {err or 'Failed'}")


async def pass_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /set_pass <user_id> <new_password>")
        return
    await crud.update_user_password(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("✅ Password changed")


async def del_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if not context.args:
        await update.effective_chat.send_message("Usage: /del_user <user_id>")
        return
    await crud.delete_user(int(context.args[0]))
    await update.effective_chat.send_message("🗑 Deleted")


async def add_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /add_company <name> <chat_id>")
        return
    ok, err = await crud.create_company(context.args[0], context.args[1])
    await update.effective_chat.send_message("✅ Added" if ok else f"❌ {err or 'Failed'}")


async def rename_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /rename_company <company_id> <new_name>")
        return
    ok, err = await crud.rename_company(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("✅ Renamed" if ok else f"❌ {err or 'Failed'}")


async def company_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /set_company_chat <company_id> <chat_id>")
        return
    await crud.change_company_chat_id(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("🔁 Chat ID changed")


async def del_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin":
        return
    if not context.args:
        await update.effective_chat.send_message("Usage: /del_company <company_id>")
        return
    await crud.delete_company(int(context.args[0]))
    await update.effective_chat.send_message("🗑 Deleted")


# ---- LEADERBOARD / REPORT (simple versions) ----
async def send_leaderboard_now(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    text = "🏆 Recruiter Leaderboard (weekly)\n(coming from stored stats; basic version)"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.effective_chat.send_message(text)


async def send_report_now(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    text = "📊 Weekly Report sent. (basic placeholder — extend as needed)"
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.effective_chat.send_message(text)


# ---- HELP & UNKNOWN ----
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
<b>HOMBA Recruit Bot – Commands</b>

👤 Recruiters:
  /start – begin / login
  /new – submit a new driver

👮 Admins:
  /admin – open Admin Panel
  /add_user <username> <password> [role]
  /rename_user <user_id> <new_username>
  /set_pass <user_id> <new_password>
  /del_user <user_id>
  /add_company <name> <chat_id>
  /rename_company <company_id> <new_name>
  /set_company_chat <company_id> <chat_id>
  /del_company <company_id>
"""
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("Unknown command. Type /help for the list.")


# =========================
# STARTUP (single, Python 3.12/3.13-safe)
# =========================
def _build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("admin", admin_menu))

    app.add_handler(CommandHandler("add_user", add_user_cmd))
    app.add_handler(CommandHandler("rename_user", rename_user_cmd))
    app.add_handler(CommandHandler("set_pass", pass_user_cmd))
    app.add_handler(CommandHandler("del_user", del_user_cmd))

    app.add_handler(CommandHandler("add_company", add_company_cmd))
    app.add_handler(CommandHandler("rename_company", rename_company_cmd))
    app.add_handler(CommandHandler("set_company_chat", company_chat_cmd))
    app.add_handler(CommandHandler("del_company", del_company_cmd))

    # login flow
    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_login_button, pattern="^login$")],
        states={
            S_LOGIN_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            S_LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        name="login",
    )
    app.add_handler(login_conv)

    # new driver flow
    new_conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            S_NEW_KIND: [CallbackQueryHandler(cb_pick_kind, pattern=r"^kind:.+$")],
            S_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_name)],
            S_NEW_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_phone)],
            S_NEW_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_exp)],
            S_NEW_ESCROW: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_escrow)],
            S_NEW_READYDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_ready)],
            S_NEW_FILE1: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, take_file1)],
            S_NEW_FILE2: [MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, take_file2)],
            S_PICK_COMPANY: [CallbackQueryHandler(cb_pick_company, pattern=r"^pickco:\d+$")],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        name="new_driver",
    )
    app.add_handler(new_conv)

    # unknown commands (keep last)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app


async def main():
    await seed_bootstrap()
    app = _build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
