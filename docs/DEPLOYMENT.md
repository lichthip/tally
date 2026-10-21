# Deployment: GitHub → Render (API) → Vercel (website)

Free tiers throughout: **GitHub** (code), **Neon** (Postgres, already set up), **Render** (Python API + worker + scheduler in one service), **Vercel** (React website). About 30–45 minutes the first time.

```
browser ──► Vercel (React site) ──HTTPS──► Render (FastAPI + worker + scheduler) ──► Neon (Postgres)
                                                    └──► Groq (LLM), Yahoo Finance (prices)
```

---

## Part 1: Put the code on GitHub (GitHub Desktop)

1. **Install** GitHub Desktop (desktop.github.com) and sign in with your GitHub account.
2. **Move the CI workflows into place** (the setup tool couldn't write into `.github`): in File Explorer, inside `Desktop\Tally`, create a folder `.github`, inside it a folder `workflows`, and **copy** both files from `deploy\github-workflows\` into `.github\workflows\`.
3. **Check secrets won't be uploaded.** `Tally\.gitignore` already excludes `backend/.env`, `frontend/.env`, `.venv/`, `node_modules/` and `__pycache__/`. Don't remove those lines.
4. In GitHub Desktop: **File → Add local repository…** → choose `C:\Users\User\Desktop\Tally`. It says "this directory does not appear to be a Git repository", so click **create a repository** in that message.
   - Name: `tally`
   - Description: `Who actually makes good calls? An honest, timestamped track record of stock ideas.`
   - Leave "Initialize with README" unticked (one exists), Git ignore: None (we have one), License: None or MIT.
   - Click **Create repository**.
5. **Review the first commit.** The left panel lists every file to be committed. Scroll through and confirm there is **no** `.env` (only `.env.example`), no `.venv`, no `node_modules`. If any appear, stop and fix `.gitignore` first.
6. Summary: `Initial commit: Tally`, then **Commit to main**.
7. Click **Publish repository**.
   - **Public** if you want interviewers to browse the code (recommended for a portfolio). Untick "Keep this code private".
   - Click **Publish repository**. It's now at `github.com/<you>/tally`.
8. On GitHub, open the **Actions** tab: the `ci` workflow runs the 100 tests and the frontend build on every push. A green tick on the repo is a nice detail to show.

Later changes: edit files → GitHub Desktop shows them → write a summary → **Commit to main** → **Push origin**. Render and Vercel redeploy automatically on every push.

---

## Part 2: Deploy the API on Render

1. Sign up at **render.com** with **Sign in with GitHub** (this links the accounts).
2. **New + → Blueprint**. Pick the `tally` repo, and authorise Render to access it if asked.
3. Render reads `render.yaml` and shows one service, **tally-api** (free plan, Singapore, root dir `backend`). It asks for the values marked secret:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | your Neon connection string (the same one in `backend\.env`; the direct one, **not** `-pooler`) |
   | `GROQ_API_KEY` | your Groq key (`gsk_…`) |
   | `CORS_ORIGINS` | put `http://localhost:5173` for now; you'll replace it with the Vercel URL in Part 3 |

4. Click **Apply**. The first build takes ~3–5 minutes (installing pandas/yfinance is the slow part). Watch the **Logs** tab; success looks like `tally ready: queue=postgres extractor=groq:openai/gpt-oss-120b`.
5. Copy the service URL at the top (e.g. `https://tally-api-xxxx.onrender.com`). Check:
   - `https://tally-api-xxxx.onrender.com/api/health` should return `{"ok":true,...}`
   - `https://tally-api-xxxx.onrender.com/docs` shows the interactive API docs (good to show interviewers)
6. **Get the admin token:** service → **Environment** → `ADMIN_TOKEN` → reveal/copy. That's the token for the website's admin box (it's different from your local `change-me…` one).

Using the same Neon database as local means everything you've already done (simulated contributors, the Reddit posts, the HOOD review) is already there in production. Before the interview, also make a Neon **branch** as a backup snapshot.

**Free-tier behaviour to know about:**
- The service **sleeps after ~15 minutes idle**, and the first request after that takes ~30–60 s to wake it. Open `/api/health` a minute before you demo.
- While asleep, the embedded worker and scheduler pause. Nothing is lost (jobs wait in Postgres), and scoring runs as soon as it wakes.
- Yahoo sometimes throttles cloud servers. If a scoring run logs "no price data", run it again later. Cached prices are reused.

---

## Part 3: Deploy the website on Vercel

1. Sign up at **vercel.com** with **Continue with GitHub**.
2. **Add New… → Project** → find `tally` → **Import**.
3. Configure:
   - **Root Directory:** click **Edit** → choose `frontend`. (Important: the repo root has no website.)
   - **Framework Preset:** Vite (auto-detected).
   - **Environment Variables:** add `VITE_API_URL` = your Render URL from Part 2, step 5 (no trailing slash).
4. **Deploy.** ~1 minute. You get a URL like `https://tally-xxxx.vercel.app`.
5. **Allow the website to call the API (CORS).** Back on Render → tally-api → **Environment** → edit `CORS_ORIGINS` → set it to your Vercel URL exactly, e.g. `https://tally-xxxx.vercel.app` (comma-separate if you also want `http://localhost:5173`) → **Save**. Render restarts the service automatically.
6. Open the Vercel URL. Leaderboard/Ideas/Signals should load (wait for the Render wake-up the first time). Paste the Render `ADMIN_TOKEN` into the admin box to reveal Review and System.

If the site shows "Failed to fetch": (a) Render is still waking, so wait and refresh; (b) `CORS_ORIGINS` doesn't exactly match the Vercel URL (https, no trailing slash); (c) `VITE_API_URL` is wrong. Fix it in Vercel → Settings → Environment Variables, then **Redeploy**, because Vite bakes the variable in at build time.

---

## Part 4 (optional): daily scoring while the free server sleeps

`.github/workflows/score.yml` wakes the API and triggers scoring every weekday after the US close.
GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TALLY_API_URL` = your Render URL
- `TALLY_ADMIN_TOKEN` = the Render `ADMIN_TOKEN`

Test it: **Actions → daily-score → Run workflow**.

---

## Checklist before the interview
- [ ] Neon branch created as a snapshot
- [ ] Render `/api/health` opened a minute before (warm)
- [ ] Admin token pasted in the site's admin box
- [ ] System tab → **Run scoring now** so marks are fresh
- [ ] Local setup still works as a fallback (`python -m tally serve` + `npm run dev`) if conference Wi-Fi or Render misbehaves
