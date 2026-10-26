import base64
from datetime import datetime, timezone
from email.utils import format_datetime
import html
import json
from pathlib import Path
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from platforms import CONTEXT, Error, SITE, local, when, markdown, request, checkpoint, link_opens

def shell_page(root, title, description, url, content, image=None, structured=None):
    template = (root / 'project.html').read_text()
    header = re.search(r'<header>.*?</header>', template, re.S).group()
    footer = '<footer>Roman Resenchuk<br>© ' + str(datetime.now().year) + '</footer>'
    image = image or SITE + '/img/avatar3.jpg'
    e = html.escape
    tags = ''.join(f'<meta property="og:{k}" content="{e(v, quote=True)}">' for k, v in {
        'type': 'article', 'title': title, 'description': description, 'url': url, 'image': image, 'locale': 'en_US'}.items())
    tags += ''.join(f'<meta name="twitter:{k}" content="{e(v, quote=True)}">' for k, v in {
        'card': 'summary_large_image', 'title': title, 'description': description, 'image': image, 'url': url}.items())
    ld = '<script type="application/ld+json">' + json.dumps(structured, ensure_ascii=False).replace('<', '\\u003c') + '</script>' if structured else ''
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(title)} | Roman Resenchuk</title><meta name="description" content="{e(description, quote=True)}">'
            f'<meta name="author" content="Roman Resenchuk"><link rel="canonical" href="{e(url, quote=True)}">'
            '<link rel="stylesheet" href="/css/style.css?v=20260930"><link rel="alternate" type="application/rss+xml" href="/blog/feed.xml">'
            f'{tags}{ld}</head><body>{header}<main><div class="container">{content}<hr></div></main>{footer}</body></html>')

def build_files(spec, root, operation=None):
    name, blog = spec['slug'], spec['blog']
    directory = root / 'blog'
    if not operation and (directory / (name + '.html')).exists():
        raise Error('Published blog slug already exists')
    plan = {'files': {}, 'assets': {}}
    url = SITE + '/blog/' + name
    figures = []
    for i, item in enumerate(spec.get('media', {}).get('images', []) if not operation else []):
        source = local(item['path'])
        relative = f'blog/{name}/{i}{source.suffix.lower()}'
        plan['assets'][relative] = item['path']
        figures.append(f'<figure><img class="img-fluid" src="/{relative}" alt="{html.escape(item["alt"], quote=True)}"></figure>')
    image = SITE + '/' + next(iter(plan['assets'])) if plan['assets'] else None
    if operation == 'fix':
        old = (directory / (name + '.html')).read_text()
        figures = re.findall(r'<figure>.*?</figure>',old,re.S)
        metadata = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',old,re.S).group(1))
        image = metadata.get('image')
        plan['assets'] = {}
    ld = {'@context': 'https://schema.org', '@type': 'BlogPosting', 'headline': blog['title'],
          'description': blog['description'], 'datePublished': spec['at'], 'inLanguage': 'en-US',
          'author': {'@type': 'Person', 'name': 'Roman Resenchuk', 'url': SITE},
          'mainEntityOfPage': url, 'url': url, **({'image': image} if image else {})}
    content = f'<article><h1>{html.escape(blog["title"])}</h1><time datetime="{html.escape(spec["at"], quote=True)}">{when(spec).date()}</time>' + ''.join(figures) + markdown(blog['body']) + '</article>'
    plan['files'][f'blog/{name}.html'] = shell_page(root, blog['title'], blog['description'], url, content, image, ld)
    entries = []
    for old in directory.glob('*.html') if directory.exists() else []:
        if old.name in ('index.html', name + '.html'):
            continue
        match = re.search(r'<script type="application/ld\+json">(.*?)</script>', old.read_text(), re.S)
        if not match:
            raise Error('Existing blog page lacks BlogPosting metadata; reconcile index manually')
        entry = json.loads(match.group(1))
        entries.append(entry)
    if operation != 'delete':
        entries.append(ld)
    else:
        plan['files'].pop(f'blog/{name}.html')
    entries.sort(key=lambda x: datetime.fromisoformat(x['datePublished'].replace('Z', '+00:00')), reverse=True)
    listing = '<section class="projects"><h1>Blog</h1><ul>' + ''.join(
        f'<li><time>{html.escape(x["datePublished"][:10])}</time> <a href="{html.escape(x["url"], quote=True)}">{html.escape(x["headline"])}</a><p>{html.escape(x["description"])}</p></li>' for x in entries) + '</ul></section>'
    plan['files']['blog/index.html'] = shell_page(root, 'Blog', 'Notes by Roman Resenchuk on building apps and tools.', SITE + '/blog', listing)
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    for tag, value in (('title', 'Roman Resenchuk'), ('link', SITE + '/blog'), ('description', 'Apps, tools and development notes'), ('language', 'en-US')):
        ET.SubElement(channel, tag).text = value
    for x in entries:
        item = ET.SubElement(channel, 'item')
        for tag, value in (('title', x['headline']), ('link', x['url']), ('guid', x['url']), ('description', x['description']), ('pubDate', format_datetime(datetime.fromisoformat(x['datePublished'].replace('Z', '+00:00'))))):
            ET.SubElement(item, tag).text = value
    plan['files']['blog/feed.xml'] = ET.tostring(rss, encoding='unicode', xml_declaration=True)
    ns = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    ET.register_namespace('', ns)
    sitemap = ET.parse(root / 'sitemap.xml').getroot() if (root / 'sitemap.xml').exists() else ET.Element('{' + ns + '}urlset')
    if operation == 'delete':
        for entry in list(sitemap):
            if entry.findtext('{' + ns + '}loc') == url:
                sitemap.remove(entry)
    present = {x.text for x in sitemap.iter('{' + ns + '}loc')}
    for link in ((SITE + '/blog',) if operation == 'delete' else (url, SITE + '/blog')):
        if link not in present:
            ET.SubElement(ET.SubElement(sitemap, '{' + ns + '}url'), '{' + ns + '}loc').text = link
    plan['files']['sitemap.xml'] = ET.tostring(sitemap, encoding='unicode', xml_declaration=True)
    return plan

