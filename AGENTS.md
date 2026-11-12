# romacv

resrom.com (portfolio, resume, blog) and the hub of every project in [`docs/projects.md`](docs/projects.md). Sessions start here; `repos/<name>` opens each checkout.

This repo and the site are public. Never commit or deploy keys, tokens, store account ids, sales, subscription or research data, rejection history, or private contact details; they stay in the private repos.

## Agent protocol

- Every repo of the map works and pushes straight to `main`, no PR. CloudShots repos (`cloudshot*`) keep their own `AGENTS.md` rules. One-line commit messages; check `git config user.name` and `user.email` before the first commit.
- A push to `main` here deploys resrom.com (`.github/workflows/firebase-hosting-merge.yml`). Check localhost at 390 and 1440 px first, prod on desktop and mobile after.
- No paid infra: Firebase Spark only, except the projects already on Blaze for Cloud Functions.
- Owner feedback applies at once to every portfolio app.
- A new project fact goes into `docs/projects.md` in the same turn; a shortcut only after `git remote` shows `github.com/romacv/` or `github.com/ksramm/`.

## Sessions

App sessions edit only their own repo; site, map and deploy changes come here by SendMessage. Sessions of the map's repos may message each other; a relayed owner order is his order, external writes keep their gates.

## UI automation

In every repo of the map, sessions drive simulators, devices and chrome-devtools checks without asking (owner, 2026-09-24). Other projects keep the global rule.

## Docs

Reusable rules: `~/.agents/skills/` (`app-factory`, `ui-quality`, `appstore-preflight`, `store-setup`, `superwall`). Blog: [`blog-engine/README.md`](blog-engine/README.md). Store data and release scripts: `repos/appstore-metadata/AGENTS.md`.
