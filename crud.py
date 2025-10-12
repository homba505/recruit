
# crud.py — cleaned & completed
from __future__ import annotations

from typing import Optional, List, Tuple
from datetime import datetime, timedelta
import io
import csv

from sqlalchemy import select, update, delete, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from db import AsyncSessionLocal
from db_models import User, Company, Driver, DriverReply, PdfFile

import bcrypt


# ========= Password helpers =========
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


# ========= Users =========
async def get_user(user_id: int) -> Optional[User]:
    async with AsyncSessionLocal() as s:
        return await s.get(User, user_id)

async def get_user_by_username(username: str) -> Optional[User]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(User).where(User.username == username))
        return res.scalar_one_or_none()

async def set_user_telegram_id(user_id: int, tg_id: str) -> None:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return
        u.telegram_id = tg_id
        await s.commit()

async def list_users() -> List[User]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(User).order_by(User.id))
        return list(res.scalars().all())

async def list_team(manager_id: int) -> List[User]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(User).where(User.manager_id == manager_id).order_by(User.id)
        )
        return list(res.scalars().all())

async def is_in_team(manager_id: int, user_id: int) -> bool:
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(User.id).where(User.id == user_id, User.manager_id == manager_id)
        )
        return res.scalar_one_or_none() is not None

async def create_user(
    username: str,
    password: str,
    role: str = "recruiter",
    manager_id: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        u = User(
            username=username,
            password_hash=hash_pw(password),
            role=role,
            is_active=True,
            manager_id=manager_id,
        )
        s.add(u)
        try:
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "username_taken"

async def update_user_username(user_id: int, new_username: str) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False, "not_found"
        u.username = new_username
        try:
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "username_taken"

async def update_user_password(user_id: int, new_password: str) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False, "not_found"
        u.password_hash = hash_pw(new_password)
        await s.commit()
        return True, None

async def set_user_role(user_id: int, role: str) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False, "not_found"
        u.role = role
        await s.commit()
        return True, None

async def disable_user(user_id: int) -> None:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return
        u.is_active = False
        await s.commit()

async def enable_user(user_id: int) -> None:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return
        u.is_active = True
        await s.commit()


# ========= Companies =========
async def list_companies() -> List[Company]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Company).order_by(Company.id))
        return list(res.scalars().all())

async def create_company(name: str, telegram_chat_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        c = Company(name=name, telegram_chat_id=telegram_chat_id)
        s.add(c)
        try:
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "name_taken"

async def rename_company(company_id: int, new_name: str) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        c = await s.get(Company, company_id)
        if not c:
            return False, "not_found"
        c.name = new_name
        try:
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "name_taken"

async def change_company_chat_id(company_id: int, chat_id: str) -> None:
    async with AsyncSessionLocal() as s:
        c = await s.get(Company, company_id)
        if not c:
            return
        c.telegram_chat_id = chat_id
        await s.commit()

async def delete_company(company_id: int) -> None:
    async with AsyncSessionLocal() as s:
        c = await s.get(Company, company_id)
        if not c:
            return
        await s.delete(c)
        await s.commit()


# ========= Drivers =========
async def create_driver(
    recruiter_id: int,
    company_id: Optional[int] = None,
    company_chat_id: Optional[str] = None,
    group_msg_id: Optional[int] = None,
    kind: Optional[str] = None,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    exp_months: Optional[int] = None,
    escrow: Optional[str] = None,
    ready_date: Optional[str] = None,
    file_types: Optional[str] = None,
    file_ids: Optional[str] = None,
    status: str = "new",
) -> int:
    async with AsyncSessionLocal() as s:
        d = Driver(
            recruiter_id=recruiter_id,
            company_id=company_id,
            company_chat_id=company_chat_id,
            group_msg_id=group_msg_id,
            kind=kind,
            name=name,
            phone=phone,
            exp_months=exp_months,
            escrow=escrow,
            ready_date=ready_date,
            file_types=file_types,
            file_ids=file_ids,
            status=status,
        )
        s.add(d)
        await s.commit()
        await s.refresh(d)
        return d.id

async def find_driver_by_message_in_company_chat(chat_id: str, msg_id: int) -> Optional[Driver]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver).where(
                Driver.company_chat_id == str(chat_id),
                Driver.group_msg_id == msg_id
            )
        )
        return res.scalar_one_or_none()

async def find_driver_by_ref(driver_id: int) -> Optional[Driver]:
    async with AsyncSessionLocal() as s:
        return await s.get(Driver, driver_id)

