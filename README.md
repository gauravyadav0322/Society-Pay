# SocietyPay — ready-to-run MVP

A responsive web app for housing society maintenance management. Includes admin and flat-owner roles, owner registration with approval, flats, monthly billing, overdue tracking, email reminder scheduling, notification logs, demo payment outcomes, demo receipts and CSV export.

## Honest scope note
This is a working MVP, **not a production-certified financial product**. The included payment flow is mock-only and never moves real money. Real Razorpay/Cashfree payments require merchant onboarding, server-side order creation, signature verification, webhook idempotency, refunds and settlement reconciliation; that live integration is not included. WhatsApp messaging is not included. Email can be configured using SMTP.

Before taking paying customers, arrange a security review, persistent PostgreSQL, HTTPS, database backups, monitoring, account recovery, rate limiting, audit logging, and a dependable scheduled worker. Free hosts can sleep; in-process reminder jobs may be delayed when the service sleeps or restarts.

## Run locally (Windows PowerShell)
1. Install Python 3.11+.
2. Extract this ZIP and open PowerShell in the folder containing `requirements.txt`.
3. Run:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```
4. Edit `.env` and replace `SECRET_KEY` with a long random value.
5. Run `uvicorn app.main:app --reload`.
6. Open `http://127.0.0.1:8000`.

If PowerShell blocks activation, use `.venv\Scripts\python.exe -m pip install -r requirements.txt` and `.venv\Scripts\python.exe -m uvicorn app.main:app --reload`.

## Demo accounts
- Admin: `admin@societypay.demo` / `AdminDemo!2026`
- Owner: `owner@societypay.demo` / `OwnerDemo!2026`
- Society join code: `GREENPARK`

Demo data seeds on first database startup only. Change/remove demo accounts and rotate the join code before sharing a deployed app. Owner registrations need admin approval.

## Test the workflow
1. Sign in as admin.
2. Add flats, then open Billing and generate invoices. Set the due date two days in the future to test reminders.
3. Open Notifications and click Run reminders now. If SMTP is not configured, the message is logged/printed instead of delivered.
4. Sign out and sign in as demo owner to view that flat's invoices and simulate payment outcomes.
5. Successful demo payments create a clearly labeled demo receipt. Export invoices as CSV from the admin area.

## SMTP email
Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_USE_TLS` in `.env`. For actual delivery, use a reputable transactional email provider and configure its sender/domain verification.

## Deploy (Render example)
1. Push the project files to your own GitHub repository.
2. Create a managed PostgreSQL database and copy its connection URL into `DATABASE_URL` in the web service environment.
3. Create a Render web service from the repo; `render.yaml` defines the basic build/start configuration.
4. Set a strong `SECRET_KEY`. Enable HTTPS-only cookies by setting `COOKIE_HTTPS_ONLY=true` once HTTPS is configured.
5. Set up a persistent worker or external scheduler for reliable daily reminders. Free services can sleep, so an in-process scheduler alone is not reliable enough for a commercial service.

The app accepts standard `postgresql://` database URLs. Use the provider's recommended URL and keep credentials private.
