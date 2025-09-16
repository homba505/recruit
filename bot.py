# bot.py
import asyncio
import os
import re
import textwrap
from typing import Dict, Optional, Tuple, List

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from db import Base, engine
from db_models import User, Company
import crud

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "HOMBA")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "fayzo2008")

PDF_DIR = os.getenv("PDF_DIR", "pdf_out")

# Assets & optional mirror/audit chats
ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
LOGO_PATH = os.path.join(ASSETS_DIR, "logo.png")  # put your file at assets/logo.png

ARCHIVE_CHAT_ID = os.getenv("ARCHIVE_CHAT_ID")  # optional backup group/channel id
AUDIT_CHAT_ID = os.getenv("AUDIT_CHAT_ID")      # optional audit group/channel id

# =========================
# STATES
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
    S_NOTE_WAIT,           # new state for notes flow
) = range(12)

# sessions & temp
SESSIONS: Dict[int, int] = {}         # chat_id -> user_id
NEW_APP: Dict[int, Dict] = {}         # per-chat draft app

def ensure_pdf_dir():
    os.makedirs(PDF_DIR, exist_ok=True)

async def _try_send_audit(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Send audit message if AUDIT_CHAT_ID is configured."""
    if not AUDIT_CHAT_ID:
        return
    try:
        chat_id = int(AUDIT_CHAT_ID) if AUDIT_CHAT_ID.lstrip("-").isdigit() else AUDIT_CHAT_ID
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass

async def seed_bootstrap() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # ensure default admin exists
    admin = await crud.get_user_by_username(DEFAULT_ADMIN_USERNAME)
    if not admin:
        await crud.create_user(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, role="admin")

async def require_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[User]:
    uid = SESSIONS.get(chat_id)
    if not uid:
        return None
    return await crud.get_user(uid)

# =========================
# PDF generation
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
    ensure_pdf_dir()
    pdf_path = os.path.join(PDF_DIR, f"driver_{driver_id}.pdf")

    c = canvas.Canvas(pdf_path, pagesize=A4)
    W, H = A4

    # draw logo if present (page 1)
    try:
        if os.path.exists(LOGO_PATH):
            c.drawImage(LOGO_PATH, 35, H - 70, width=120, height=45, preserveAspectRatio=True, mask='auto')
    except Exception:
        pass

    # Header bar
    c.setFillColorRGB(0.066, 0.285, 0.43)
    c.rect(0, H - 70, W, 70, fill=1, stroke=0)
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
    line("Driver Name:", name or "—")
    line("Phone:", phone or "—")
    line("Experience (months):", str(exp_months) if exp_months is not None else "—")
    line("Escrow:", escrow or "—")
    line("Ready Date:", ready_date or "—")
    y -= 6
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(left, y, "Generated automatically by HOMBA Recruit Bot")

    # Page 2: docs
    c.showPage()
    # logo on page 2 as well (optional)
    try:
        if os.path.exists(LOGO_PATH):
            c.drawImage(LOGO_PATH, 35, H - 70, width=120, height=45, preserveAspectRatio=True, mask='auto')
    except Exception:
        pass

    # Download telegram files to temp
    tmp_files: List[str] = []
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

    for p in tmp_files:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    return pdf_path

# =========================
# Commands & flows
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await seed_bootstrap()
    chat_id = update.effective_chat.id
    u = await require_user(context, chat_id)
    if u:
        await update.effective_chat.send_message(
            f"Hi {u.username}! 👋\nUse /new to submit a driver.\nAdmins/HR: /admin"
        )
    else:
        await update.effective_chat.send_message(
            "Welcome to HOMBA Recruit Bot!\nPlease log in to continue.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="login")]]),
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
    # bcrypt verification + active
    if not user or not user.is_active or not crud.check_pw(pwd, user.password_hash):
        await update.message.reply_text("❌ Invalid credentials or inactive user.")
        return ConversationHandler.END

    await crud.set_user_telegram_id(user.id, str(update.effective_user.id))
    SESSIONS[update.effective_chat.id] = user.id

    await update.message.reply_text(f"✅ Logged in as {user.username}. Use /new to submit a driver.")
    return ConversationHandler.END

# ---- NEW DRIVER ----
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u:
        await update.effective_chat.send_message("You need to log in.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="login")]]))
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
        await q.edit_message_text("You need to log in.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="login")]]))
        return ConversationHandler.END

    company_id = int(q.data.split(":")[1])
    company = await crud.get_company(company_id)
    if not company:
        await q.edit_message_text("Company not found.")
        return ConversationHandler.END

    d = NEW_APP.get(chat_id, {})
    file_types = f"{d.get('file_types_1','')},{d.get('file_types_2','')}"
    fid1, fid2 = d["file_ids"]
    file_ids = f"{fid1 or ''}|{fid2 or ''}"

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
        company_id=company.id,
        company_chat_id=company.telegram_chat_id,
    )

    # compose & send (include Ref)
    text_msg = textwrap.dedent(f"""
        <b>New driver application</b>

        <b>Type:</b> {d['kind'].upper()}
        <b>Name:</b> {d.get('name') or '—'}
        <b>Phone:</b> {d.get('phone') or '—'}
        <b>Experience (months):</b> {d.get('exp_months') if d.get('exp_months') is not None else '—'}
        <b>Escrow:</b> {d.get('escrow') or '—'}
        <b>Ready date:</b> {d.get('ready_date') or '—'}

        Ref: <code>#D{driver_id}</code>
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
    if fid1:
        media.append(InputMediaPhoto(media=fid1, caption="CDL"))
    if fid2:
        media.append(InputMediaPhoto(media=fid2, caption="Medical Card"))
    if media:
        try:
            await context.bot.send_media_group(chat_id=int(company.telegram_chat_id), media=media)
        except BadRequest:
            # fallback one by one
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
            caption=f"Driver Summary (PDF) — Ref #D{driver_id}",
        )
    except Exception:
        pass

    # Mirror to archive (if configured)
    if ARCHIVE_CHAT_ID:
        try:
            mirror_chat = int(ARCHIVE_CHAT_ID) if ARCHIVE_CHAT_ID.lstrip("-").isdigit() else ARCHIVE_CHAT_ID
            await context.bot.send_document(
                chat_id=mirror_chat,
                document=open(pdf_path, "rb"),
                filename=os.path.basename(pdf_path),
                caption=f"[ARCHIVE] Driver Summary — Ref #D{driver_id} → {company.name}",
            )
        except Exception:
            pass

    # Audit line (if configured)
    await _try_send_audit(context, f"📨 Driver #{driver_id} submitted to {company.name} by {user.username}")

    await q.edit_message_text("✅ Submitted to company.")
    NEW_APP.pop(chat_id, None)
    return ConversationHandler.END

