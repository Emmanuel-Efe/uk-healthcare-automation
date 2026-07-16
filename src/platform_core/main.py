import os
import re
import json
import asyncio
import datetime
import calendar
import secrets
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from platform_core.database import (
    init_db, SessionLocal, User, Job, Application, EmploymentHistory,
    Education, Reference, Document, Notification, get_db,
)
from platform_core.scraper import run_continuous_ingestion_loop, purge_expired_jobs
from platform_core.auth_utils import hash_password, verify_password
from dateutil.relativedelta import relativedelta


# Project layout is:
#   uk-healthcare-automation/
#     .env                         <- this holds SESSION_SECRET_KEY, ADZUNA keys, etc.
#     src/platform_core/main.py    <- this file
#     templates/                   <- sibling of src/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# CRITICAL: without this, os.getenv() below never sees the values in your
# .env file (SESSION_SECRET_KEY, ADZUNA_APP_ID/KEY, ANTHROPIC_API_KEY, etc.).
load_dotenv(BASE_DIR / ".env")

# Initialize database mapping automatically on startup
init_db()

app = FastAPI(title="UK Caregiver Automation Platform")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY", "YOUR_LOCAL_PLATFORM_SECRET_KEY"))

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.cache = None

UPLOAD_DIR = BASE_DIR / "uploads" / "documents"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

AVATAR_DIR = BASE_DIR / "static" / "uploads" / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def start_background_scraper():
    """Launches the ingestion loop in the background so job data starts
    populating as soon as the server boots, instead of never running."""
    task = asyncio.create_task(run_continuous_ingestion_loop())

    def _log_if_failed(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            import traceback
            print("[FATAL] Background scraper task crashed:")
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    task.add_done_callback(_log_if_failed)


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request, msg: str = None, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "msg": msg, "error": error})


@app.get("/login", response_class=HTMLResponse)
async def login_page_alias(request: Request, msg: str = None, error: str = None):
    return templates.TemplateResponse("login.html", {"request": request, "msg": msg, "error": error})


@app.post("/login")
async def handle_login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    """Real credential check. No account is created here - only /register
    creates accounts. This closes the bug where typing any email/password
    combo granted access without registering."""
    user = db.query(User).filter(User.email == email.strip().lower()).first()

    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "No account found with that email. Please register first.",
        })

    if not user.hashed_password or not user.password_salt:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "This account has no password set. Please contact support or register a new account.",
        })

    if not verify_password(password, user.hashed_password, user.password_salt):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Incorrect password. Please try again.",
        })

    request.session["user_email"] = user.email
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, error: str = None):
    return templates.TemplateResponse("register.html", {"request": request, "error": error})


@app.post("/register")
async def handle_register(
    request: Request, db: Session = Depends(get_db),
    full_name: str = Form(...), email: str = Form(...),
    password: str = Form(...), confirm_password: str = Form(...),
):
    email_clean = email.strip().lower()

    if password != confirm_password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords do not match."})
    if len(password) < 8:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Password must be at least 8 characters."})

    existing = db.query(User).filter(User.email == email_clean).first()
    if existing:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "An account with this email already exists. Please log in instead.",
        })

    hashed, salt = hash_password(password)
    verification_token = secrets.token_urlsafe(32)

    user = User(
        email=email_clean, full_name=full_name.strip(),
        hashed_password=hashed, password_salt=salt,
        is_verified=False, verification_token=verification_token,
    )
    db.add(user)
    db.commit()

    # Best-effort verification email - doesn't block registration if mail
    # isn't configured yet (mail_utils.py / SMTP settings).
    try:
        from platform_core.mail_utils import send_verification_email
        send_verification_email(user.email, verification_token)
    except Exception as e:
        print(f"[Registration] Verification email not sent ({e}). "
              f"Manual verification link: /verify/{verification_token}")

    return RedirectResponse(url="/login?msg=Registration+successful!+Please+log+in.", status_code=303)


@app.get("/verify/{token}")
async def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.verification_token == token).first()
    if not user:
        return RedirectResponse(url="/login?error=Invalid+or+expired+verification+link.", status_code=303)
    user.is_verified = True
    user.verification_token = None
    db.commit()
    return RedirectResponse(url="/login?msg=Email+verified!+You+can+now+log+in.", status_code=303)


