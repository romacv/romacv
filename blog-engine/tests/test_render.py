import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from render import Shell, render_site, shell_from_html

SHELL = Shell("<header>nav</header>", "<footer>foot</footer>", "/css/style.css")
TEXT = {
    "slug": "text-post",
    "title": "Text <post>",
    "description": "About text.",
    "publishedAt": "2026-11-01T10:00:00Z",
    "cover": "cover.jpg",
    "body": "## Heading\n\nSome **bold** text with [a link](https://example.com) and <script>alert(1)</script>.\n\n- one\n- two",
    "tags": ["notes"],
}
VIDEO = {
    "slug": "video-post",
    "title": "Video post",
    "description": "About video.",
    "publishedAt": "2026-11-02T10:00:00Z",
    "cover": "cover.jpg",
    "body": "Watch it.",
    "tags": ["video"],
    "youtubeId": "dQw4w9WgXcQ",
    "durationSeconds": 754,
    "transcript": "First line.\n\nSecond line.",
}
MEDIA = {"text-post": {"cover.jpg"}, "video-post": {"cover.jpg"}}


def json_ld(page):
    return [json.loads(item) for item in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page)]


def test_text_post_page_is_escaped_with_canonical_and_json_ld():
    files = render_site([TEXT], {"text-post": MEDIA["text-post"]}, SHELL)
    page = files["blog/text-post.html"].decode()
    assert '<link rel="canonical" href="https://www.resrom.com/blog/text-post">' in page
    assert "<title>Text &lt;post&gt; | Roman Resenchuk</title>" in page
    assert "<script>alert(1)</script>" not in page
    assert '<a href="https://example.com" rel="noopener noreferrer">a link</a>' in page
    types = [item["@type"] for item in json_ld(page)]
    assert types == ["BlogPosting", "BreadcrumbList"]


def test_video_post_has_video_object_and_feed_entry():
    files = render_site([TEXT, VIDEO], MEDIA, SHELL)
    page = files["blog/video-post.html"].decode()
    video = json_ld(page)[0]
    assert video["@type"] == "VideoObject"
    assert video["duration"] == "PT12M34S"
    assert "youtube-nocookie.com/embed/dQw4w9WgXcQ" in page
    assert "<h2 id=\"transcript\">Transcript</h2>" in page
    assert "reel" not in "".join(files)
    feed = files["blog/feed.xml"].decode()
    assert "<link>https://www.resrom.com/blog/video-post</link>" in feed
    assert feed.index("video-post") < feed.index("text-post")
    index = files["blog/index.html"].decode()
    assert index.index("video-post") < index.index("text-post")
    assert "blog/sitemap.xml" in files and "blog/llms.txt" in files


def test_shell_is_taken_from_project_html():
    source = '<link rel="stylesheet" href="css/style.css?v=1"><header><div>H</div></header><main></main><footer>F</footer>'
    shell = shell_from_html(source)
    assert shell.header == "<header><div>H</div></header>"
    assert shell.css_href == "/css/style.css?v=1"


def test_only_root_relative_and_site_links_are_internal():
    post = {**TEXT, "body": "[a](/blog/x) [b](//evil.example/x) [c](https://www.resrom.com/blog/y) [d](https://www.resrom.com.evil.example/z)"}
    page = render_site([post], {"text-post": {"cover.jpg"}}, SHELL)["blog/text-post.html"].decode()
    assert '<a href="/blog/x">a</a>' in page
    assert '<a href="//evil.example/x" rel="noopener noreferrer">b</a>' in page
    assert '<a href="/blog/y">c</a>' in page
    assert '<a href="https://www.resrom.com.evil.example/z" rel="noopener noreferrer">d</a>' in page


def test_link_inside_emphasis_is_rendered_and_cover_shown_on_the_post_page():
    post = {**TEXT, "body": "*Posted on [LinkedIn](https://example.com/a)*"}
    page = render_site([post], {"text-post": {"cover.jpg"}}, SHELL)["blog/text-post.html"].decode()
    assert '<em>Posted on <a href="https://example.com/a" rel="noopener noreferrer">LinkedIn</a></em>' in page
    assert 'src="/blog/text-post/cover.jpg" alt="Text &lt;post&gt;"' in page
    assert 'fetchpriority="high"' in page
