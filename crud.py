# crud.py
import asyncio
from typing import List, Optional, Tuple
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db import AsyncSessionLocal
from db_models import User, Company, Driver, DriverReply, PdfFile


# ---------- helper ----------
async def _session() -> AsyncSession:
    return AsyncSessionLocal()  # type: ignore


# ---------- USERS ----------
async def get_user_by_username(username: str) -> Optional[User]:
    async with await _session() as s:
        res = await s.execute(select(User).where(User.username == username))
        return res.scalar_one_or_none()


async def get_user_by_telegram_id(telegram_id: str) -> Optional[User]:
    async with await _session() as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        return res.scalar_one_or_none()


async def list_users() -> List[User]:
    async with await _session() as s:
        res = await s.execute(select(User).order_by(User.id))
        return list(res.scalars().all())


async def create_user(username: str, password: str, role: str = "recruiter") -> Tuple[bool, Optional[str]]:
    async with await _session() as s:
        try:
            u = User(username=username, password_hash=password, role=role, is_active=True)
            s.add(u)
            await s.commit()
            return True, None
        except IntegrityError as e:
            await s.rollback()
            return False, "Username already exists"


async def delete_user(user_id: int) -> None:
    async with await _session() as s:
        await s.execute(delete(User).where(User.id == user_id))
        await s.commit()


async def update_user_username(user_id: int, new_username: str) -> Tuple[bool, Optional[str]]:
    async with await _session() as s:
        try:
            await s.execute(update(User).where(User.id == user_id).values(username=new_username))
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "Username already exists"


async def update_user_password(user_id: int, new_password: str) -> None:
    async with await _session() as s:
        await s.execute(update(User).where(User.id == user_id).values(password_hash=new_password))
        await s.commit()


async def toggle_user_active(user_id: int) -> None:
    async with await _session() as s:
        res = await s.execute(select(User).where(User.id == user_id))
        u = res.scalar_one()
        await s.execute(update(User).where(User.id == user_id).values(is_active=not u.is_active))
        await s.commit()


async def set_user_telegram_id(user_id: int, telegram_id: str | None) -> None:
    async with await _session() as s:
        await s.execute(update(User).where(User.id == user_id).values(telegram_id=telegram_id))
        await s.commit()


# ---------- COMPANIES ----------
async def list_companies() -> List[Company]:
    async with await _session() as s:
        res = await s.execute(select(Company).order_by(Company.id))
        return list(res.scalars().all())


async def get_company(company_id: int) -> Optional[Company]:
    async with await _session() as s:
        res = await s.execute(select(Company).where(Company.id == company_id))
        return res.scalar_one_or_none()


async def create_company(name: str, chat_id: str) -> Tuple[bool, Optional[str]]:
    async with await _session() as s:
        try:
            c = Company(name=name, telegram_chat_id=chat_id, is_active=True)
            s.add(c)
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "Company name already exists"


async def rename_company(company_id: int, new_name: str) -> Tuple[bool, Optional[str]]:
    async with await _session() as s:
        try:
            await s.execute(update(Company).where(Company.id == company_id).values(name=new_name))
            await s.commit()
            return True, None
        except IntegrityError:
            await s.rollback()
            return False, "Company name already exists"


async def change_company_chat_id(company_id: int, new_chat_id: str) -> None:
    async with await _session() as s:
        await s.execute(update(Company).where(Company.id == company_id).values(telegram_chat_id=new_chat_id))
        await s.commit()


async def delete_company(company_id: int) -> None:
    async with await _session() as s:
        await s.execute(delete(Company).where(Company.id == company_id))
        await s.commit()


# ---------- DRIVERS ----------
async def create_driver(
    kind: str,
    recruiter_id: int,
    name: str | None,
    phone: str | None,
    exp_months: int | None,
    escrow: str | None,
    ready_date: str | None,
    file_types: str | None,
    file_ids: str | None,
) -> int:
    async with await _session() as s:
        d = Driver(
            kind=kind,
            recruiter_id=recruiter_id,
            name=name,
            phone=phone,
            exp_months=exp_months,
            escrow=escrow,
            ready_date=ready_date,
            file_type=file_types,
            file_id=file_ids,
            status="waiting",
        )
        s.add(d)
        await s.flush()
        new_id = d.id
        await s.commit()
        return new_id


async def set_driver_group_msg_id(driver_id: int, msg_id: int) -> None:
    async with await _session() as s:
        await s.execute(update(Driver).where(Driver.id == driver_id).values(group_msg_id=msg_id))
        await s.commit()


async def update_driver_status(driver_id: int, status: str) -> None:
    async with await _session() as s:
        await s.execute(update(Driver).where(Driver.id == driver_id).values(status=status))
        await s.commit()


async def get_driver(driver_id: int) -> Optional[Driver]:
    async with await _session() as s:
        res = await s.execute(select(Driver).where(Driver.id == driver_id))
        return res.scalar_one_or_none()


async def get_driver_by_group_msg_id(group_msg_id: int) -> Optional[Driver]:
    async with await _session() as s:
        res = await s.execute(select(Driver).where(Driver.group_msg_id == group_msg_id))
        return res.scalar_one_or_none()


async def list_waiting_older_than(days: int) -> List[Driver]:
    # Simple version (no timezone math): reminder logic will check all WAITING
    async with await _session() as s:
        res = await s.execute(select(Driver).where(Driver.status == "waiting"))
        return list(res.scalars().all())


# ---------- REPLIES / PDF ----------
async def save_reply(driver_id: int, from_name: str, text: str) -> None:
    async with await _session() as s:
        r = DriverReply(driver_id=driver_id, from_name=from_name, text=text)
        s.add(r)
        await s.commit()


async def save_pdf(driver_id: int, path: str) -> None:
    async with await _session() as s:
        p = PdfFile(driver_id=driver_id, path=path)
        s.add(p)
        await s.commit()
