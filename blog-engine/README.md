# Blog engine

resrom.com/blog is static and free: `render_all.py` renders Firestore posts into `blog/` at Hosting predeploy (shell from `project.html`); `after_deploy.py` pings IndexNow and clears `publishPending` at postdeploy.

## Writer contract

- Firestore `blog/{slug}` in `resromcom-493b4`: the post fields of `contract.py` (`validate_post`), plus `status` (`published` or `deleted`) and `publishPending: true`.
- Media: `blog/{slug}/media/{file}` = `{data: bytes, sha256}`, file names from `contract.MEDIA_FILES`, at most 900 KB each.
- CLI (ADC): `blog_cli.py put <dir>` (`post.json` plus media; refuses a missing referenced file or one over 900 KB) and `blog_cli.py delete <slug>`.
- Inline images: a body line `![alt](N.jpg)` (a `contract.MEDIA_FILES` name, uploaded like any media) with an `images` entry of the same `src` and integer `width` and `height`; consecutive lines form one figure, a grid from two images.
- A post failing validation is skipped and logged.

## Going live

After writing, `blog_cli.py` dispatches `.github/workflows/firebase-hosting-merge.yml` (live in about a minute) with a token from `BLOG_DEPLOY_TOKEN` or `~/Documents/keys/autopost/blog_deploy_token.txt`: fine-grained, repository `romacv/romacv` only, permission Actions: Read and write. Without a token the post is saved and goes live with the next deploy (any push to `main`). The bot passes the token as `BLOG_DEPLOY_TOKEN`.

## IAM

Writer and the CI service account (`FIREBASE_SERVICE_ACCOUNT_RESROMCOM_493B4`): `roles/datastore.user` on `resromcom-493b4`.
