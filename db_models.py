from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.sql import func
from db import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="recruiter")
    is_active = Column(Boolean, default=True)
    telegram_id = Column(String(64), nullable=True)   # store recruiter's telegram chat id
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    telegram_chat_id = Column(String(64), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Driver(Base):
    __tablename__ = "drivers"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)             # solo / team / owner_op
    recruiter_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    name = Column(String(200), nullable=True)
    phone = Column(String(64), nullable=True)
    exp_months = Column(Integer, nullable=True)
    escrow = Column(String(100), nullable=True)
    ready_date = Column(String(100), nullable=True)
    file_type = Column(String(40), nullable=True)
    file_id = Column(String(255), nullable=True)
    group_msg_id = Column(Integer, nullable=True)         # message id in company group
    status = Column(String(30), default="waiting")        # waiting/hired/rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class DriverReply(Base):
    __tablename__ = "driver_replies"
    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=False)
    from_name = Column(String(200), nullable=False)
    text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PdfFile(Base):
    __tablename__ = "pdf_files"
    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=False)
    path = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
