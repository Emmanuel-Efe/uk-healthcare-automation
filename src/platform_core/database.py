import datetime
import os
from pathlib import Path
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Reads DATABASE_URL from the environment first (so this can point at
# Postgres in production without any code change). If it's not set, falls
# back to an ABSOLUTE path for the local SQLite file next to the project
# root - deliberately not a cwd-relative "./caregiver_platform.db", since
# that silently resolves to a different (empty) file depending on which
# directory the server process happens to be launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{_PROJECT_ROOT / 'caregiver_platform.db'}"

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    password_salt = Column(String, nullable=True)    # per-user salt for hashed_password
    full_name = Column(String, nullable=True)
    profile_picture = Column(String, nullable=True)  # path under /static/uploads/avatars/
    
    # Subscription Tiering Engine
    tier = Column(String, default="demo") # demo, basic, standard, premium
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    tier_expires_at = Column(DateTime, default=lambda: datetime.datetime.utcnow() + datetime.timedelta(hours=24))

    # Core profile details used for automation matching and CV generation
    experience_summary = Column(Text, nullable=True)
    skills_list = Column(Text, nullable=True)

    # Email verification
    is_verified = Column(Boolean, default=False)
    verification_token = Column(String, unique=True, index=True, nullable=True)

    # --- Personal Information ---
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)

    # --- Professional Identity & Right to Work ---
    professional_registration_body = Column(String, nullable=True)   # e.g. NMC, GMC, HCPC
    professional_registration_number = Column(String, nullable=True) # e.g. PIN / GMC number
    visa_status = Column(String, nullable=True)          # e.g. "Skilled Worker Visa", "ILR", "British Citizen"
    right_to_work = Column(Boolean, default=False)
    national_insurance_number = Column(String, nullable=True)

    # --- Declarations ---
    dbs_status = Column(String, nullable=True)            # e.g. "Clear - Enhanced, issued Jan 2026"
    health_declaration_status = Column(String, nullable=True)
    indemnity_status = Column(String, nullable=True)

    # --- Automation / Personalized feed preferences ---
    preferred_band = Column(String, nullable=True)         # e.g. "Band 5", "Band 6"
    preferred_region = Column(String, nullable=True)       # e.g. "London"
    preferred_clinical_interest = Column(String, nullable=True)  # e.g. "Adult Nursing"
    automation_enabled = Column(Boolean, default=False)
    applications_auto_sent = Column(Integer, default=0)

    # --- Notification preferences (Settings page) ---
    notify_email_enabled = Column(Boolean, default=True)
    notify_job_alerts = Column(Boolean, default=True)
    email_alerts = Column(Boolean, default=True)
    interview_reminders = Column(Boolean, default=True)     

    def is_trial_active(self) -> bool:
        if self.tier == "demo" and datetime.datetime.utcnow() > self.tier_expires_at:
            return False
        return True

    @property
    def display_name(self):
        """Used in the nav/header - prefers the real name, falls back to a
        prettified email prefix rather than showing the raw email address."""
        if self.full_name:
            return self.full_name
        prefix = self.email.split("@")[0]
        return prefix.replace(".", " ").replace("_", " ").title()

    @property
    def avatar_initials(self):
        """Used for the default avatar circle in the nav before a profile
        picture is uploaded."""
        name = self.display_name
        parts = [p for p in name.split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return name[:2].upper() if name else "?"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    employer = Column(String, nullable=True)
    location = Column(String, nullable=True)
    band = Column(String, nullable=True)
    salary_min = Column(Float, nullable=True)
    salary_max = Column(Float, nullable=True)
    visa_sponsorship = Column(Boolean, default=False)
    ai_match_score = Column(Float, nullable=True)
    job_ref = Column(String, unique=True, index=True, nullable=False)
    source = Column(String, nullable=True)
    source_url = Column(String, nullable=True)  # link to the real, official listing
    description = Column(Text, nullable=True)    # real snippet/summary from the listing
    closing_date = Column(DateTime, nullable=True)
    ingested_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def salary(self):
        """Convenience string used by templates, e.g. '£23,000 - £26,000'."""
        if self.salary_min and self.salary_max:
            return f"£{int(self.salary_min):,} - £{int(self.salary_max):,}"
        return None

    @property
    def date_added(self):
        """Convenience display date used by dashboard.html."""
        if self.ingested_at:
            return self.ingested_at.strftime("%d %b %Y")
        return ""


def init_db():
    Base.metadata.create_all(bind=engine)


class Application(Base):
    """Powers the Application Tracker on the dashboard."""
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    job_id = Column(Integer, index=True, nullable=False)
    status = Column(String, default="Saved")  # Saved, Applied, Shortlisted, Interviewing, Offer, Rejected
    applied_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)
    interview_date = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)


class EmploymentHistory(Base):
    __tablename__ = "employment_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    institution_name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    job_title = Column(String, nullable=False)
    grade_band = Column(String, nullable=True)
    start_date = Column(String, nullable=True)   # e.g. "Jan 2022"
    end_date = Column(String, nullable=True)      # blank/None = current role
    is_current = Column(Boolean, default=False)


class Education(Base):
    __tablename__ = "education"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    qualification_name = Column(String, nullable=False)
    institution = Column(String, nullable=True)
    qualification_type = Column(String, nullable=True)  # Degree, Certification, Short Course
    date_awarded = Column(String, nullable=True)


class Reference(Base):
    __tablename__ = "references_table"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=True)
    institution = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)


class Document(Base):
    """Powers the Document Vault (CV, cover letters, certificates)."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    doc_type = Column(String, nullable=False)  # CV, Cover Letter, Certificate, Other
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)


class Notification(Base):
    """Powers the Inbox badge + Action Required panel on the dashboard."""
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    message = Column(String, nullable=False)
    notif_type = Column(String, default="info")  # info, action_required, recruiter_message
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    related_job_id = Column(Integer, nullable=True)


def get_db():
    """FastAPI dependency: yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()