# Alias expected by some handlers
async def find_driver_by_id(driver_id: int) -> Optional[Driver]:
    return await find_driver_by_ref(driver_id)

async def create_driver_reply(driver_id: int, from_user: str, text: str, message_id: int) -> None:
    async with AsyncSessionLocal() as s:
        r = DriverReply(driver_id=driver_id, from_user=from_user, text=text, tg_message_id=str(message_id))
        s.add(r)
        await s.commit()

async def list_my_drivers(recruiter_id: int, limit: int = 20) -> List[Driver]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver)
            .where(Driver.recruiter_id == recruiter_id)
            .order_by(desc(Driver.id))
            .limit(limit)
        )
        return list(res.scalars().all())


# ========= Exports & Reports =========
async def export_all_drivers_csv() -> tuple[bytes, str]:
    """
    Returns (csv_bytes, filename)
    """
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver)
            .options(joinedload(Driver.recruiter), joinedload(Driver.company))
            .order_by(Driver.id)
        )
        drivers = list(res.scalars().all())

    header = [
        "id", "created_at", "recruiter", "company",
        "kind", "name", "phone", "exp_months", "escrow", "ready_date", "status",
        "company_chat_id", "group_msg_id",
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)

    for d in drivers:
        created_at = ""
        ca = getattr(d, "created_at", None)
        if ca:
            try:
                created_at = ca.isoformat(timespec="seconds")
            except Exception:
                created_at = str(ca)

        w.writerow([
            getattr(d, "id", ""),
            created_at,
            getattr(getattr(d, "recruiter", None), "username", "") or "",
            getattr(getattr(d, "company", None), "name", "") or "",
            getattr(d, "kind", "") or "",
            getattr(d, "name", "") or "",
            getattr(d, "phone", "") or "",
            getattr(d, "exp_months", "") or "",
            getattr(d, "escrow", "") or "",
            getattr(d, "ready_date", "") or "",
            getattr(d, "status", "") or "",
            getattr(d, "company_chat_id", "") or "",
            getattr(d, "group_msg_id", "") or "",
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    filename = f"drivers_{datetime.utcnow().date().isoformat()}.csv"
    return csv_bytes, filename


async def weekly_report_summary() -> tuple[str, Optional[bytes], Optional[str]]:
    """
    Return (text, csv_bytes_or_None, csv_filename_or_None) for the last 7 days.
    """
    since = datetime.utcnow() - timedelta(days=7)
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver)
            .where(Driver.created_at >= since)
            .options(joinedload(Driver.recruiter), joinedload(Driver.company))
            .order_by(Driver.id)
        )
        drivers = list(res.scalars().all())

    if not drivers:
        return "No activity in the last 7 days.", None, None

    total = len(drivers)
    by_status: dict[str, int] = {}
    by_recruiter: dict[str, int] = {}

    for d in drivers:
        st = (getattr(d, "status", None) or "new").lower()
        by_status[st] = by_status.get(st, 0) + 1
        rname = getattr(getattr(d, "recruiter", None), "username", None) or f"id:{getattr(d, 'recruiter_id', '?')}"
        by_recruiter[rname] = by_recruiter.get(rname, 0) + 1

    status_lines = ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items()))
    top = ", ".join(f"{n}({c})" for n, c in sorted(by_recruiter.items(), key=lambda kv: kv[1], reverse=True)[:5])

    text = (
        f"📊 Weekly report (since {since.date().isoformat()}):\n"
        f"Total drivers: {total}\n"
        f"By status: {status_lines or '-'}\n"
        f"Top recruiters: {top or '-'}"
    )

    # Attach a CSV for the week
    header = ["id","created_at","recruiter","company","status","kind","name","phone","exp_months","escrow","ready_date"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for d in drivers:
        created_at = ""
        ca = getattr(d, "created_at", None)
        if ca:
            try:
                created_at = ca.isoformat(timespec="seconds")
            except Exception:
                created_at = str(ca)
        w.writerow([
            getattr(d,"id",""),
            created_at,
            getattr(getattr(d,"recruiter",None),"username","") or "",
            getattr(getattr(d,"company",None),"name","") or "",
            getattr(d,"status","") or "",
            getattr(d,"kind","") or "",
            getattr(d,"name","") or "",
            getattr(d,"phone","") or "",
            getattr(d,"exp_months","") or "",
            getattr(d,"escrow","") or "",
            getattr(d,"ready_date","") or "",
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    filename = f"weekly_{datetime.utcnow().date().isoformat()}.csv"
    return text, csv_bytes, filename


async def weekly_report_summary_for_user(user_id: int) -> str:
    """
    Return plain text summary for a single user for last 7 days.
    """
    since = datetime.utcnow() - timedelta(days=7)
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver)
            .where(Driver.created_at >= since, Driver.recruiter_id == user_id)
            .order_by(Driver.id)
        )
        drivers = list(res.scalars().all())

    if not drivers:
        return "No activity for you in the last 7 days."

    total = len(drivers)
    by_status: dict[str, int] = {}
    for d in drivers:
        st = (getattr(d, "status", None) or "new").lower()
        by_status[st] = by_status.get(st, 0) + 1
    status_lines = ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items()))
    return (f"📊 Your weekly report (since {since.date().isoformat()}):\n"
            f"Total drivers: {total}\nBy status: {status_lines or '-'}")


