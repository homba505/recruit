# crud.py
from typing import Optional, List, Tuple
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError

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
        res = await s.execute(select(User).where(User.manager_id == manager_id).order_by(User.id))
        return list(res.scalars().all())

async def is_in_team(manager_id: int, user_id: int) -> bool:
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(User.id).where(User.id == user_id, User.manager_id == manager_id))
        return res.scalar_one_or_none() is not None

async def create_user(username: str, password: str, role: str = "recruiter", manager_id: Optional[int] = None) -> Tuple[bool, Optional[str]]:
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

async def update_user_password(user_id: int, new_password: str) -> bool:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return False
        u.password_hash = hash_pw(new_password)
        await s.commit()
        return True

async def delete_user(user_id: int) -> None:
    async with AsyncSessionLocal() as s:
        u = await s.get(User, user_id)
        if not u:
            return
        await s.delete(u)
        await s.commit()

# ========= Companies =========
async def create_company(name: str, chat_id: str) -> Tuple[bool, Optional[str]]:
    async with AsyncSessionLocal() as s:
        c = Company(name=name, telegram_chat_id=chat_id)
        s.add(c)
        try:
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "name_taken"

async def list_companies() -> List[Company]:
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Company).order_by(Company.id))
        return list(res.scalars().all())

async def get_company(company_id: int) -> Optional[Company]:
    async with AsyncSessionLocal() as s:
        return await s.get(Company, company_id)

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

# ========= Drivers & Replies =========
async def create_driver(
    kind: str,
    recruiter_id: int,
    name: Optional[str],
    phone: Optional[str],
    exp_months: Optional[int],
    escrow: Optional[str],
    ready_date: Optional[str],
    file_types: str,
    file_ids: str,
    company_id: Optional[int] = None,
    company_chat_id: Optional[str] = None,
) -> int:
    async with AsyncSessionLocal() as s:
        d = Driver(
            kind=kind,
            recruiter_id=recruiter_id,
            name=name,
            phone=phone,
            exp_months=exp_months,
            escrow=escrow,
            ready_date=ready_date,
            file_types=file_types,
            file_ids=file_ids,
            company_id=company_id,
            company_chat_id=company_chat_id,
            status="new",
        )
        s.add(d)
        await s.commit()
        return d.id

async def set_driver_group_msg_id(driver_id: int, msg_id: int) -> None:
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, driver_id)
        if not d:
            return
        d.group_msg_id = msg_id
        await s.commit()

async def save_pdf(driver_id: int, path: str) -> None:
    async with AsyncSessionLocal() as s:
        p = PdfFile(driver_id=driver_id, path=path)
        s.add(p)
        await s.commit()

async def set_driver_status(driver_id: int, status: str) -> bool:
    async with AsyncSessionLocal() as s:
        d = await s.get(Driver, driver_id)
        if not d:
            return False
        d.status = status
        await s.commit()
        return True

async def find_driver_by_group_msg(chat_id: int, msg_id: int) -> Optional[Driver]:
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

async def create_driver_reply(driver_id: int, from_user: str, text: str, message_id: int) -> None:
    async with AsyncSessionLocal() as s:
        r = DriverReply(driver_id=driver_id, from_user=from_user, text=text, tg_message_id=str(message_id))
        s.add(r)
        await s.commit()