# ---- ADMIN PANEL ----
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u:
        return
    if u.role == "admin":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Users", callback_data="admin:users"),
             InlineKeyboardButton("🏢 Companies", callback_data="admin:companies")],
            [InlineKeyboardButton("🏆 Send Leaderboard (now)", callback_data="admin:leaderboard"),
             InlineKeyboardButton("📊 Send Weekly Report (now)", callback_data="admin:report")],
        ])
    elif u.role == "hr_manager":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Users", callback_data="admin:users")],
            [InlineKeyboardButton("🏆 Send Leaderboard (now)", callback_data="admin:leaderboard"),
             InlineKeyboardButton("📊 Send Weekly Report (now)", callback_data="admin:report")],
        ])
    else:
        await update.effective_chat.send_message("Admins/HR only.")
        return
    await update.effective_chat.send_message("Admin panel:", reply_markup=kb)

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    parts = data.split(":", 1)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    chat_id = q.message.chat_id
    me = await require_user(context, chat_id)
    if not me:
        await q.edit_message_text("You need to log in.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Login", callback_data="login")]]))
        return

    if head == "admin":
        sub = tail
        if sub == "users":
            if me.role == "admin":
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
            else:
                users = await crud.list_team(me.id)
                lines = [f"{u.id}. {u.username} ({'✅' if u.is_active else '❌'})" for u in users]
                text = "👤 <b>Your Team</b>\n" + ("\n".join(lines) or "No recruiters yet")
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add", callback_data="user:add"),
                     InlineKeyboardButton("✏️ Rename", callback_data="user:rename")],
                    [InlineKeyboardButton("🔑 Change Password", callback_data="user:pass")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]
                ])
                await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return

        if sub == "companies":
            if me.role != "admin":
                await q.edit_message_text("Admins only.")
                return
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
            return

        if sub == "leaderboard":
            await send_leaderboard_now(update, context, edit=True); return
        if sub == "report":
            await send_report_now(update, context, edit=True); return
        if sub == "back":
            await admin_menu(update, context); return

    if head == "user":
        action = tail
        if action == "add":
            hint = "HR: <code>/add_user &lt;username&gt; &lt;password&gt; recruiter</code>\nAdmin: add any role"
        elif action == "rename":
            hint = "<code>/rename_user &lt;user_id&gt; &lt;new_username&gt;</code>"
        elif action == "pass":
            hint = "<code>/set_pass &lt;user_id&gt; &lt;new_password&gt;</code>"
        elif action == "toggle":
            hint = "No inline toggle yet."
        elif action == "del":
            hint = "Admin only: <code>/del_user &lt;user_id&gt;</code>"
        else:
            hint = "Unknown."
        await q.edit_message_text(hint, parse_mode=ParseMode.HTML); return

    if head == "co":
        if me.role != "admin":
            await q.edit_message_text("Admins only."); return
        action = tail
        if action == "add":
            text = "➕ Add company:\n<code>/add_company &lt;name&gt; &lt;chat_id&gt;</code>"
        elif action == "rename":
            text = "✏️ Rename company:\n<code>/rename_company &lt;company_id&gt; &lt;new_name&gt;</code>"
        elif action == "chat":
            text = "🔁 Change company chat:\n<code>/set_company_chat &lt;company_id&gt; &lt;chat_id&gt;</code>"
        elif action == "del":
            text = "🗑 Delete company:\n<code>/del_company &lt;company_id&gt;</code>"
        else:
            text = "Unknown."
        await q.edit_message_text(text, parse_mode=ParseMode.HTML); return

