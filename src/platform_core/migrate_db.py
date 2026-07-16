"""
One-time migration: adds new columns to existing tables in
caregiver_platform.db without deleting any data already there.

Brand-new tables (applications, employment_history, education,
references_table, documents, notifications) don't need this script -
init_db() creates missing tables automatically. This script only handles
ALTER TABLE for columns added to tables that already existed.

Run this ONCE from the project root (the folder containing caregiver_platform.db):
    python migrate_db.py
"""
import sqlite3

DB_PATH = "caregiver_platform.db"

# table_name: {column_name: SQL type}
NEW_COLUMNS = {
    "users": {
        "phone": "TEXT",
        "address": "TEXT",
        "linkedin_url": "TEXT",
        "professional_registration_body": "TEXT",
        "professional_registration_number": "TEXT",
        "visa_status": "TEXT",
        "right_to_work": "BOOLEAN DEFAULT 0",
        "national_insurance_number": "TEXT",
        "dbs_status": "TEXT",
        "health_declaration_status": "TEXT",
        "indemnity_status": "TEXT",
        "preferred_band": "TEXT",
        "preferred_region": "TEXT",
        "preferred_clinical_interest": "TEXT",
        "automation_enabled": "BOOLEAN DEFAULT 0",
        "applications_auto_sent": "INTEGER DEFAULT 0",
        "password_salt": "TEXT",
        "profile_picture": "TEXT",
        "notify_email_enabled": "BOOLEAN DEFAULT 1",
        "notify_job_alerts": "BOOLEAN DEFAULT 1",
    },
    "jobs": {
        "source_url": "TEXT",
        "description": "TEXT",
    },
}

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

total_added = []
for table, columns in NEW_COLUMNS.items():
    cur.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in cur.fetchall()}
    if not existing_columns:
        # Table doesn't exist yet - init_db() will create it fresh, skip.
        continue
    for col_name, col_type in columns.items():
        if col_name not in existing_columns:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            total_added.append(f"{table}.{col_name}")

conn.commit()
conn.close()

if total_added:
    print(f"Added {len(total_added)} column(s): {', '.join(total_added)}")
else:
    print("No changes needed - database already up to date.")