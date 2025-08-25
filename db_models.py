# db_models.py
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, BigInteger, Text
)
from sqlalchemy.orm import relationship

from db import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="recruiter")  # recruiter | hr_manager | admin
    is_active = Column(Boolean, nullable=False, default=True)
    telegram_id = Column(String(64), nullable=True)

    # NEW: HR ownership (recruiter belongs to an HR manager)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    manager = relationship("User", remote_side="User.id", backref="team_members")

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    telegram_chat_id = Column(String(64), nullable=True)  # e.g. "-1001234567890"
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class Driver(Base):
    __tablename__ = "drivers"

    id = Column(Integer, primary_key=True)
    recruiter_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    recruiter = relationship("User")

    # NEW: track which company this driver was sent to
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company = relationship("Company")
    company_chat_id = Column(String(64), nullable=True)  # exact chat id used
    group_msg_id = Column(BigInteger, nullable=True)     # message id in that chat

    kind = Column(String(32), nullable=True)             # solo | team | owner_op
    name = Column(String(128), nullable=True)
    phone = Column(String(64), nullable=True)
    exp_months = Column(Integer, nullable=True)
    escrow = Column(String(64), nullable=True)
    ready_date = Column(String(64), nullable=True)

    file_types = Column(String(64), nullable=True)       # "photo,photo" etc
    file_ids = Column(Text, nullable=True)               # "fid1|fid2"

    # NEW: status
    status = Column(String(20), nullable=False, default="new")  # new|waiting|approved|rejected

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class DriverReply(Base):
    __tablename__ = "driver_replies"

    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=False)
    driver = relationship("Driver")
    from_user = Column(String(128), nullable=True)        # who replied in company chat
    text = Column(Text, nullable=True)
    tg_message_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class PdfFile(Base):
    __tablename__ = "pdf_files"

    id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=False)
    driver = relationship("Driver")
    path = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