# ---- ADMIN QUICK COMMANDS ----
async def add_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u: return

    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /add_user <username> <password> [role]")
        return

    req_role = context.args[2] if len(context.args) >= 3 else "recruiter"

    if u.role == "admin":
        ok, err = await crud.create_user(context.args[0], context.args[1], role=req_role, manager_id=None)
        await update.effective_chat.send_message("✅ Created" if ok else f"❌ {err or 'Failed'}")
        return

    if u.role == "hr_manager":
        if req_role != "recruiter":
            await update.effective_chat.send_message("HR Managers can only create recruiters.")
            return
        ok, err = await crud.create_user(context.args[0], context.args[1], role="recruiter", manager_id=u.id)
        await update.effective_chat.send_message("✅ Recruiter created" if ok else f"❌ {err or 'Failed'}")
        return

    await update.effective_chat.send_message("You don't have permission to add users.")

async def rename_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u: return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /rename_user <user_id> <new_username>")
        return
    target_id = int(context.args[0]); new_name = context.args[1]

    if u.role == "admin":
        ok, err = await crud.update_user_username(target_id, new_name)
        await update.effective_chat.send_message("✅ Renamed" if ok else f"❌ {err or 'Failed'}")
        return

    if u.role == "hr_manager":
        if not await crud.is_in_team(u.id, target_id):
            await update.effective_chat.send_message("You can only rename recruiters in your team."); return
        ok, err = await crud.update_user_username(target_id, new_name)
        await update.effective_chat.send_message("✅ Renamed" if ok else f"❌ {err or 'Failed'}")
        return

    await update.effective_chat.send_message("You don't have permission to rename users.")

