# Blog engine

resrom.com/blog is static and free: `render_all.py` renders `blog-posts/` into `blog/` at Hosting predeploy (shell from `project.html`); `after_deploy.py` pings IndexNow for the index and posts from the last 2 days at postdeploy. Both are stdlib only.

## Writer contract

- A post is the folder `blog-posts/{slug}/` with `post.json` (fields per `contract.py`, `validate_post`) and its media files named per `contract.MEDIA_FILES`.
- Add or edit the folder; delete the folder to remove the post. A post that fails validation or lacks a referenced media file is skipped and logged.
- Commit and push to `main`: the push-to-main workflow deploys, or run `firebase deploy --only hosting` from the Mac. A remote writer (the bot) commits through the GitHub contents API.
