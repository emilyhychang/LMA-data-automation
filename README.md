# LMA ServiceTitan Data Automation

The hosted Streamlit app keeps one shared master dataset and one shared
new-leads-only dataset. Approved users sign in, upload only the newest
ServiceTitan export, and the app updates both saved datasets together.

## What is saved

- The active master and new-leads-only CSVs are compressed and stored together
  in PostgreSQL.
- Every successful import keeps the prior pair as a backup; the newest 25
  backups are retained.
- An import log records the user, source filename, timestamp, and row counts.
- A database transaction and advisory lock prevent simultaneous imports from
  overwriting one another.
- Client CSVs and credentials are never committed to GitHub.

## One-time hosted setup

### 1. Create PostgreSQL storage

Create a Supabase project and copy its PostgreSQL **Session pooler** connection
string. Use a database user that can create the private `lma_private` schema
and its three tables. The app creates them automatically on first launch. That
schema should not be added to Supabase's exposed Data API schemas.

The connection string should resemble:

```text
postgresql://USER:PASSWORD@HOST:5432/postgres?sslmode=require
```

Do not expose the database through Supabase's public Data API. The Streamlit
server connects directly with the secret database URL.

### 2. Configure Google login

Create a Google OAuth web client and add these authorized redirect URLs:

- Local: `http://localhost:8501/oauth2callback`
- Hosted: `https://YOUR-APP.streamlit.app/oauth2callback`

Google provides the client ID and client secret used below.

### 3. Configure Streamlit secrets

Copy `servicetitan_reports/.streamlit/secrets.toml.example` to
`servicetitan_reports/.streamlit/secrets.toml` for local development. The real
file is ignored by Git and must never be committed.

For Streamlit Community Cloud, paste the same values into the app's **Secrets**
settings and change `redirect_uri` to the hosted URL. Add every approved user
to `allowed_emails`. Only people in `admin_emails` can perform the one-time
initial dataset upload.

### 4. Install and run locally

```bash
cd servicetitan_reports
python3 -m pip install -r requirements.txt
streamlit run app.py
```

### 5. Initialize the shared datasets

Sign in using an administrator email. The first screen asks for the existing
complete master and new-leads-only CSV once. After initialization, all approved
users see only one upload box for the newest ServiceTitan export.

## Normal workflow

1. Sign in with an approved Google account.
2. Upload the newest ServiceTitan CSV.
3. Select **Clean and save report**.
4. Download the cleaned export or period comparison if needed.

The newest master and new-leads-only datasets remain available for download,
but users no longer need to upload them for every run.

## Private deployment

Keep the GitHub repository private and configure the Streamlit app as private.
The in-app approved-email check remains the final authorization layer even if
someone obtains the app URL.