class GitHub:
    def __init__(self, config):
        self.token = config['github_pat']
        self.email = config['github_noreply']
        if not re.fullmatch(r'[A-Za-z0-9+_.-]+@users\.noreply\.github\.com',self.email):
            raise Error('GitHub noreply author address required')
        self.base = 'https://api.github.com/repos/romacv/romacv'

    def api(self, method, path, value=None):
        return request(method,self.base + path,token=self.token,data=json.dumps(value).encode() if value is not None else None,headers={'Accept':'application/vnd.github+json','Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'})

    def publish(self, spec, state, root):
        parent = checkpoint(spec,state,'blog','parent',lambda:self.api('GET','/git/ref/heads/main')['object']['sha'])
        commit = self.api('GET','/git/commits/' + parent)
        tree = self.api('GET','/git/trees/' + commit['tree']['sha'] + '?recursive=1')
        if tree.get('truncated'):
            raise Error('Git tree truncated')
        entries = {x['path']:x for x in tree['tree'] if x['type'] == 'blob'}
        for path, entry in entries.items():
            if path in ('project.html','sitemap.xml') or (path.startswith('blog/') and path.endswith('.html')):
                content = self.api('GET','/git/blobs/' + entry['sha'])
                destination = root / path
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(base64.b64decode(content['content']))
        plan = build_files(spec,root)
        files = {path:content.encode() for path,content in plan['files'].items()}
        files.update({path:local(source).read_bytes() for path,source in plan['assets'].items()})
        nodes = []
        for path, content in files.items():
            sha = checkpoint(spec,state,'blog','blob-' + path,lambda content=content:self.api('POST','/git/blobs',{'content':base64.b64encode(content).decode(),'encoding':'base64'})['sha'])
            nodes.append({'path':path,'mode':'100644','type':'blob','sha':sha})
        tree_sha = checkpoint(spec,state,'blog','tree',lambda:self.api('POST','/git/trees',{'base_tree':commit['tree']['sha'],'tree':nodes})['sha'])
        new_commit = checkpoint(spec,state,'blog','commit',lambda:self.api('POST','/git/commits',{'message':'Add ' + spec['blog']['title'],'tree':tree_sha,'parents':[parent],'author':{'name':'Roman Resenchuk','email':self.email,'date':spec['at']},'committer':{'name':'Roman Resenchuk','email':self.email,'date':spec['at']}})['sha'])
        def publish_ref():
            try:
                return self.api('PATCH','/git/refs/heads/main',{'sha':new_commit,'force':False})
            except Error as failure:
                if failure.status_code != 422:
                    raise
                current = self.api('GET','/git/ref/heads/main')
                if current['object']['sha'] == new_commit:
                    return current
                if current['object']['sha'] != parent:
                    store = CONTEXT.get()[1]
                    for name in list(state['steps']):
                        if name.startswith('blog:'):
                            store.invalidate(spec['slug'],name)
                raise
        checkpoint(spec,state,'blog','publish',publish_ref)
        return SITE + '/blog/' + spec['slug']

    def deployed(self, spec, url, state):
        commit = state['steps']['blog:commit']['value']
        for _ in range(8):
            runs = request('GET', self.base + '/actions/runs?head_sha=' + commit).get('workflow_runs',[])
            completed = runs and all(run.get('status') == 'completed' and run.get('conclusion') == 'success' for run in runs)
            if completed and link_opens(url,spec['blog']['title']):
                return True
            time.sleep(10)
        return False


    def change_post(self, spec, path, operation, root):
        if path != 'blog/' + spec['slug'] + '.html':
            raise Error('Blog path mismatch')
        parent = self.api('GET','/git/ref/heads/main')['object']['sha']
        commit = self.api('GET','/git/commits/' + parent)
        tree = self.api('GET','/git/trees/' + commit['tree']['sha'] + '?recursive=1')
        if tree.get('truncated'):
            raise Error('Git tree truncated')
        entries = {x['path']:x for x in tree['tree'] if x['type'] == 'blob'}
        if path not in entries:
            raise Error('Blog post missing; reconcile')
        for name, entry in entries.items():
            if name in ('project.html','sitemap.xml') or name.startswith('blog/') and name.endswith('.html'):
                content = self.api('GET','/git/blobs/' + entry['sha'])
                destination = root / name
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(base64.b64decode(content['content']))
        plan = build_files(spec,root,operation)
        nodes = []
        for name, content in plan['files'].items():
            sha = self.api('POST','/git/blobs',{'content':base64.b64encode(content.encode()).decode(),'encoding':'base64'})['sha']
            nodes.append({'path':name,'mode':'100644','type':'blob','sha':sha})
        if operation == 'delete':
            for name in entries:
                if name == path or re.fullmatch(r'blog/' + re.escape(spec['slug']) + r'/[0-9]+\.jpg',name):
                    nodes.append({'path':name,'mode':'100644','type':'blob','sha':None})
        tree_sha = self.api('POST','/git/trees',{'base_tree':commit['tree']['sha'],'tree':nodes})['sha']
        author = {'name':'Roman Resenchuk','email':self.email,'date':datetime.now(timezone.utc).isoformat()}
        new_commit = self.api('POST','/git/commits',{'message':('Delete ' if operation == 'delete' else 'Fix ') + spec['blog']['title'],'tree':tree_sha,'parents':[parent],'author':author,'committer':author})['sha']
        try:
            self.api('PATCH','/git/refs/heads/main',{'sha':new_commit,'force':False})
        except Error as failure:
            if failure.status_code != 422 or self.api('GET','/git/ref/heads/main')['object']['sha'] != new_commit:
                raise
        return new_commit