async def pass_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u: return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /set_pass <user_id> <new_password>")
        return
    target_id = int(context.args[0]); new_pw = context.args[1]

    if u.role == "admin":
        await crud.update_user_password(target_id, new_pw)
        await update.effective_chat.send_message("✅ Password changed"); return

    if u.role == "hr_manager":
        if not await crud.is_in_team(u.id, target_id):
            await update.effective_chat.send_message("You can only change passwords for your team."); return
        await crud.update_user_password(target_id, new_pw)
        await update.effective_chat.send_message("✅ Password changed"); return

    await update.effective_chat.send_message("You don't have permission to change passwords.")

async def del_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u: return
    if not context.args:
        await update.effective_chat.send_message("Usage: /del_user <user_id>"); return
    if u.role != "admin":
        await update.effective_chat.send_message("Only admins can delete users."); return
    await crud.delete_user(int(context.args[0]))
    await update.effective_chat.send_message("🗑 Deleted")

async def add_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin": return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /add_company <name> <chat_id>"); return
    ok, err = await crud.create_company(context.args[0], context.args[1])
    await update.effective_chat.send_message("✅ Added" if ok else f"❌ {err or 'Failed'}")

async def rename_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin": return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /rename_company <company_id> <new_name>"); return
    ok, err = await crud.rename_company(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("✅ Renamed" if ok else f"❌ {err or 'Failed'}")

async def company_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin": return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /set_company_chat <company_id> <chat_id>"); return
    await crud.change_company_chat_id(int(context.args[0]), context.args[1])
    await update.effective_chat.send_message("🔁 Chat ID changed")

async def del_company_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role != "admin": return
    if not context.args:
        await update.effective_chat.send_message("Usage: /del_company <company_id>"); return
    await crud.delete_company(int(context.args[0]))
    await update.effective_chat.send_message("🗑 Deleted")

