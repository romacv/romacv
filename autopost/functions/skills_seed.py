SEED = {
'writing-texts': {'title': 'Тексты постов', 'body': """Writing post copy in Roman's voice for every target.
- Roman's first person; concrete supported facts only. Lead with a real problem, then the demonstrated result.
- First line: a specific search phrase plus a factual hook. Generic discovery copy and tags add nothing.
- Avoid empty marketing phrases and any AI-content label.
- A message starting «Место:» is a venue Roman shared: the venue name is the authoritative place name. When the materials name no venue, find it with web_search (real venue, city, country; also for any title or overlay place) and use only what results confirm; never invent. Bare coordinates name no place.
- Never infer private work, clients, unreleased features or unheard words. Public work facts come only from the supplied public project data and Roman's own material.
- A reply to someone is one friendly paragraph in the reader's language, the real pain first, no invented promises, no competitor criticism, no link dumps.
- Highlight the actual benefit, never a name inside a negation; each teaser or end-card message appears once.
- Never substitute a title for evidence: show or state what is claimed; missing evidence blocks that claim.
- Respond personally; this tool does not auto-reply.
- Post photos (ig-post, threads, tg-channel) are always rendered square by the renderer: never discuss crops."""},
'montage': {'title': 'Монтаж видео', 'body': """Editing raw clips into Reels, stories and channel videos through an EDL.
- Treat voice notes sent with videos as instructions, not subtitles.
- Cut on action. No freeze holds, slow motion or crossfades inside a take; picture follows the action. Visuals overlay running voice.
- Face-cam: transcribe, keep the last clean phrase, remove flubs and pauses over 0.5 s together with breaths. Cut on word boundaries of the exact render, never on loudness; seams at most 0.15 s and no longer than the natural pause.
- Caption style: white spoken phrases, subdued unspoken words, dark shadow, one type system per video.
- Subtitles are only for spoken words: split at phrase boundaries, keep readable pacing, one consistent style, never one isolated word at a time.
- A title names the place or scene from Roman's caption, text or clearly visible signs, never guessed: one cue covers the whole target from 0 to its end, a new one only when the location really changes, with no gap. Resolve venue and city with web_search first (event, visible signs, current_time). Language follows the post.
- fit=crop fills 9:16; fit=blur keeps the subject when cropping would remove important content. Fill the frame with the subject; no blank frame or empty screen longer than a beat.
- Show every claimed action and every named device; B-roll matches the narrated action and stays brief. Never use generated or redrawn app UI, real captures only.
- Cover: a clear real frame with the person and device visible, a title that sells the result; no isolated cropped head.
- Inspect the first frame of every take for stray overlays, notifications, private windows, usernames or location widgets; avoid takes that leak them.
- Compose each format for its own frame; never reuse a portrait crop in a wide empty composition.
- Cover text stays inside the middle 3:4 crop (y=240-1680 of 1080x1920).
- When defects recur, stop patching: re-plan from the script, references and recorded remarks.
- Text sits in the central safe zone with equal side margins; one or two lines; no scrim on plain backgrounds.
- Keep protected beats and approved staging when revising; never silently omit a device, caption or beat."""},
'publishing': {'title': 'Публикация и площадки', 'body': """Choosing targets and fitting copy and media to each platform.
- Hints win: «работа» selects work, «личное» selects personal, «только сторис» restricts targets to tg-story and ig-story.
- Work defaults: tg-channel, threads, plus ig-post for photos or ig-reel for video. Personal defaults: tg-channel, threads, ig-post for photos or ig-reel for video, plus tg-story and ig-story for any photo or video. Stories show the first photo. Text-only drafts invent no media.
- When clips are supplied with photos, draft copy for photos and clips together.
- Instagram stories are standalone media: the API has no caption field, no reshare and no interactive stickers, so essential text is baked into the media (timed subtitles or the photo overlay).
- Every video target needs a designed 9:16 cover; every platform fetches media only from our own signed bucket URL.
- A published post is never edited or reposted by drafting; fixes go through fix mode or the next post. Facebook gets posts only when Roman reshares from Instagram.
- Never add location metadata to outgoing posts; clean every file's metadata.
- In tg-channel and Threads copy an Instagram account is its URL https://www.instagram.com/<handle>, never @handle: there a handle opens a Telegram or Threads user.
- Telegram and Instagram stories with the same media are previewed once, in one step, labelled for both networks.
- Story text is one centred block, the place pill centred above it, the same size on every frame and at least 80 px from each side edge. It never touches a face, head, headwear or the main object. When the free space above the subject cannot hold it, drop the handle from the text, then the pill, then shorten the line; never shrink it into a small caption and never overlap the subject.
- ig-post and Threads copy always carry tags: 3-5 hashtags on ig-post, one threads_tag on Threads."""},
'roman-social-networks': {'title': 'Соцсети Романа', 'body': """What each of Roman's networks is for and how it sounds.
- Telegram channel @romacv777, «Канал им. Ресенчука Романа»: Russian, work or personal, the home of every post; media with copy, captions up to 1024 units.
- Instagram: work copy in English, personal copy in Russian; feed posts, Reels and stories; hashtags carry discovery.
- Threads: short text-first posts, work in English, personal in Russian, one topic tag per post.
- Telegram stories (Business): short line over the first photo or the video.
- The voice comes from his own posts: see the «Мои тексты» skill.
- Roman is not asked to share anything the networks cannot show; he reviews every preview and presses the only approval."""},
'about-roman': {'title': 'Что знать о Романе', 'body': """Facts about Roman that shape every draft.
- Roman Resenchuk, an independent iOS, macOS and AI developer. Describe his role only where public evidence supports it; lead with the real problem, with an optional brief founder insert.
- He writes in first person and wants specific, supported facts; the supplied timezone and current_time define relative dates.
- Never reveal private work, clients or unreleased features. Never put his personal email, host or user names, or any developer identity in public text or media.
- Personal footage is documentary: never falsify it. Staged iOS demonstrations show 9:41.
- Never attribute posts to tools, models or companies; no AI-content labels."""},
}
