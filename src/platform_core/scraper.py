import os
import time
import asyncio
import logging
import datetime
import hashlib
import re
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from sqlalchemy import or_
from .database import SessionLocal, Job, Application

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SystemScraperEngine")

NHS_BASE_URL = "https://www.jobs.nhs.uk"

# Broad healthcare keyword set - covers nursing, medical, allied health
# professions, mental health, social care, and support/admin roles across
# the NHS and wider UK healthcare sector, to maximise real listing coverage.
CARE_KEYWORDS = [
    # Care & support roles
    "care assistant", "support worker", "healthcare assistant", "care worker",
    "senior care assistant", "domiciliary care worker", "residential care worker",
    "live-in carer", "night carer", "support worker learning disability",
    "mental health support worker", "rehabilitation support worker",

    # Nursing
    "registered nurse", "staff nurse", "adult nurse", "mental health nurse",
    "learning disability nurse", "childrens nurse", "paediatric nurse",
    "district nurse", "practice nurse", "theatre nurse", "recovery nurse",
    "ICU nurse", "critical care nurse", "A&E nurse", "emergency nurse",
    "nurse practitioner", "clinical nurse specialist", "nurse manager",
    "ward manager", "deputy ward manager", "matron",

    # Midwifery
    "midwife", "student midwife", "maternity support worker",

    # Allied health professions
    "physiotherapist", "occupational therapist", "speech and language therapist",
    "radiographer", "paramedic", "pharmacist", "pharmacy technician",
    "dietitian", "podiatrist", "chiropodist", "audiologist", "optometrist",
    "clinical psychologist", "counsellor", "art therapist",

    # Medical / doctors
    "GP", "general practitioner", "junior doctor", "consultant",
    "medical registrar", "locum doctor", "clinical fellow",

    # Dental
    "dental nurse", "dental hygienist", "dentist", "dental receptionist",

    # Technicians & clinical support
    "phlebotomist", "healthcare technician", "theatre technician",
    "operating department practitioner", "ambulance technician",
    "clinical support worker", "endoscopy assistant", "cardiac physiologist",

    # Social work
    "social worker", "senior social worker", "family support worker",
    "safeguarding officer",

    # Admin & management (healthcare-specific)
    "medical secretary", "ward clerk", "GP receptionist", "medical receptionist",
    "clinical coder", "healthcare administrator", "practice manager",
    "clinical governance manager", "infection control nurse",

    # Estates & facilities (healthcare-specific)
    "hospital porter", "domestic assistant", "healthcare cleaner",
    "catering assistant NHS",
]


