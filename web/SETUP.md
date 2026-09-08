# ProdMind — Supabase Setup Guide

## Folder Structure
```
/
├── index.html          ← Landing page
├── dashboard.html      ← Main app dashboard
└── auth/
    └── auth-v2.html    ← Sign in / Sign up page
```

---

## Step 1: Run the Database Schema

1. Open your **Supabase dashboard** → https://supabase.com/dashboard
2. Select your project (or create a new one)
3. Go to **SQL Editor** → click **New query**
4. Open the `supabase_schema.sql` file and **paste the entire contents**
5. Click **Run** (green button)

All 14 tables, RLS policies, indexes, and triggers will be created automatically.

> **Already have tables?** The schema uses `IF NOT EXISTS` — re-running it is safe.

---

## Step 2: Configure Your Supabase Credentials

Both files already contain your project credentials. If you ever need to change them (e.g., moving to a different project), search for and update:

```javascript
const SUPABASE_URL  = 'https://YOUR_PROJECT_ID.supabase.co';
const SUPABASE_ANON = 'YOUR_ANON_KEY';
```

This needs updating in **all three files**:
- `index.html`
- `auth/auth-v2.html`
- `dashboard.html`

---

## Step 3: Enable OAuth Providers (Optional)

To enable Google / GitHub sign-in:

1. Go to **Supabase Dashboard** → Authentication → Providers
2. Enable **Google** and/or **GitHub**
3. Add your OAuth app credentials

---

## Step 4: Configure Email Confirmation (Recommended)

1. Go to **Authentication** → **Email Templates** → customize your emails
2. Go to **Authentication** → **URL Configuration**:
   - **Site URL**: `http://localhost:PORT` (for local) or your production domain
   - **Redirect URLs**: add `http://localhost:PORT/dashboard.html`

---

## Step 5: Run Locally

No build step needed — this is pure HTML/JS.

**Option A — VS Code Live Server:**
1. Install the [Live Server extension](https://marketplace.visualstudio.com/items?itemName=ritwickdey.LiveServer)
2. Right-click `index.html` → **Open with Live Server**

**Option B — Python:**
```bash
cd your-project-folder
python3 -m http.server 8080
# Visit http://localhost:8080
```

**Option C — Node.js serve:**
```bash
npx serve .
```

---

## What's Stored in the Database

| Table | What it stores |
|-------|---------------|
| `profiles` | User name, role, timezone, avatar |
| `workspaces` | Workspace name, website, industry, GitHub repo |
| `notification_preferences` | Email notification on/off toggles per user |
| `ai_config` | AI model selection, temperature, rate limits, privacy settings |
| `hooks` | External integrations (App Store, Zendesk, Intercom, etc.) |
| `sync_settings` | Sync frequency, min star rating, auto-tag toggle |
| `api_keys` | Developer API keys (hashed — full key shown once only) |
| `webhook_secrets` | Signing secret per workspace |
| `activity_log` | Timeline of all workspace events |
| `billing` | Plan, price, usage counters, payment card details |
| `invoices` | Historical invoice records |
| `suggested_features` | AI-generated feature recommendations |
| `feedback_synthesis` | AI-synthesized user feedback insights |
| `dashboard_stats` | Cached analytics counters |

---

## Moving to Supabase Cloud

The schema is **100% cloud-compatible**. When you're ready:

1. Create a new project on [supabase.com](https://supabase.com)
2. Run `supabase_schema.sql` in the new project's SQL editor
3. Update `SUPABASE_URL` and `SUPABASE_ANON` in all three HTML files
4. Deploy your HTML files to any static host (Vercel, Netlify, GitHub Pages, etc.)
5. Update the Auth redirect URLs in your new Supabase project settings

That's it — zero migration needed since Supabase local and cloud use the same PostgreSQL schema.

---

## Security Notes

- **API keys are hashed** — the full plaintext key is shown only once at creation. Only the SHA-256 hash is stored in the DB.
- **RLS is enabled** on every table — users can only read/write their own data.
- **Webhook secrets** are protected by RLS and only accessible to the workspace owner.
- Never expose your `service_role` key in frontend code — only the `anon` key is used here.
