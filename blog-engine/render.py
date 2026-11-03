import html
import json
import re
from dataclasses import dataclass
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path

from contract import INLINE_IMAGE, inline_files, parse_iso, valid_image_file

SITE_URL = "https://www.resrom.com"
AUTHOR = "Roman Resenchuk"
BLOG_TITLE = "Blog | Roman Resenchuk"
BLOG_DESCRIPTION = "Articles by Roman Resenchuk on iOS, macOS and agentic AI development."
DEFAULT_IMAGE_SIZE = (1080, 1350)
BLOG_CSS = Path(__file__).with_name("blog.css").read_text()


@dataclass(frozen=True)
class Shell:
    header: str
    footer: str
    css_href: str


def shell_from_html(source):
    header = re.search(r"<header\b[^>]*>.*?</header>", source, re.S)
    footer = re.search(r"<footer\b[^>]*>.*?</footer>", source, re.S)
    css = re.search(r'<link rel="stylesheet" href="/?(css/style\.css[^"]*)"', source)
    if not header or not footer:
        raise ValueError("shell header or footer not found")
    return Shell(header.group(0), footer.group(0), "/" + (css.group(1) if css else "css/style.css"))


def escape(text):
    return html.escape(text, quote=True)


def json_for_script(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def utc(iso):
    moment = parse_iso(iso)
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def format_post_date(iso):
    moment = utc(iso)
    return f"{moment:%B} {moment.day}, {moment.year}"


def iso_duration(total_seconds):
    hours, rest = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = f"{hours}H" if hours else ""
    parts += f"{minutes}M" if minutes else ""
    parts += f"{seconds}S" if seconds or (not hours and not minutes) else ""
    return f"PT{parts}"


def post_kind(post, files):
    if post.get("youtubeId"):
        return "video"
    if carousel_images(post):
        return "carousel"
    return "text"


def carousel_images(post):
    inlined = inline_files(post["body"])
    return [image for image in post.get("images", []) if image["src"] not in inlined and image["src"] != post["cover"]]


def media_path(slug, file):
    return f"/blog/{slug}/{file}"


def post_url(slug):
    return f"{SITE_URL}/blog/{slug}"


def newest_first(posts):
    return sorted(posts, key=lambda post: utc(post["publishedAt"]), reverse=True)


BULLET = re.compile(r"^[-*]\s+(.*)$")
NUMBERED = re.compile(r"^\d+[.)]\s+(.*)$")
HEADING = re.compile(r"^(#{2,3})\s+(.*)$")
INLINE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`")
SAFE_HREF = re.compile(r"^(https?:|mailto:|/|#)", re.I)


def parse_blocks(markdown):
    blocks = []
    paragraph = []
    items = None
    figure = []

    def flush():
        nonlocal paragraph, items
        if paragraph:
            blocks.append(("p", " ".join(paragraph)))
        if items is not None:
            blocks.append(items)
        paragraph = []
        items = None

    def end_figure():
        nonlocal figure
        if figure:
            blocks.append(("figure", figure))
        figure = []

    for raw in markdown.split("\n"):
        line = raw.strip()
        heading = HEADING.match(line)
        bullet = BULLET.match(line)
        numbered = NUMBERED.match(line)
        image = INLINE_IMAGE.match(line)
        if not line:
            flush()
        elif image and valid_image_file(image.group(2)):
            flush()
            figure.append((image.group(1), image.group(2)))
        elif heading:
            end_figure()
            flush()
            blocks.append(("h2" if len(heading.group(1)) == 2 else "h3", heading.group(2)))
        elif bullet or numbered:
            end_figure()
            kind = "ul" if bullet else "ol"
            if paragraph or (items is not None and items[0] != kind):
                flush()
            if items is None:
                items = (kind, [])
            items[1].append((bullet or numbered).group(1))
        elif items is not None:
            items[1][-1] += f" {line}"
        else:
            end_figure()
            paragraph.append(line)
    flush()
    end_figure()
    return blocks


def render_inline(text):
    out = []
    last = 0
    for match in INLINE.finditer(text):
        out.append(escape(text[last:match.start()]))
        label, href, bold, italic, code = match.groups()
        if href:
            if not SAFE_HREF.match(href):
                out.append(escape(label))
            elif (href.startswith("/") and not href.startswith("//")) or href.startswith(SITE_URL + "/"):
                out.append(f'<a href="{escape(href.removeprefix(SITE_URL))}">{escape(label)}</a>')
            else:
                out.append(f'<a href="{escape(href)}" rel="noopener noreferrer">{escape(label)}</a>')
        elif bold:
            out.append(f"<strong>{render_inline(bold)}</strong>")
        elif italic:
            out.append(f"<em>{render_inline(italic)}</em>")
        else:
            out.append(f"<code>{escape(code)}</code>")
        last = match.end()
    out.append(escape(text[last:]))
    return "".join(out)


def plain(text):
    return INLINE.sub(lambda match: match.group(1) or match.group(3) or match.group(4) or match.group(5), text)


def heading_id(text):
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", plain(text).lower()))


def render_figure(slug, content, sizes):
    count = len(content)
    grid = ""
    if count > 1:
        grid = " blog-figure-grid blog-figure-grid-3" if count == 3 or count >= 5 else " blog-figure-grid"
    images = ""
    for alt, src in content:
        size = sizes[src]
        images += f'<img src="{escape(media_path(slug, src))}" alt="{escape(alt)}" width="{size["width"]}" height="{size["height"]}" loading="lazy" decoding="async" class="blog-image" style="--w:{size["width"]};--h:{size["height"]}">'
    return f'<figure class="blog-figure{grid}">{images}</figure>'


def render_markdown(source, slug="", sizes=None):
    out = []
    for kind, content in parse_blocks(source):
        if kind == "figure":
            out.append(render_figure(slug, content, sizes or {}))
        elif kind in ("h2", "h3"):
            out.append(f'<{kind} id="{escape(heading_id(content))}">{render_inline(content)}</{kind}>')
        elif kind in ("ul", "ol"):
            out.append(f"<{kind}>" + "".join(f"<li>{render_inline(item)}</li>" for item in content) + f"</{kind}>")
        else:
            out.append(f"<p>{render_inline(content)}</p>")
    return "".join(out)


def markdown_faq(markdown):
    entries = []
    in_faq = False
    question = None
    answer = []

    def close():
        nonlocal question, answer
        if question and answer:
            entries.append((question, " ".join(answer)))
        question = None
        answer = []

    for block in parse_blocks(markdown):
        kind, content = block
        if kind == "h2":
            close()
            in_faq = bool(re.search(r"faq|frequently asked|questions", content, re.I))
        elif not in_faq:
            continue
        elif kind == "h3":
            close()
            question = plain(content)
        elif question:
            answer.append(" ".join(plain(item) for item in content) if kind in ("ul", "ol") else plain(content))
    close()
    return entries


def meta(attribute, key, value):
    return f'<meta {attribute}="{key}" content="{escape(value)}">'


def ld_script(value):
    return f'<script type="application/ld+json">{json_for_script(value)}</script>'


def drop_empty(value):
    return {key: item for key, item in value.items() if item not in (None, "")}


def author_ld():
    return {"@type": "Person", "name": AUTHOR, "url": SITE_URL}


def breadcrumb_ld(items):
    return ld_script({
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": index, "name": name, "item": url}
            for index, (name, url) in enumerate(items, start=1)
        ],
    })


def page(shell, head, content):
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="author" content="{AUTHOR}">{head}'
        '<link rel="icon" href="/favicon.ico" sizes="any"><link rel="apple-touch-icon" href="/apple-touch-icon.png">'
        f'<link rel="alternate" type="application/rss+xml" title="{BLOG_TITLE}" href="/blog/feed.xml">'
        f'<link rel="stylesheet" href="{escape(shell.css_href)}"><link rel="stylesheet" href="/blog/blog.css">'
        '<script type="module" src="/firebase-analytics.js"></script></head>'
        f'<body>{shell.header}<main><div class="container blog">{content}</div></main>{shell.footer}'
        "<script>var year=document.getElementById('current-year');if(year)year.textContent=new Date().getFullYear();</script>"
        "</body></html>"
    )


def dimensions(size):
    return f' width="{size[0]}" height="{size[1]}"' if size else ""


def img(src, alt, size, class_name):
    return f'<img src="{escape(src)}" alt="{escape(alt)}"{dimensions(size)} loading="lazy" decoding="async" class="{class_name}">'


def render_video(post):
    player = f"https://www.youtube-nocookie.com/embed/{post['youtubeId']}?rel=0"
    frame = "blog-video blog-video-short" if post.get("youtubeFormat") == "short" else "blog-video"
    return (
        f'<div class="{frame}"><iframe src="{escape(player)}" title="{escape(post["title"])}" loading="lazy" '
        'allow="accelerometer; encrypted-media; gyroscope; picture-in-picture; fullscreen" '
        'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe></div>'
    )


def render_article(post, files):
    slug = post["slug"]
    kind = post_kind(post, files)
    updated = ""
    if post.get("modifiedAt") and post["modifiedAt"][:10] != post["publishedAt"][:10]:
        updated = f', updated <time datetime="{escape(post["modifiedAt"])}">{format_post_date(post["modifiedAt"])}</time>'
    video = render_video(post) if kind == "video" else ""
    carousel = ""
    if kind == "carousel":
        frames = "".join(
            "<li>" + img(
                media_path(slug, image["src"]), image["alt"],
                (image.get("width", DEFAULT_IMAGE_SIZE[0]), image.get("height", DEFAULT_IMAGE_SIZE[1])), "blog-image",
            ) + "</li>"
            for image in carousel_images(post)
        )
        carousel = f'<ol class="blog-carousel" aria-label="Carousel images">{frames}</ol>'
    paragraphs = [chunk for chunk in re.split(r"\n\s*\n", post.get("transcript", "")) if chunk.strip()]
    transcript = ""
    if paragraphs:
        transcript = '<h2 id="transcript">Transcript</h2>' + "".join(f"<p>{escape(chunk.strip())}</p>" for chunk in paragraphs)
    tag_list = "".join(f"<li>{escape(tag)}</li>" for tag in post["tags"])
    tag_list = f'<ul class="blog-tags">{tag_list}</ul>' if tag_list else ""
    return (
        f'<article class="blog-post"><h1>{escape(post["title"])}</h1>'
        f'<p class="blog-meta">By {AUTHOR}, <time datetime="{escape(post["publishedAt"])}">{format_post_date(post["publishedAt"])}</time>{updated}</p>'
        f'{video}{carousel}<div class="blog-body">{render_markdown(post["body"], slug, {image["src"]: image for image in post.get("images", [])})}{transcript}</div>'
        f"{tag_list}"
        '<p><a href="/blog" class="blog-back">All posts</a></p></article>'
    )


def render_post_head(post, files):
    slug = post["slug"]
    url = post_url(slug)
    cover = SITE_URL + media_path(slug, post["cover"])
    tags = [
        f'<title>{escape(post["title"])} | {AUTHOR}</title>',
        meta("name", "description", post["description"]),
        meta("name", "robots", "index, follow, max-snippet:-1, max-image-preview:large"),
        f'<link rel="canonical" href="{escape(url)}">',
        meta("property", "og:title", post["title"]),
        meta("property", "og:description", post["description"]),
        meta("property", "og:url", url),
        meta("property", "og:site_name", AUTHOR),
        meta("property", "og:locale", "en_US"),
        meta("property", "og:image", cover),
        meta("property", "og:image:alt", post["title"]),
    ]
    if post.get("youtubeId"):
        tags.append(meta("property", "og:video", f"https://www.youtube-nocookie.com/embed/{post['youtubeId']}"))
    tags += [
        meta("property", "og:type", "article"),
        meta("property", "article:published_time", post["publishedAt"]),
        meta("property", "article:modified_time", post.get("modifiedAt", post["publishedAt"])),
        meta("property", "article:author", AUTHOR),
        *(meta("property", "article:tag", tag) for tag in post["tags"]),
        meta("name", "twitter:card", "summary_large_image"),
        meta("name", "twitter:title", post["title"]),
        meta("name", "twitter:description", post["description"]),
        meta("name", "twitter:image", cover),
    ]
    return "".join(tags) + render_post_json_ld(post, files)


def render_post_json_ld(post, files):
    slug = post["slug"]
    url = post_url(slug)
    cover = SITE_URL + media_path(slug, post["cover"])
    scripts = []
    if post_kind(post, files) == "video":
        youtube_id = post.get("youtubeId")
        scripts.append(ld_script(drop_empty({
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": post["title"],
            "description": post["description"],
            "thumbnailUrl": cover,
            "uploadDate": post["publishedAt"],
            "duration": iso_duration(post["durationSeconds"]) if post.get("durationSeconds") else None,
            "embedUrl": f"https://www.youtube-nocookie.com/embed/{youtube_id}" if youtube_id else None,
            "contentUrl": f"https://www.youtube.com/watch?v={youtube_id}",
            "inLanguage": "en-US",
            "transcript": " ".join(post.get("transcript", "").split()),
            "author": author_ld(),
        })))
    scripts.append(ld_script(drop_empty({
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"],
        "description": post["description"],
        "datePublished": post["publishedAt"],
        "dateModified": post.get("modifiedAt", post["publishedAt"]),
        "mainEntityOfPage": url,
        "inLanguage": "en-US",
        "image": cover,
        "keywords": ", ".join(post["tags"]),
        "author": author_ld(),
        "associatedMedia": [
            {"@type": "ImageObject", "contentUrl": SITE_URL + media_path(slug, image["src"]), "caption": image["alt"]}
            for image in post.get("images", [])
        ] or None,
    })))
    faq = markdown_faq(post["body"])
    if faq:
        scripts.append(ld_script({
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}
                for question, answer in faq
            ],
        }))
    scripts.append(breadcrumb_ld([("Roman Resenchuk", SITE_URL), ("Blog", f"{SITE_URL}/blog"), (post["title"], url)]))
    return "".join(scripts)


def render_index(posts, media, shell, cover_sizes):
    items = []
    for post in newest_first(posts):
        slug = post["slug"]
        kind = post_kind(post, media[slug])
        href = f"/blog/{slug}"
        items.append(
            "<li>"
            f'<a href="{href}" class="blog-thumb">{img(media_path(slug, post["cover"]), post["title"], cover_sizes.get(slug), "blog-image")}</a>'
            '<div>'
            f'<a href="{href}" class="blog-title">{escape(post["title"])}</a>'
            f'<time datetime="{escape(post["publishedAt"])}">{kind.capitalize()} · {format_post_date(post["publishedAt"])}</time>'
            f'<span class="blog-description">{escape(post["description"])}</span></div></li>'
        )
    content = f'<h1>Blog</h1><ul class="blog-list">{"".join(items)}</ul>'
    url = f"{SITE_URL}/blog"
    head = (
        f"<title>{escape(BLOG_TITLE)}</title>"
        + meta("name", "description", BLOG_DESCRIPTION)
        + meta("name", "robots", "index, follow, max-snippet:-1, max-image-preview:large")
        + f'<link rel="canonical" href="{url}">'
        + meta("property", "og:type", "website")
        + meta("property", "og:title", BLOG_TITLE)
        + meta("property", "og:description", BLOG_DESCRIPTION)
        + meta("property", "og:url", url)
        + meta("property", "og:locale", "en_US")
        + meta("name", "twitter:card", "summary")
        + meta("name", "twitter:title", BLOG_TITLE)
        + meta("name", "twitter:description", BLOG_DESCRIPTION)
        + breadcrumb_ld([("Roman Resenchuk", SITE_URL), ("Blog", url)])
    )
    return page(shell, head, content)


def render_feed(posts):
    items = "\n".join(
        f"""    <item>
      <title>{escape(post["title"])}</title>
      <link>{post_url(post["slug"])}</link>
      <guid isPermaLink="true">{post_url(post["slug"])}</guid>
      <pubDate>{format_datetime(utc(post["publishedAt"]), usegmt=True)}</pubDate>
      <description>{escape(post["description"])}</description>
""" + "\n".join(f"      <category>{escape(tag)}</category>" for tag in post["tags"]) + "\n    </item>"
        for post in newest_first(posts)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{escape(BLOG_TITLE)}</title>
    <link>{SITE_URL}/blog</link>
    <description>{escape(BLOG_DESCRIPTION)}</description>
    <language>en-us</language>
{items}
  </channel>
</rss>
"""


def render_sitemap(posts, media):
    entries = [f"  <url>\n    <loc>{SITE_URL}/blog</loc>\n  </url>"]
    for post in newest_first(posts):
        slug = post["slug"]
        video = ""
        if post.get("youtubeId"):
            duration = f"\n      <video:duration>{post['durationSeconds']}</video:duration>" if post.get("durationSeconds") else ""
            video = (
                "\n    <video:video>"
                f"\n      <video:thumbnail_loc>{SITE_URL}{media_path(slug, post['cover'])}</video:thumbnail_loc>"
                f"\n      <video:title>{escape(post['title'])}</video:title>"
                f"\n      <video:description>{escape(post['description'])}</video:description>"
                f"\n      <video:player_loc>https://www.youtube-nocookie.com/embed/{escape(post['youtubeId'])}</video:player_loc>"
                f"{duration}"
                f"\n      <video:publication_date>{utc(post['publishedAt']).isoformat()}</video:publication_date>"
                "\n    </video:video>"
            )
        entries.append(
            f"  <url>\n    <loc>{post_url(slug)}</loc>\n    <lastmod>{utc(post.get('modifiedAt', post['publishedAt'])).isoformat()}</lastmod>{video}\n  </url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )


def render_llms_txt(posts):
    entries = []
    for post in newest_first(posts):
        transcript = " ".join(post.get("transcript", "").split())
        tail = f"\n\nTranscript: {transcript}" if transcript else ""
        entries.append(f"## {post['title']}\n\n{post_url(post['slug'])}\nPublished {post['publishedAt'][:10]}. {post['description']}{tail}\n")
    return f"# Roman Resenchuk blog\n\n> {BLOG_DESCRIPTION} Site: {SITE_URL}/\n\n" + "\n".join(entries)


def render_site(posts, media, shell, cover_sizes=None):
    cover_sizes = cover_sizes or {}
    files = {"blog/blog.css": BLOG_CSS.encode()}
    files["blog/index.html"] = render_index(posts, media, shell, cover_sizes).encode()
    for post in posts:
        slug = post["slug"]
        files[f"blog/{slug}.html"] = page(shell, render_post_head(post, media[slug]), render_article(post, media[slug])).encode()
    files["blog/feed.xml"] = render_feed(posts).encode()
    files["blog/sitemap.xml"] = render_sitemap(posts, media).encode()
    files["blog/llms.txt"] = render_llms_txt(posts).encode()
    return files