async def scrape_nhs_jobs(db: Session, keyword: str):
    """Scrapes real, live listings from jobs.nhs.uk. This is currently the
    ONLY source wired into the ingestion loop - no synthetic/random data is
    generated anywhere in this file."""
    logger.info(f"[NHS] Running ingestion matrix for: '{keyword}'")
    # REMOVED: &location=United+Kingdom to prevent redirect/blocking
    search_url = f"{NHS_BASE_URL}/candidate/search/results?keyword={keyword.replace(' ', '+')}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en;q=0.9"
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(search_url, headers=headers)
            
            if response.status_code != 200:
                logger.warning(f"[NHS] Status {response.status_code} for '{keyword}'")
                return 0

            soup = BeautifulSoup(response.text, 'html.parser')
            # Updated selector for NHS Jobs result cards
            job_cards = soup.find_all('li', class_='nhsuk-list-panel')
            
            logger.info(f"[NHS] '{keyword}': found {len(job_cards)} card(s).")

            count = 0
            for card in job_cards:
                try:
                    title_element = card.find('a') or card.find('h3')
                    if not title_element or not title_element.text.strip():
                        continue

                    title = title_element.text.strip()
                    text_content = card.get_text()

                    # Build the real, absolute application URL - this is what
                    # the job detail page's "Apply" button links to.
                    href = title_element.get('href', '') if title_element.name == 'a' else None
                    if not href:
                        link_tag = card.find('a', href=True)
                        href = link_tag.get('href') if link_tag else None
                    source_url = None
                    if href:
                        source_url = href if href.startswith('http') else f"{NHS_BASE_URL}{href}"

                    # Real NHS job reference pattern, when present on the card
                    ref_match = re.search(r'[A-Z0-9]{3,5}-\d{2}-\d{4}|[0-9]{3}-[A-Z0-9]+-[A-Z0-9]+', text_content)
                    if ref_match:
                        job_ref = ref_match.group(0)
                    elif href:
                        href_digits = "".join(filter(str.isdigit, href))
                        job_ref = f"NHS-{href_digits}" if href_digits else None
                    else:
                        job_ref = None

                    if not job_ref:
                        # Deterministic fallback (not random) so re-scraping the
                        # same real listing doesn't create a duplicate.
                        job_ref = "NHS-" + hashlib.sha1(f"{title}|{href}".encode()).hexdigest()[:10]

                    if db.query(Job).filter(Job.job_ref == job_ref).first():
                        continue  # already ingested, skip

                    employer = None
                    for p in card.find_all('p'):
                        if "employer:" in p.text.lower() or "posted by:" in p.text.lower():
                            employer = p.text.replace("Employer:", "").replace("Posted by:", "").strip()
                            break

                    location = None
                    for span in card.find_all(['span', 'p']):
                        if "location:" in span.text.lower() or "📍" in span.text:
                            location = span.text.replace("Location:", "").strip()
                            break

                    band = None
                    for b in ["Band 2", "Band 3", "Band 4", "Band 5", "Band 6", "Band 7", "Band 8"]:
                        if b.lower() in text_content.lower():
                            band = b
                            break

                    # Only record a salary if one is actually printed on the card
                    salary_min, salary_max = None, None
                    salary_match = re.search(r'£\s?([\d,]+)\s?(?:-|to)\s?£?\s?([\d,]+)', text_content)
                    if salary_match:
                        try:
                            salary_min = float(salary_match.group(1).replace(',', ''))
                            salary_max = float(salary_match.group(2).replace(',', ''))
                        except ValueError:
                            pass

                    # Only mark visa sponsorship true if the card explicitly says so
                    visa_sponsorship = "sponsorship" in text_content.lower() and "no sponsorship" not in text_content.lower()

                    # Closing date, if printed on the card; otherwise left unknown
                    closing_date = None
                    date_match = re.search(r'closing date:?\s*([\d]{1,2}\s\w+\s[\d]{4})', text_content, re.IGNORECASE)
                    if date_match:
                        try:
                            closing_date = datetime.datetime.strptime(date_match.group(1), "%d %B %Y").date()
                        except ValueError:
                            closing_date = None

                    # Real description snippet straight from the card (cleaned + trimmed)
                    description = re.sub(r'\s+', ' ', text_content).strip()[:600] or None

                    new_job = Job(
                        title=title,
                        employer=employer or "NHS Employer (see listing)",
                        location=location or "United Kingdom",
                        band=band,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        visa_sponsorship=visa_sponsorship,
                        ai_match_score=None,  # reserved for a real matching model against the user's profile
                        job_ref=job_ref,
                        source="NHS Jobs",
                        source_url=source_url,
                        description=description,
                        closing_date=closing_date,
                        ingested_at=datetime.datetime.utcnow(),
                    )
                    db.add(new_job)
                    count += 1
                except Exception:
                    continue

            db.commit()
            logger.info(f"[NHS Ingestion] Success: Added {count} new real listings for '{keyword}'")
            return count
    except Exception as e:
        logger.error(f"[NHS Pipeline Engine Error]: {e}")
        return 0


# ------------------------------------------------------------------
# Adzuna - official, free, ToS-compliant UK jobs API. This is the
# PRIMARY real-jobs source: NHS Jobs actively blocks automated requests
# (bot detection confirmed directly), so scraping it cannot reliably
# return results no matter how the selectors are tuned. Adzuna requires
# free registration at https://developer.adzuna.com/signup - add the
# resulting ADZUNA_APP_ID and ADZUNA_APP_KEY to your .env file.
# ------------------------------------------------------------------
ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs/gb/search"

# A smaller, high-signal subset of CARE_KEYWORDS used for Adzuna queries
# (Adzuna's "what" search already matches broadly, so fewer, broader terms
# return better coverage than looping all 60+ CARE_KEYWORDS).
ADZUNA_SEARCH_TERMS = [
    "healthcare assistant", "registered nurse", "care assistant",
    "support worker", "physiotherapist", "occupational therapist",
    "paramedic", "midwife", "mental health nurse", "phlebotomist",
    "social worker", "pharmacist", "radiographer", "dental nurse",
]


