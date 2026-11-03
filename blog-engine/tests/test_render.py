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


def test_link_inside_emphasis_is_rendered_and_cover_absent_from_the_post_page():
    post = {**TEXT, "body": "*Posted on [LinkedIn](https://example.com/a)*"}
    page = render_site([post], {"text-post": {"cover.jpg"}}, SHELL)["blog/text-post.html"].decode()
    assert '<em>Posted on <a href="https://example.com/a" rel="noopener noreferrer">LinkedIn</a></em>' in page
    assert "cover.jpg" not in page.split("</head>")[-1]
    assert "blog-cover" not in page


def inline_page(body, images=None, files=("cover.jpg", "1.jpg", "2.jpg")):
    post = {**TEXT, "body": body}
    if images:
        post["images"] = images
    return render_site([post], {"text-post": set(files)}, SHELL)["blog/text-post.html"].decode()


def test_run_of_image_lines_renders_one_figure_with_two_images():
    images = [{"src": "1.jpg", "alt": "One", "width": 800, "height": 1000}, {"src": "2.jpg", "alt": "Two", "width": 800, "height": 1000}]
    page = inline_page("Intro.\n\n![One](1.jpg)\n\n![Two](2.jpg)\n\nOutro.", images)
    figures = re.findall(r'<figure class="blog-figure blog-figure-grid">(.*?)</figure>', page)
    assert len(figures) == 1 and figures[0].count("<img") == 2
    assert 'src="/blog/text-post/1.jpg"' in figures[0] and 'width="800" height="1000" loading="lazy" decoding="async"' in figures[0]


def test_inlined_images_leave_the_carousel():
    images = [{"src": "1.jpg", "alt": "One", "width": 800, "height": 1000}, {"src": "2.jpg", "alt": "Two"}]
    page = inline_page("![One](1.jpg)", images)
    assert page.count('src="/blog/text-post/1.jpg"') == 1
    assert 'class="blog-carousel"' in page and 'src="/blog/text-post/2.jpg"' in page


def test_three_image_run_uses_the_three_column_modifier_and_index_cover_takes_real_size():
    sizes = [{"src": f"{n}.jpg", "alt": "x", "width": 800, "height": 1000} for n in (1, 2, 3)]
    body = "\n\n".join(f"![x]({n}.jpg)" for n in (1, 2, 3))
    assert 'blog-figure-grid blog-figure-grid-3' in inline_page(body, sizes, ("cover.jpg", "1.jpg", "2.jpg", "3.jpg"))
    two = inline_page("![x](1.jpg)\n![x](2.jpg)", sizes[:2])
    assert "blog-figure-grid-3" not in two
    files = render_site([TEXT], {"text-post": {"cover.jpg"}}, SHELL, {"text-post": (600, 900)})
    assert "cover.jpg" not in files["blog/text-post.html"].decode().split("</head>")[-1]
    assert 'width="600" height="900"' in files["blog/index.html"].decode()