def compute_match_score(job: Job, user: User) -> int | None:
    """Computes a real match percentage between a job and the user's stated
    preferences (band, region, clinical interest). Replaces the old
    ai_match_score placeholder, which was always None/random and showed as
    'None% match'. Returns None if the user hasn't set any preferences yet,
    so the badge can be hidden entirely rather than showing a meaningless
    number."""
    criteria = [user.preferred_band, user.preferred_region, user.preferred_clinical_interest]
    if not any(criteria):
        return None

    total_criteria = 0
    matched = 0
    if user.preferred_band:
        total_criteria += 1
        if job.band and user.preferred_band.lower() in job.band.lower():
            matched += 1
    if user.preferred_region:
        total_criteria += 1
        if job.location and user.preferred_region.lower() in job.location.lower():
            matched += 1
    if user.preferred_clinical_interest:
        total_criteria += 1
        haystack = f"{job.title} {job.description or ''}".lower()
        if user.preferred_clinical_interest.lower() in haystack:
            matched += 1

    if total_criteria == 0:
        return None
    # Floor at 55% for any partial match so a single hit doesn't look weak,
    # scale up to 100% as more criteria line up.
    return round(55 + (matched / total_criteria) * 45)


def get_current_user(request: Request, db: Session) -> User | None:
    """Looks up the logged-in user by the email stored in session. Does
    NOT auto-create an account - a valid session must correspond to a real
    registered user. If the session references a user that no longer
    exists, the session is cleared rather than silently creating a new
    account."""
    user_email = request.session.get("user_email")
    if not user_email:
        return None
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        request.session.clear()
        return None
    return user


def verify_admin_key(key: str = Query(None)):
    """Guards the /admin/* routes. Requires an ADMIN_API_KEY to be set in
    the environment and passed back as ?key=... on the request. If
    ADMIN_API_KEY isn't configured at all, these routes are locked out
    entirely (fails closed rather than open) rather than left reachable
    by anyone who finds the URL."""
    expected = os.getenv("ADMIN_API_KEY")
    if not expected or not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=404)
    return True


@app.get("/admin/scrape-now")
async def trigger_scrape_now(db: Session = Depends(get_db), _admin: bool = Depends(verify_admin_key)):
    """Manual trigger to populate jobs immediately (useful before a demo/
    presentation instead of waiting on the hourly background loop).
    Adzuna (official API) is the reliable source; NHS is best-effort."""
    from platform_core.scraper import scrape_nhs_jobs, scrape_adzuna_jobs, CARE_KEYWORDS, ADZUNA_SEARCH_TERMS
    added_before = db.query(Job).count()
    adzuna_added = 0
    for term in ADZUNA_SEARCH_TERMS:
        adzuna_added += await scrape_adzuna_jobs(db, term)
    nhs_added = 0
    for keyword in CARE_KEYWORDS:
        nhs_added += await scrape_nhs_jobs(db, keyword)
    added_after = db.query(Job).count()
    return {
        "status": "ok",
        "jobs_added": added_after - added_before,
        "adzuna_added": adzuna_added,
        "nhs_added": nhs_added,
        "total_jobs": added_after,
        "note": "If adzuna_added is 0 and nhs_added is 0, check ADZUNA_APP_ID/ADZUNA_APP_KEY are set in .env - NHS Jobs blocks bots by design so 0 there is expected.",
    }


@app.get("/admin/scraper-status")
async def scraper_status(keyword: str = "care assistant", _admin: bool = Depends(verify_admin_key)):
    """Diagnostic route - fetches the NHS search page once (no DB writes)
    and reports status code / HTML size / cards found. Use this to check
    why job counts might be stuck at 0: a non-200 status usually means
    NHS is blocking the request; 200 with 0 cards usually means their page
    structure changed and the CSS selectors need updating."""
    from platform_core.scraper import check_nhs_connectivity
    result = await check_nhs_connectivity(keyword)
    return result


@app.get("/admin/purge-expired-jobs")
async def purge_expired_jobs_route(db: Session = Depends(get_db), _admin: bool = Depends(verify_admin_key)):
    """Removes job listings whose closing date has passed (keeps ones a
    user has tracked in Applications, for their history)."""
    removed = purge_expired_jobs(db)
    return {"status": "ok", "expired_jobs_removed": removed}


