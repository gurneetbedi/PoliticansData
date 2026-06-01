# Publishing PolitiTrack India — final manual steps

Everything is staged and ready. The sandbox can't commit/push directly because
a stale `.git/index.lock` is blocking writes on the mounted folder. Run these
five commands from your Mac Terminal, then flip the repo to public on GitHub.

```bash
cd "/Users/gurneetbedi/Desktop/Claude/Project 1/Politicians Project"

# 1. Clear the stale lock (safe — no other git process is running)
rm -f .git/index.lock

# 2. Stop tracking the personal email draft (kept on disk, removed from repo)
git rm --cached ADR_EMAIL_DRAFT.md

# 3. Stage every other change (bug fixes + LICENSE + README + .gitignore)
git add -A

# 4. Commit
git commit -m "Prep for public release: bug fixes, MIT license, README polish

- Fix /browse year= empty-string 422 via resolve_year() permissive parser
- Fix Dashboard /?state= empty-link bug by reordering current_state set
  before _q in base.html
- Add MIT LICENSE
- README: expand current-coverage section to Punjab + Bihar + Goa,
  update project layout, document multi-state ingest targets,
  set license to MIT
- Gitignore: untrack personal ADR outreach draft, add log patterns"

# 5. Push
git push origin main
```

Then on GitHub:

1. Visit https://github.com/gurneetbedi/PoliticansData/settings
2. Scroll to **Danger Zone** → **Change repository visibility** → **Public**
3. Confirm the repo name to flip it.

While you're in Settings, you may also want to:

- Repo name: consider renaming to `politrack-india` (more descriptive than
  `PoliticansData`). Settings → top of page → rename.
- About panel (top of repo page, gear icon): add a one-line description
  ("Open-source transparency for Indian elected representatives"), website
  URL once you deploy, and topics like `transparency`, `india`, `fastapi`,
  `civic-tech`, `open-data`.
- Issues tab: enable templates so first-time contributors have a starting
  point (Settings → Features → Issue templates).

That's it — once it's public anyone can clone, run `python -m app.ingest punjab`,
and have the full dashboard locally in about 10 minutes.
