# Blog engine

resrom.com/blog is static and free: `render_all.py` renders Firestore posts into `blog/` at Hosting predeploy (shell from `project.html`); `after_deploy.py` pings IndexNow and clears `publishPending` at postdeploy.

## Writer contract

- Firestore `blog/{slug}` in `resromcom-493b4`: the post fields of `contract.py` (`validate_post`), plus `status` (`published` or `deleted`) and `publishPending: true`.
- Media: `blog/{slug}/media/{file}` = `{data: bytes, sha256}`, file names from `contract.MEDIA_FILES`, at most 900 KB each.
- CLI (ADC): `blog_cli.py put <dir>` (`post.json` plus media; refuses a missing referenced file or one over 900 KB) and `blog_cli.py delete <slug>`.
- A post failing validation is skipped and logged.

## Going live

Within 15 minutes: the schedule in `.github/workflows/firebase-hosting-merge.yml` deploys when a post has `publishPending`; a push to `main` deploys at once.

## IAM

Writer and the CI service account (`FIREBASE_SERVICE_ACCOUNT_RESROMCOM_493B4`): `roles/datastore.user` on `resromcom-493b4`.