@app.get("/admin/purge-fake-jobs")
async def purge_fake_jobs(db: Session = Depends(get_db), _admin: bool = Depends(verify_admin_key)):
    """One-time cleanup: removes jobs previously seeded by the old fake
    generator (source was 'Indeed UK', 'Totaljobs', or 'Nurses.co.uk'),
    plus any applications that reference them. Run this once, then it's a
    no-op on future calls since nothing fake gets added anymore."""
    fake_sources = ["Indeed UK", "Totaljobs", "Nurses.co.uk"]
    fake_jobs = db.query(Job).filter(Job.source.in_(fake_sources)).all()
    fake_job_ids = [j.id for j in fake_jobs]
    apps_removed = 0
    if fake_job_ids:
        apps_removed = db.query(Application).filter(Application.job_id.in_(fake_job_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.id.in_(fake_job_ids)).delete(synchronize_session=False)
        db.commit()
    return {"status": "ok", "fake_jobs_removed": len(fake_job_ids), "applications_removed": apps_removed}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    today = datetime.datetime.utcnow().date()
    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)

    total_discovered = db.query(Job).count()
    jobs_today = db.query(Job).filter(func.date(Job.ingested_at) == today).count()
    jobs_week = db.query(Job).filter(Job.ingested_at >= week_ago).count()
    sources_count = db.query(Job.source).distinct().count()

    metrics = {
        "total_discovered": total_discovered,
        "jobs_today": jobs_today,
        "jobs_week": jobs_week,
        "sources_count": sources_count,
    }

    latest_jobs = db.query(Job).filter(or_(Job.closing_date.is_(None), Job.closing_date >= today)).order_by(Job.ingested_at.desc()).limit(6).all()

    # --- Application Tracker: group applications by status for the Kanban view ---
    applications = db.query(Application).filter(Application.user_id == user.id).all()
    app_job_ids = [a.job_id for a in applications]
    jobs_by_id = {j.id: j for j in db.query(Job).filter(Job.id.in_(app_job_ids)).all()} if app_job_ids else {}

    tracker_columns = ["Saved", "Applied", "Shortlisted", "Interviewing", "Offer", "Rejected"]
    tracker = {status: [] for status in tracker_columns}
    for a in applications:
        job = jobs_by_id.get(a.job_id)
        if job and a.status in tracker:
            tracker[a.status].append({"application": a, "job": job})

    # --- Action Required: upcoming interviews + missing core documents ---
    action_items = []
    upcoming_interviews = [
        a for a in applications
        if a.status == "Interviewing" and a.interview_date and a.interview_date >= datetime.datetime.utcnow()
    ]
    for a in upcoming_interviews:
        job = jobs_by_id.get(a.job_id)
        if job:
            action_items.append({
                "label": f"Interview with {job.employer} on {a.interview_date.strftime('%d %b %Y')}",
                "type": "interview",
            })

    has_cv = db.query(Document).filter(Document.user_id == user.id, Document.doc_type == "CV").first()
    if not has_cv:
        action_items.append({"label": "Upload your CV to enable one-click applications", "type": "document"})
    if not user.professional_registration_number:
        action_items.append({"label": "Add your professional registration number in Profile", "type": "profile"})

    # --- Inbox / notifications ---
    unread_count = db.query(Notification).filter(Notification.user_id == user.id, Notification.is_read == False).count()  # noqa: E712
    recent_notifications = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(5).all()

    # --- Personalized feed: match preferred band / region / clinical interest ---
    feed_query = db.query(Job).filter(or_(Job.closing_date.is_(None), Job.closing_date >= today))
    if user.preferred_band:
        feed_query = feed_query.filter(Job.band.ilike(f"%{user.preferred_band}%"))
    if user.preferred_region:
        feed_query = feed_query.filter(Job.location.ilike(f"%{user.preferred_region}%"))
    if user.preferred_clinical_interest:
        feed_query = feed_query.filter(Job.title.ilike(f"%{user.preferred_clinical_interest}%"))
    candidate_jobs = feed_query.order_by(Job.ingested_at.desc()).limit(30).all()
    if not candidate_jobs:
        # No preferences set yet, or no exact matches - fall back to recent jobs generally
        candidate_jobs = db.query(Job).filter(or_(Job.closing_date.is_(None), Job.closing_date >= today)).order_by(Job.ingested_at.desc()).limit(10).all()

    personalized_feed = sorted(
        ({"job": j, "match": compute_match_score(j, user)} for j in candidate_jobs),
        key=lambda x: (x["match"] or 0), reverse=True,
    )[:5]

    # --- Document vault ---
    documents = db.query(Document).filter(Document.user_id == user.id).order_by(Document.uploaded_at.desc()).all()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "username": user.display_name,
        "user": user,
        "tab": "overview",
        "metrics": metrics,
        "jobs": latest_jobs,
        "tracker": tracker,
        "tracker_columns": tracker_columns,
        "action_items": action_items,
        "unread_count": unread_count,
        "notifications": recent_notifications,
        "personalized_feed": personalized_feed,
        "documents": documents,
    })


