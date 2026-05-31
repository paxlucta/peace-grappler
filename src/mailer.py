#!/usr/bin/env python3
"""Send PeaceGrappler daily report via Gmail SMTP."""

import os
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file(Path(__file__).parent.parent / ".env")

SMTP_USER = "paxlucta@gmail.com"
SMTP_PASS = os.environ["GMAIL_PASSWORD_TOKEN"]
RECIPIENTS = ["abghandour@gmail.com", "marcello1spinelli@gmail.com"]

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
HTML_FILE = os.path.join(OUTPUT_DIR, "comprehensive-growth-report.html")
EXCEL_FILE = os.path.join(OUTPUT_DIR, "Engagement Rankings.xlsx")

# MTD files use current year-month
YEAR_MONTH = datetime.now().strftime("%Y-%m")
EXCEL_MTD_FILE = os.path.join(OUTPUT_DIR, f"Engagement Rankings {YEAR_MONTH}.xlsx")


def send_email(subject, html_file, recipients, attachments=None):
    """Send HTML email with optional attachments via Gmail SMTP."""

    with open(html_file, "r", encoding="utf-8") as f:
        html_content = f.read()

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText("See HTML version of this email.", "plain"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(alt)

    for path in (attachments or []):
        if os.path.exists(path):
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{os.path.basename(path)}"',
            )
            msg.attach(part)

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, recipients, msg.as_string())

    return True


def main():
    report_time = os.environ.get("REPORT_TIME", "Daily")
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"PeaceGrappler - {report_time.title()} Report {today}"

    print(f"Preparing {report_time.title()} report email...")

    if not os.path.exists(HTML_FILE):
        print(f"ERROR: HTML report not found: {HTML_FILE}")
        sys.exit(1)

    attachments = []
    for f in [EXCEL_FILE, EXCEL_MTD_FILE]:
        if os.path.exists(f):
            attachments.append(f)
            print(f"  Attaching: {os.path.basename(f)}")
        else:
            print(f"  Skipping (not found): {os.path.basename(f)}")

    print(f"Sending to: {', '.join(RECIPIENTS)}")

    try:
        send_email(subject, HTML_FILE, RECIPIENTS, attachments)
        print("SUCCESS: Email sent via Gmail SMTP!")
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