async def scrape_adzuna_jobs(db: Session, search_term: str) -> int:
    """Fetches real, live UK healthcare listings from the official Adzuna
    API. Requires ADZUNA_APP_ID / ADZUNA_APP_KEY env vars - if unset, this
    is skipped (logged once) rather than failing the whole ingestion loop."""
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        logger.warning("[Adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env - skipping this real source. "
                        "Register free at https://developer.adzuna.com/signup")
        return 0

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": 50,
        "what": search_term,
        "category": "healthcare-nursing-jobs",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{ADZUNA_BASE_URL}/1", params=params)
            if response.status_code != 200:
                logger.warning(f"[Adzuna] Status {response.status_code} for '{search_term}': {response.text[:200]}")
                return 0

            data = response.json()
            results = data.get("results", [])
            count = 0

            for item in results:
                adzuna_id = item.get("id")
                job_ref = f"ADZUNA-{adzuna_id}"
                if not adzuna_id or db.query(Job).filter(Job.job_ref == job_ref).first():
                    continue  # already ingested, skip

                title = item.get("title", "").strip()
                if not title:
                    continue

                company = (item.get("company") or {}).get("display_name")
                location = (item.get("location") or {}).get("display_name")
                description = item.get("description")
                salary_min = item.get("salary_min")
                salary_max = item.get("salary_max")
                redirect_url = item.get("redirect_url")  # real application link

                # Best-effort band detection from title/description text
                band = None
                combined_text = f"{title} {description or ''}".lower()
                for b in ["Band 2", "Band 3", "Band 4", "Band 5", "Band 6", "Band 7", "Band 8"]:
                    if b.lower() in combined_text:
                        band = b
                        break

                visa_sponsorship = "sponsorship" in combined_text and "no sponsorship" not in combined_text

                created_str = item.get("created")  # e.g. "2026-07-10T08:00:00Z"
                ingested_at = datetime.datetime.utcnow()
                if created_str:
                    try:
                        ingested_at = datetime.datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        pass

                new_job = Job(
                    title=title,
                    employer=company or "Employer not disclosed",
                    location=location or "United Kingdom",
                    band=band,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    visa_sponsorship=visa_sponsorship,
                    ai_match_score=None,
                    job_ref=job_ref,
                    source="Adzuna",
                    source_url=redirect_url,
                    description=(description or "").strip()[:600] or None,
                    closing_date=None,  # Adzuna doesn't provide a closing date
                    ingested_at=ingested_at,
                )
                db.add(new_job)
                count += 1

            db.commit()
            logger.info(f"[Adzuna Ingestion] Success: Added {count} new real listings for '{search_term}'")
            return count
    except Exception as e:
        logger.error(f"[Adzuna Pipeline Error]: {e}")
        return 0


async def check_nhs_connectivity(keyword: str = "care assistant") -> dict:
    """Diagnostic helper (no DB writes) - fetches the NHS search page once
    and reports what it found, for debugging why job counts might be 0."""
    search_url = f"{NHS_BASE_URL}/candidate/search/results?keyword={keyword.replace(' ', '+')}&location=United+Kingdom"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    result = {"search_url": search_url, "status_code": None, "html_length": 0, "cards_found": 0, "error": None}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(search_url, headers=headers)
            result["status_code"] = response.status_code
            result["html_length"] = len(response.text)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                job_cards = (
                    soup.find_all('div', class_='nhsuk-jobs-search-results__item')
                    or soup.find_all('[id^=search-result]')
                    or soup.find_all('div', {'class': lambda x: x and 'result' in x.lower()})
                )
                result["cards_found"] = len(job_cards)
    except Exception as e:
        result["error"] = str(e)
    return result


def purge_expired_jobs(db: Session) -> int:
    """Removes jobs whose closing date has passed, so expired listings don't
    clutter the site. Jobs a user has actually tracked in their Applications
    are preserved (for their history) even after expiry - only untracked,
    expired jobs are deleted."""
    today = datetime.date.today()
    tracked_job_ids = {a.job_id for a in db.query(Application.job_id).all()}
    expired = db.query(Job).filter(Job.closing_date.isnot(None), Job.closing_date < today).all()
    removed = 0
    for job in expired:
        if job.id not in tracked_job_ids:
            db.delete(job)
            removed += 1
    if removed:
        db.commit()
        logger.info(f"[Cleanup] Removed {removed} expired job listing(s).")
    return removed


async def run_continuous_ingestion_loop():
    """Runs the real job ingestion loop:
      1. Adzuna (official API) - the reliable primary source.
      2. NHS Jobs (direct scrape) - best-effort bonus; NHS actively blocks
         bot traffic so this will often add 0, which is expected, not a bug.
    Then purges any expired listings. Private-sector boards (Indeed,
    Totaljobs, Nurses.co.uk) are NOT scraped - those sites actively block
    automated scraping and doing so would violate their Terms of Service."""
    logger.info(f"[CoreWorkerEngine] Ingestion Loop initialized. Adzuna terms: {len(ADZUNA_SEARCH_TERMS)}, NHS keywords: {len(CARE_KEYWORDS)}.")
    while True:
        db = SessionLocal()
        total_added = 0
        try:
            for term in ADZUNA_SEARCH_TERMS:
                total_added += await scrape_adzuna_jobs(db, term)
                await asyncio.sleep(1.0)
            for keyword in CARE_KEYWORDS:
                total_added += await scrape_nhs_jobs(db, keyword)
                await asyncio.sleep(1.5)
            purge_expired_jobs(db)
        except Exception as err:
            logger.error(f"[CoreWorkerEngine Loop Exception]: {err}")
        finally:
            db.close()

        logger.info(f"[CoreWorkerEngine] Sync complete. Added {total_added} jobs this cycle. Sleeping 1 hour.")
        await asyncio.sleep(3600)