async def generate_driver_pdf(driver_id: int) -> tuple[Optional[bytes], Optional[str]]:
    """
    Create a simple PDF summary for a driver. Returns (pdf_bytes, filename) or (None, None).
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    import io as _io

    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver)
            .where(Driver.id == driver_id)
            .options(joinedload(Driver.recruiter), joinedload(Driver.company))
        )
        d = res.scalars().first()

    if not d:
        return None, None

    recruiter = getattr(getattr(d,"recruiter",None),"username","") or f"id:{getattr(d,'recruiter_id','?')}"
    company = getattr(getattr(d,"company",None),"name","") or ""
    created = getattr(d,"created_at",None)
    created_s = created.strftime("%Y-%m-%d %H:%M:%S") if created else ""

    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    y = h - 20*mm

    def line(txt: str, step: float = 8*mm, font=("Helvetica", 12)):
        nonlocal y
        c.setFont(*font)
        c.drawString(20*mm, y, txt)
        y -= step
from reportlab.lib.utils import ImageReader
import os

logo_path = os.path.join("assets", "logo.png")
if os.path.exists(logo_path):
    c.drawImage(ImageReader(logo_path), 20*mm, y - 15*mm, width=40*mm, preserveAspectRatio=True)
    y -= 25*mm  # move down after drawing the logo

c.setFont("Helvetica-Bold", 16)
c.drawString(20*mm, y, "Driver Submission")
y -= 10*mm

    line(f"Driver ID: {getattr(d,'id','')}")
    line(f"Created: {created_s}")
    line(f"Recruiter: {recruiter}")
    line(f"Company: {company}")
    line(f"Status: {getattr(d,'status','')}")
    line(f"Kind: {getattr(d,'kind','')}")
    line(f"Name: {getattr(d,'name','')}")
    line(f"Phone: {getattr(d,'phone','')}")
    line(f"Experience (months): {getattr(d,'exp_months','')}")
    line(f"Escrow: {getattr(d,'escrow','')}")
    line(f"Ready date: {getattr(d,'ready_date','')}")

    c.showPage()
    c.save()

    return buf.getvalue(), f"driver_{driver_id}.pdf"

# === Compatibility helpers for test.py ===

async def get_company(company_id: int):
    """Return Company by id or None."""
    async with AsyncSessionLocal() as s:
        return await s.get(Company, company_id)


async def delete_user(user_id: int) -> bool:
    """Hard-delete a user. Returns True on success, False on FK constraint failure or not found."""
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        try:
            await s.delete(u)
            await s.commit()
            return True
        except Exception:
            await s.rollback()
            return False


async def set_driver_status(driver_id: int, status: str) -> bool:
    """Update driver.status. Returns True if updated."""
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, driver_id)
        if not d:
            return False
        d.status = status
        await s.commit()
        return True


async def set_driver_group_msg_id(driver_id: int, msg_id: int) -> bool:
    """Store the message anchor id in the driver's record."""
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, driver_id)
        if not d:
            return False
        d.group_msg_id = int(msg_id)
        await s.commit()
        return True


async def find_driver_by_group_msg(chat_id: int | str, replied_message_id: int) -> Driver | None:
    """Find driver by (company_chat_id, group_msg_id). chat_id may be int or str."""
    chat_id_str = str(chat_id)
    async with AsyncSessionLocal() as s:
        res = await s.execute(
            select(Driver).where(
                Driver.company_chat_id == chat_id_str,
                Driver.group_msg_id == int(replied_message_id)
            )
        )
        return res.scalar_one_or_none()