@app.get("/jobs", response_class=HTMLResponse)
@app.get("/dashboard/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    location: str = "",
    min_salary: str = Query(""),  # accepted as str - empty form fields send "" which crashes float parsing
    max_salary: str = Query(""),
    visa: str | None = None,
    remote: str | None = None,
    closing_soon: str | None = None,
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    # Only convert to float if the field was actually filled in - leaving
    # one side blank (e.g. just a min) is valid and should still filter.
    min_salary_val = float(min_salary) if min_salary.strip() else None
    max_salary_val = float(max_salary) if max_salary.strip() else None

    query = db.query(Job)
    today = datetime.date.today()
    query = query.filter(or_(Job.closing_date.is_(None), Job.closing_date >= today))
    
    if q:
        query = query.filter(Job.title.ilike(f"%{q}%"))
    if location:
        query = query.filter(Job.location.ilike(f"%{location}%"))
    
    if min_salary_val is not None:
        query = query.filter(Job.salary_min >= min_salary_val)
    if max_salary_val is not None:
        query = query.filter(Job.salary_max <= max_salary_val)
        
    if visa:
        query = query.filter(Job.visa_sponsorship == True)
    if closing_soon:
        soon = datetime.date.today() + datetime.timedelta(days=7)
        query = query.filter(Job.closing_date <= soon)

    jobs = query.order_by(Job.ingested_at.desc()).all()
    tracked_job_ids = {a.job_id for a in db.query(Application).filter(Application.user_id == user.id).all()}

    return templates.TemplateResponse("tab_jobs.html", {
        "request": request,
        "username": user.display_name,
        "tab": "jobs",
        "user": user,
        "jobs": jobs,
        "tracked_job_ids": tracked_job_ids,
    })


@app.get("/job/{job_id}", response_class=HTMLResponse)
@app.get("/dashboard/job/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    application = db.query(Application).filter(Application.user_id == user.id, Application.job_id == job.id).first()

    return templates.TemplateResponse("job_detail.html", {
        "request": request,
        "username": user.display_name,
        "tab": "jobs",
        "user": user,
        "job": job,
        "application": application,
    })


@app.post("/jobs/{job_id}/track")
async def track_job(job_id: int, request: Request, status: str = Form("Saved"), db: Session = Depends(get_db)):
    """Creates or updates an Application record - used by Save/Apply buttons on the Jobs page."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    existing = db.query(Application).filter(Application.user_id == user.id, Application.job_id == job_id).first()
    if existing:
        existing.status = status
        existing.updated_at = datetime.datetime.utcnow()
    else:
        db.add(Application(user_id=user.id, job_id=job_id, status=status))
    db.commit()

    referer = request.headers.get("referer", "/jobs")
    return RedirectResponse(url=referer, status_code=303)


@app.post("/applications/{application_id}/status")
async def update_application_status(application_id: int, request: Request, status: str = Form(...), db: Session = Depends(get_db)):
    """Moves an application between tracker columns (Saved -> Applied -> Shortlisted -> Interviewing -> Offer/Rejected)."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    application = db.query(Application).filter(Application.id == application_id, Application.user_id == user.id).first()
    if application:
        application.status = status
        application.updated_at = datetime.datetime.utcnow()
        db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/settings/password")
async def change_password(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user: return RedirectResponse(url="/")
    
    if new_password != confirm_password:
        return RedirectResponse(url="/settings?error=Passwords+do+not+match", status_code=303)
    
    if len(new_password) < 8:
        return RedirectResponse(url="/settings?error=Password+too+short", status_code=303)
        
    hashed, salt = hash_password(new_password)
    user.hashed_password = hashed
    user.password_salt = salt
    db.commit()
    return RedirectResponse(url="/settings?msg=Password+updated", status_code=303)

@app.post("/settings/notifications")
async def update_notifications(
    request: Request,
    email_alerts: str | None = Form(None),
    interview_reminders: str | None = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user: return RedirectResponse(url="/")
    
    user.email_alerts = bool(email_alerts)
    user.interview_reminders = bool(interview_reminders)
    db.commit()
    return RedirectResponse(url="/settings?msg=Preferences+saved", status_code=303)

@app.post("/settings/delete-account")
async def delete_account(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user: return RedirectResponse(url="/")
    
    # Optional: Delete associated applications/documents here before the user
    db.delete(user)
    db.commit()
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

@app.post("/automation/settings")
async def update_automation_settings(
    request: Request,
    db: Session = Depends(get_db),
    automation_enabled: str | None = Form(None),
    preferred_band: str = Form(""),
    preferred_region: str = Form(""),
    preferred_clinical_interest: str = Form(""),
):
    """Updates the 'Job Search Assistant' toggle and search criteria."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    user.automation_enabled = bool(automation_enabled)
    user.preferred_band = preferred_band or None
    user.preferred_region = preferred_region or None
    user.preferred_clinical_interest = preferred_clinical_interest or None
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/documents/upload")
async def upload_document(
    request: Request,
    db: Session = Depends(get_db),
    doc_type: str = Form(...),
    file: UploadFile = File(...),
):
    """Handles Document Vault uploads (CV, Cover Letter, Certificates)."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    user_dir = UPLOAD_DIR / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}"
    dest_path = user_dir / safe_name

    with open(dest_path, "wb") as f:
        f.write(await file.read())

    db.add(Document(user_id=user.id, doc_type=doc_type, file_name=file.filename, file_path=str(dest_path)))
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/documents/{document_id}/download")
async def download_document(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    document = db.query(Document).filter(Document.id == document_id, Document.user_id == user.id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return FileResponse(document.file_path, filename=document.file_name)

# Add this route to delete a document
@app.post("/documents/{document_id}/delete")
async def delete_document(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user: return RedirectResponse(url="/")
    
    doc = db.query(Document).filter(Document.id == document_id, Document.user_id == user.id).first()
    if doc:
        if os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        db.delete(doc)
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)

# Add this route to replace a document
@app.post("/documents/{document_id}/replace")
async def replace_document(
    document_id: int, 
    request: Request, 
    db: Session = Depends(get_db),
    file: UploadFile = File(...)
):
    user = get_current_user(request, db)
    if not user: return RedirectResponse(url="/")
    
    doc = db.query(Document).filter(Document.id == document_id, Document.user_id == user.id).first()
    if doc:
        # Remove old file
        if os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        
        # Save new file
        user_dir = UPLOAD_DIR / str(user.id)
        safe_name = f"{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}"
        dest_path = user_dir / safe_name
        with open(dest_path, "wb") as f:
            f.write(await file.read())
            
        doc.file_name = file.filename
        doc.file_path = str(dest_path)
        doc.uploaded_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)
    
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: Session = Depends(get_db), msg: str = None, error: str = None):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    employment_history = db.query(EmploymentHistory).filter(EmploymentHistory.user_id == user.id).order_by(EmploymentHistory.id.desc()).all()
    education = db.query(Education).filter(Education.user_id == user.id).order_by(Education.id.desc()).all()
    references = db.query(Reference).filter(Reference.user_id == user.id).all()
    skills = [s.strip() for s in (user.skills_list or "").split(",") if s.strip()]

    return templates.TemplateResponse("profile.html", {
        "request": request,
        "username": user.display_name,
        "tab": "profile",
        "user": user,
        "employment_history": employment_history,
        "education": education,
        "references": references,
        "skills": skills,
        "msg": msg,
        "error": error,
    })


@app.post("/profile/personal")
async def update_personal_info(
    request: Request, db: Session = Depends(get_db),
    full_name: str = Form(""), phone: str = Form(""), address: str = Form(""), linkedin_url: str = Form(""),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    user.full_name = full_name or None
    user.phone = phone or None
    user.address = address or None
    user.linkedin_url = linkedin_url or None
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/bio")
async def update_bio(request: Request, db: Session = Depends(get_db), experience_summary: str = Form("")):
    """Professional bio/summary - shown at the top of the auto-generated CV."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    user.experience_summary = experience_summary or None
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/professional")
async def update_professional_info(
    request: Request, db: Session = Depends(get_db),
    professional_registration_body: str = Form(""), professional_registration_number: str = Form(""),
    visa_status: str = Form(""), right_to_work: str | None = Form(None),
    national_insurance_number: str = Form(""),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    user.professional_registration_body = professional_registration_body or None
    user.professional_registration_number = professional_registration_number or None
    user.visa_status = visa_status or None
    user.right_to_work = bool(right_to_work)
    user.national_insurance_number = national_insurance_number or None
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/declarations")
async def update_declarations(
    request: Request, db: Session = Depends(get_db),
    dbs_status: str = Form(""), health_declaration_status: str = Form(""), indemnity_status: str = Form(""),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    user.dbs_status = dbs_status or None
    user.health_declaration_status = health_declaration_status or None
    user.indemnity_status = indemnity_status or None
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/skills")
async def update_skills(request: Request, db: Session = Depends(get_db), skills_list: str = Form("")):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    user.skills_list = skills_list
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/employment/add")
async def add_employment(
    request: Request, db: Session = Depends(get_db),
    institution_name: str = Form(...), location: str = Form(""), job_title: str = Form(...),
    grade_band: str = Form(""), start_date: str = Form(""), end_date: str = Form(""),
    is_current: str | None = Form(None),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    db.add(EmploymentHistory(
        user_id=user.id, institution_name=institution_name, location=location, job_title=job_title,
        grade_band=grade_band, start_date=start_date, end_date=end_date, is_current=bool(is_current),
    ))
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/education/add")
async def add_education(
    request: Request, db: Session = Depends(get_db),
    qualification_name: str = Form(...), institution: str = Form(""), qualification_type: str = Form(""),
    date_awarded: str = Form(""),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    db.add(Education(
        user_id=user.id, qualification_name=qualification_name, institution=institution,
        qualification_type=qualification_type, date_awarded=date_awarded,
    ))
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/reference/add")
async def add_reference(
    request: Request, db: Session = Depends(get_db),
    name: str = Form(...), role: str = Form(""), institution: str = Form(""),
    email: str = Form(""), phone: str = Form(""),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    db.add(Reference(user_id=user.id, name=name, role=role, institution=institution, email=email, phone=phone))
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/profile/avatar/upload")
async def upload_avatar(request: Request, db: Session = Depends(get_db), avatar: UploadFile = File(...)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    ext = Path(avatar.filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return RedirectResponse(url="/profile?error=Please+upload+a+JPG%2C+PNG%2C+WEBP+or+GIF+image.", status_code=303)

    filename = f"user_{user.id}{ext}"
    file_path = AVATAR_DIR / filename
    contents = await avatar.read()
    if len(contents) > 5 * 1024 * 1024:
        return RedirectResponse(url="/profile?error=Image+must+be+under+5MB.", status_code=303)
    with open(file_path, "wb") as f:
        f.write(contents)

    user.profile_picture = f"/static/uploads/avatars/{filename}"
    db.commit()
    return RedirectResponse(url="/profile", status_code=303)


@app.post("/cv/generate")
async def generate_cv(request: Request, db: Session = Depends(get_db)):
    """Builds a professional CV document from the user's Profile data
    (personal info, employment history, education, skills, references)
    and saves it straight into the Document Vault."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    from platform_core.cv_builder import build_cv_docx

    employment = db.query(EmploymentHistory).filter(EmploymentHistory.user_id == user.id).all()
    education = db.query(Education).filter(Education.user_id == user.id).all()
    references = db.query(Reference).filter(Reference.user_id == user.id).all()

    file_name = f"{(user.full_name or 'CV').replace(' ', '_')}_CV.docx"
    file_path = UPLOAD_DIR / f"user_{user.id}_{file_name}"

    build_cv_docx(user, employment, education, references, str(file_path))

    # Replace any previous auto-generated CV rather than piling up duplicates
    existing_cv = db.query(Document).filter(Document.user_id == user.id, Document.doc_type == "CV", Document.file_name == file_name).first()
    if not existing_cv:
        db.add(Document(user_id=user.id, doc_type="CV", file_name=file_name, file_path=str(file_path)))
        db.commit()

    return RedirectResponse(url="/profile?msg=CV+generated!+Check+your+Document+Vault+on+the+Dashboard.", status_code=303)


@app.get("/logout")
async def handle_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


# ------------------------------------------------------------------
# Applications - full detail view, distinct from the dashboard's
# compact Kanban summary. Shows every tracked job with notes,
# interview scheduling, and a link back to the job's own page.
# ------------------------------------------------------------------
@app.get("/applications", response_class=HTMLResponse)
async def applications_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    applications = db.query(Application).filter(Application.user_id == user.id).order_by(Application.updated_at.desc()).all()
    job_ids = [a.job_id for a in applications]
    jobs_by_id = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}

    tracker_columns = ["Saved", "Applied", "Shortlisted", "Interviewing", "Offer", "Rejected"]
    rows = [{"application": a, "job": jobs_by_id.get(a.job_id)} for a in applications if jobs_by_id.get(a.job_id)]

    return templates.TemplateResponse("applications.html", {
        "request": request,
        "username": user.display_name,
        "tab": "applications",
        "user": user,
        "rows": rows,
        "tracker_columns": tracker_columns,
    })


@app.post("/applications/{application_id}/details")
async def update_application_details(
    application_id: int, request: Request, db: Session = Depends(get_db),
    notes: str = Form(""), interview_date: str = Form(""),
):
    """Updates notes and/or interview date/time for a tracked application."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    application = db.query(Application).filter(Application.id == application_id, Application.user_id == user.id).first()
    if application:
        application.notes = notes or None
        if interview_date:
            try:
                application.interview_date = datetime.datetime.fromisoformat(interview_date)
            except ValueError:
                pass
        application.updated_at = datetime.datetime.utcnow()
        db.commit()

    return RedirectResponse(url="/applications", status_code=303)


# ------------------------------------------------------------------
# Settings - account details + the Job Search Assistant automation
# controls (moved here from the dashboard, which now just links over).
# ------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db), msg: str = None, error: str = None):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "username": user.display_name,
        "tab": "settings",
        "user": user,
        "msg": msg,
        "error": error,
    })


# ------------------------------------------------------------------
# Calendar - upcoming interviews and closing dates pulled from real
# tracked applications, sorted chronologically.
# ------------------------------------------------------------------
@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, date_str: str = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")
    # Determine date (default to current month)
    target_date = (datetime.datetime.strptime(date_str, "%Y-%m-%d")
    if date_str
    else datetime.datetime.utcnow())
        
    # Calculate Nav links
    prev_month = target_date - relativedelta(months=1)
    next_month = target_date + relativedelta(months=1)

    applications = db.query(Application).filter(
        Application.user_id == user.id, Application.interview_date.isnot(None)
    ).order_by(Application.interview_date.asc()).all()
    job_ids = [a.job_id for a in applications]
    jobs_by_id = {j.id: j for j in db.query(Job).filter(Job.id.in_(job_ids)).all()} if job_ids else {}

    now = datetime.datetime.utcnow()
    events = [{"application": a, "job": jobs_by_id.get(a.job_id), "is_past": a.interview_date < now}
              for a in applications if jobs_by_id.get(a.job_id)]

    # Generate Month Grid
    cal = calendar.Calendar(firstweekday=6)
    month_days = cal.monthdatescalendar(target_date.year, target_date.month)

    # JSON-friendly version for the JS month-grid calendar (date -> list of labels)
    calendar_events = {}
    for e in events:
        date_key = e["application"].interview_date.strftime("%Y-%m-%d")
        calendar_events.setdefault(date_key, []).append({
            "label": f"Interview: {e['job'].title}",
            "time": e["application"].interview_date.strftime("%H:%M"),
            "job_id": e["job"].id,
        })

    return templates.TemplateResponse("calendar.html", {
        "request": request,
        "username": user.display_name,
        "tab": "calendar",
        "user": user,
        "events": events, # This is the list of dicts used by the UI
        "calendar_events_json": json.dumps(calendar_events),
        "today_str": target_date.strftime("%Y-%m-%d"),
        "month_days": month_days, # This was missing
        "current_date": target_date,
        "prev_link": (target_date - relativedelta(months=1)).strftime("%Y-%m-%d"),
        "next_link": (target_date + relativedelta(months=1)).strftime("%Y-%m-%d"),
    })


# ------------------------------------------------------------------
# Inbox - full notification list (dashboard only shows the latest 5).
# ------------------------------------------------------------------
@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    notifications = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at.desc()).all()

    return templates.TemplateResponse("inbox.html", {
        "request": request,
        "username": user.display_name,
        "tab": "inbox",
        "user": user,
        "notifications": notifications,
    })


@app.post("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/")

    notification = db.query(Notification).filter(Notification.id == notification_id, Notification.user_id == user.id).first()
    if notification:
        notification.is_read = True
        db.commit()

    referer = request.headers.get("referer", "/inbox")
    return RedirectResponse(url=referer, status_code=303)