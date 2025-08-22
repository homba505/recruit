from sqlalchemy import select
from db import AsyncSessionLocal
from db_models import User, Company, Driver, DriverReply, PdfFile

# ---------- Users ----------
async def get_user_by_username(username: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        return result.scalars().first()

async def get_user_by_id(user_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalars().first()

async def create_user(username: str, password_hash: str, role="recruiter"):
    async with AsyncSessionLocal() as session:
        user = User(username=username, password_hash=password_hash, role=role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

async def set_user_telegram_id(user_id: int, telegram_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalars().first()
        if user:
            user.telegram_id = str(telegram_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user

# ---------- Companies ----------
async def get_company_by_name(name: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Company).where(Company.name == name))
        return result.scalars().first()

async def get_company_by_chat_id(chat_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Company).where(Company.telegram_chat_id == str(chat_id)))
        return result.scalars().first()

async def create_company(name: str, chat_id: str):
    async with AsyncSessionLocal() as session:
        company = Company(name=name, telegram_chat_id=str(chat_id))
        session.add(company)
        await session.commit()
        await session.refresh(company)
        return company

# ---------- Drivers ----------
async def create_driver(data: dict):
    async with AsyncSessionLocal() as session:
        driver = Driver(**data)
        session.add(driver)
        await session.commit()
        await session.refresh(driver)
        return driver

async def get_driver(driver_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Driver).where(Driver.id == driver_id))
        return result.scalars().first()

async def update_driver_group_msg(driver_id: int, group_msg_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Driver).where(Driver.id == driver_id))
        driver = result.scalars().first()
        if driver:
            driver.group_msg_id = int(group_msg_id)
            session.add(driver)
            await session.commit()
            await session.refresh(driver)
        return driver

async def get_driver_by_group_msg_and_company(group_msg_id: int, company_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Driver).where(Driver.group_msg_id == int(group_msg_id), Driver.company_id == int(company_id)))
        return result.scalars().first()

async def get_most_recent_driver_for_company(company_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Driver).where(Driver.company_id == int(company_id)).order_by(Driver.created_at.desc()).limit(1))
        return result.scalars().first()

# ---------- Replies ----------
async def create_reply(driver_id: int, from_name: str, text: str):
    async with AsyncSessionLocal() as session:
        reply = DriverReply(driver_id=driver_id, from_name=from_name, text=text)
        session.add(reply)
        await session.commit()
        await session.refresh(reply)
        return reply

# ---------- PDFs ----------
async def save_pdf(driver_id: int, path: str):
    async with AsyncSessionLocal() as session:
        pdf = PdfFile(driver_id=driver_id, path=path)
        session.add(pdf)
        await session.commit()
        await session.refresh(pdf)
        return pdf
