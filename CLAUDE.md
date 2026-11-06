# DtekParser: rules for Claude sessions

## One branch: main

- Commit straight to `main` and push it. Create no other branch, local or on
  GitHub, and no worktree on a new branch. Do not propose a branch.
- `.githooks/` enforces this once a clone runs
  `git config core.hooksPath .githooks`: it refuses to create a branch, to
  commit anywhere but `main`, and to push anything but `main`.
- If the checkout is not on `main`, or holds uncommitted work that is not
  yours, stop and say so. Do not stash, reset or switch branches over it.

## Deploy only the pushed main

- Every deployed change is a commit on `main` that is already pushed.
- The home service (`ssh ubuntu`, `/opt/dtek-service`) is deployed only with
  `tools/deploy.sh` (`--dry-run` first shows what would change). It refuses
  unless the checkout is on `main`, clean and equal to `origin/main`, ships
  `git archive HEAD`, and writes the commit to `/opt/dtek-service/REVISION`.
- Never copy code to the server by hand (scp, rsync, `cat >`, `sed -i`,
  `docker cp`) and never run `docker compose build` / `up --build` there
  yourself. `~/.claude/hooks/dtek_deploy_guard.py` blocks that.
- `dtek-edge` (`edge/worker.js` + `app/static/index.html`) and `dtek-brain`
  (`python edge/bundle.py`, see `edge/README.md`) are uploaded only from
  `main`, clean and equal to `origin/main`. The hook checks `wrangler deploy`;
  an upload through the Cloudflare API is on you. Afterwards the deployed
  modules must match the files at `main` byte for byte.
- Roll back with `git revert`, push, `tools/deploy.sh`.
- `/opt/dtek-service/.env` is config, not code: editing it and restarting
  without `--build` is fine.