# ---- LEADERBOARD / REPORT stubs ----
async def send_leaderboard_now(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    text = "🏆 Recruiter Leaderboard (weekly)\n(placeholder)"
    if edit and update.callback_query: await update.callback_query.edit_message_text(text)
    else: await update.effective_chat.send_message(text)

async def send_report_now(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    text = "📊 Weekly Report sent. (placeholder)"
    if edit and update.callback_query: await update.callback_query.edit_message_text(text)
    else: await update.effective_chat.send_message(text)

# ---- HR / Status / View ----
VALID_STATUSES = {"approved", "waiting", "rejected"}

async def my_team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await require_user(context, update.effective_chat.id)
    if not me or me.role != "hr_manager":
        await update.effective_chat.send_message("Only HR Managers can view a team."); return
    team = await crud.list_team(me.id)
    if not team:
        await update.effective_chat.send_message("Your team is empty. Use /add_user to create recruiters."); return
    lines = [f"{u.id}. {u.username} ({'✅' if u.is_active else '❌'})" for u in team]
    await update.effective_chat.send_message("👥 <b>Your Recruiters</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

async def set_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u:
        return
    if len(context.args) < 2:
        await update.effective_chat.send_message("Usage: /set_status <driver_id> <approved|waiting|rejected>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Driver ID must be a number."); return
    new_status = context.args[1].lower()
    if new_status not in VALID_STATUSES:
        await update.effective_chat.send_message("Status must be one of: approved, waiting, rejected"); return

    d = await crud.find_driver_by_ref(driver_id)
    if not d:
        await update.effective_chat.send_message("Driver not found."); return

    if u.role == "admin":
        pass
    elif u.role == "hr_manager":
        recruiter = await crud.get_user(d.recruiter_id)
        if not recruiter or recruiter.manager_id != u.id:
            await update.effective_chat.send_message("You can only update status for your team's drivers.")
            return
    else:
        await update.effective_chat.send_message("Only admins or HR managers can set status.")
        return

    await crud.set_driver_status(driver_id, new_status)
    await update.effective_chat.send_message(f"✅ Status for #D{driver_id} set to {new_status}.")

    # notify recruiter
    recruiter = await crud.get_user(d.recruiter_id)
    if recruiter and recruiter.telegram_id:
        try:
            await context.bot.send_message(
                int(recruiter.telegram_id),
                f"ℹ️ Status update for <b>#D{driver_id}</b>: <b>{new_status}</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

async def driver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_chat.send_message("Usage: /driver <driver_id>"); return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.effective_chat.send_message("Driver ID must be a number."); return
    d = await crud.find_driver_by_ref(driver_id)
    if not d:
        await update.effective_chat.send_message("Driver not found."); return
    await update.effective_chat.send_message(
        f"#D{d.id} — status: <b>{d.status}</b>\nName: {d.name or '—'}\nPhone: {d.phone or '—'}",
        parse_mode=ParseMode.HTML
    )

# ---- Company Inbox (replies & follow-ups) ----
REF_RX = re.compile(r"#D(\d+)\b")

async def company_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not chat or chat.type not in ("group", "supergroup"):
        return

    text = (msg.text or msg.caption or "").strip()

    driver = None

    # 1) Direct reply to original post
    if msg.reply_to_message:
        try:
            replied_id = msg.reply_to_message.message_id
            driver = await crud.find_driver_by_group_msg(chat.id, replied_id)
        except Exception:
            driver = None

    # 2) Fallback: message contains Ref #D123
    if not driver and text:
        m = REF_RX.search(text)
        if m:
            ref_id = int(m.group(1))
            candidate = await crud.find_driver_by_ref(ref_id)
            # accept only if it’s the same company chat where we posted
            if candidate and candidate.company_chat_id and candidate.company_chat_id == str(chat.id):
                driver = candidate

    if not driver:
        return  # ignore unrelated messages

    # Log reply
    who = f"{msg.from_user.full_name} (@{msg.from_user.username})" if msg.from_user else "unknown"
    await crud.create_driver_reply(driver.id, who, text, msg.message_id)

    # Notify recruiter + HR
    try:
        recruiter = await crud.get_user(driver.recruiter_id)
        hr = await crud.get_user(recruiter.manager_id) if recruiter and recruiter.manager_id else None

        notice = f"💬 <b>Company replied</b> on <b>#D{driver.id}</b>\nFrom: {who}\n\n{text or '(no text)'}"
        if recruiter and recruiter.telegram_id:
            await context.bot.send_message(int(recruiter.telegram_id), notice, parse_mode=ParseMode.HTML)
        if hr and hr.telegram_id:
            await context.bot.send_message(int(hr.telegram_id), notice, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ---- NOTES (simple: HR/Admin only, stored in DriverReply) ----
async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /note <driver_id>  — then send the note text in the next message."""
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role not in ("hr_manager", "admin"):
        await update.effective_chat.send_message("Only HR/Admin can add notes.")
        return ConversationHandler.END
    args = (update.message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await update.effective_chat.send_message("Usage: /note <driver_id>")
        return ConversationHandler.END
    driver_id = int(args[1])
    context.user_data['note_driver_id'] = driver_id
    await update.effective_chat.send_message(f"✍️ Send your note text for driver #{driver_id} as a single message.")
    return S_NOTE_WAIT

async def take_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = await require_user(context, update.effective_chat.id)
    if not u or u.role not in ("hr_manager", "admin"):
        return ConversationHandler.END
    driver_id = context.user_data.get('note_driver_id')
    text = (update.message.text or '').strip()
    if not (driver_id and text):
        await update.effective_chat.send_message("Nothing to save.")
        return ConversationHandler.END
    await crud.create_driver_reply(driver_id, from_user=f"NOTE:{u.username}", text=text, message_id=update.message.message_id)
    await update.effective_chat.send_message(f"🗒️ Note saved for driver #{driver_id}.")
    await _try_send_audit(context, f"🗒️ Note added by {u.username} for driver #{driver_id}: {text[:140]}")
    context.user_data.pop('note_driver_id', None)
    return ConversationHandler.END

# ---- HELP & Logout & Unknown ----
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = await require_user(context, update.effective_chat.id)
    base = [
        "👤 <b>Recruiters</b>",
        "  /start – begin / login",
        "  /new – submit a new driver",
        "  /driver &lt;driver_id&gt; – view driver status",
        "  /logout – log out",
    ]
    hr = [
        "",
        "🧑‍💼 <b>HR Managers</b>",
        "  /my_team – list your recruiters",
        "  /add_user &lt;username&gt; &lt;password&gt; recruiter",
        "  /rename_user &lt;user_id&gt; &lt;new_username&gt;",
        "  /set_pass &lt;user_id&gt; &lt;new_password&gt;",
        "  /set_status &lt;driver_id&gt; &lt;approved|waiting|rejected&gt;",
        "  /note &lt;driver_id&gt; – add internal note",
    ]
    adm = [
        "",
        "👮 <b>Admins</b>",
        "  /admin – open Admin Panel",
        "  /add_user &lt;username&gt; &lt;password&gt; [role]",
        "  /rename_user &lt;user_id&gt; &lt;new_username&gt;",
        "  /set_pass &lt;user_id&gt; &lt;new_password&gt;",
        "  /del_user &lt;user_id&gt;",
        "  /add_company &lt;name&gt; &lt;chat_id&gt;",
        "  /rename_company &lt;company_id&gt; &lt;new_name&gt;",
        "  /set_company_chat &lt;company_id&gt; &lt;chat_id&gt;",
        "  /del_company &lt;company_id&gt;",
    ]
    lines = base[:]
    if me and me.role in ("hr_manager", "admin"): lines += hr
    if me and me.role == "admin": lines += adm
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in SESSIONS:
        SESSIONS.pop(chat_id, None)
        await update.effective_chat.send_message("✅ Logged out. Use /start to log in again.")
    else:
        await update.effective_chat.send_message("ℹ️ You are not logged in. Use /start to log in.")

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("Unknown command. Type /help for the list.")
# --- Chat ID helper ---
async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_chat.send_message(
        f"Chat ID: <code>{chat.id}</code>\nType: <b>{chat.type}</b>",
        parse_mode=ParseMode.HTML
    )

# =========================
# STARTUP
# =========================
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("admin", admin_menu))

    app.add_handler(CommandHandler("add_user", add_user_cmd))
    app.add_handler(CommandHandler("rename_user", rename_user_cmd))
    app.add_handler(CommandHandler("set_pass", pass_user_cmd))
    app.add_handler(CommandHandler("del_user", del_user_cmd))

    app.add_handler(CommandHandler("add_company", add_company_cmd))
    app.add_handler(CommandHandler("rename_company", rename_company_cmd))
    app.add_handler(CommandHandler("set_company_chat", company_chat_cmd))
    app.add_handler(CommandHandler("del_company", del_company_cmd))

    app.add_handler(CommandHandler("my_team", my_team_cmd))
    app.add_handler(CommandHandler("set_status", set_status_cmd))
    app.add_handler(CommandHandler("driver", driver_cmd))
# 🔹 Add this line right here:
    app.add_handler(CommandHandler("chatid", chatid))
    # login flow
    login_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_login_button, pattern=r"^login$")],
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

    # note flow
    note_conv = ConversationHandler(
        entry_points=[CommandHandler("note", cmd_note)],
        states={ S_NOTE_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, take_note_text)] },
        fallbacks=[],
        per_chat=True,
        per_user=True,
        name="note_flow",
    )
    app.add_handler(note_conv)

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

    # company inbox listener (all group messages, non-command)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, company_inbox))

    # admin panel callbacks
    app.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^(admin|user|co):"))

    # unknown commands last
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    return app

async def main():
    app = build_application()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await app.updater.wait()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(seed_bootstrap())
    asyncio.run(main())
