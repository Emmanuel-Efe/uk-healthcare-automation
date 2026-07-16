import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

# The public URL of the deployed site, e.g. https://your-app.onrender.com
# Falls back to localhost for local development.
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def send_verification_email(target_email: str, token: str):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        # Not configured - skip silently rather than attempting a login
        # that's guaranteed to fail (and slow down registration while it
        # times out). main.py already falls back to logging a manual
        # verification link in this case.
        print("[mail_utils] SMTP_USERNAME/SMTP_PASSWORD not set - skipping verification email.")
        return False

    # Matches the actual route in main.py: @app.get("/verify/{token}")
    verification_url = f"{BASE_URL}/verify/{token}"

    message = MIMEMultipart("alternative")
    message["Subject"] = "Verify Your UK Healthcare Job Automation Account"
    message["From"] = SMTP_USERNAME
    message["To"] = target_email

    text = f"Welcome! Please verify your account by opening this link: {verification_url}"
    html = f"""
    <html>
      <body style="font-family: sans-serif; color: #334155; padding: 20px;">
        <h2 style="color: #2563eb;">Verify Your Account</h2>
        <p>Thank you for signing up for UK Healthcare Job Automation. Click the button below to activate your account access:</p>
        <a href="{verification_url}" style="display: inline-block; background-color: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 8px; font-weight: bold; margin: 15px 0;">Activate Account</a>
        <p style="font-size: 11px; color: #94a3b8;">If the button does not work, copy and paste this link: {verification_url}</p>
      </body>
    </html>
    """

    message.attach(MIMEText(text, "plain"))
    message.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, target_email, message.as_string())
        return True
    except Exception as e:
        print(f"Mail dispatch error: {e}")
        return False