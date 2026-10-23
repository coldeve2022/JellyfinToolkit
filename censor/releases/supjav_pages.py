"""Supjav text metadata only: primary post identity, categories and result links."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

from censor.releases.base import contains_number
from censor.releases.transport import TransportError, HttpTransport, TransportBlocked


def article_url(url):
    parsed = urlsplit(urljoin('https://supjav.com/', url))
    if parsed.scheme != 'https' or parsed.hostname not in ('supjav.com', 'www.supjav.com'):
        return ''
    match = re.fullmatch(r'/(?:[a-z]{2}/)?(\d+)\.html/?', parsed.path)
    return f'https://supjav.com/{match[1]}.html' if match else ''


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    chunks: list = field(default_factory=list)

    def nodes(self, tag=None):
        for child in self.children:
            if tag is None or child.tag == tag:
                yield child
            yield from child.nodes(tag)

    def text(self):
        return ' '.join(' '.join(self.chunks + [c.text() for c in self.children]).split())


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        if self.stack[-1].tag not in ('script', 'style', 'noscript'):
            self.stack[-1].chunks.append(text)


def checked_page(html):
    if HttpTransport._looks_blocked(html.encode('utf-8')):
        raise TransportBlocked('Supjav 需要浏览器验证，请在检索设置连接 Supjav 会话')
    if not html.strip():
        raise TransportError('Supjav 返回空页面')
    return Page(html).root


def primary(root):
    scope = (next(root.nodes('article'), None) or next(root.nodes('main'), None)
             or next((n for n in root.nodes('div') if 'main' in n.attrs.get('class', '').split()), None))
    if scope is None:
        raise TransportError('Supjav 未识别文章内容区域')
    return scope


def search_links(html, number):
    root = checked_page(html)
    scope = next(root.nodes('main'), None) or root
    links = {}
    for anchor in scope.nodes('a'):
        url = article_url(anchor.attrs.get('href', ''))
        text = anchor.text() or anchor.attrs.get('title', '')
        if url and contains_number(text, number):
            links.setdefault(url, text)
    body = next(root.nodes('body'), Node('body'))
    text = scope.text().casefold()
    known_empty = ('search-no-results' in body.attrs.get('class', '') or any(
        term in text for term in ('nothing found', 'no results found', 'no videos found', '未找到', '没有找到', '検索結果がありません')))
    known_search = 'search' in body.attrs.get('class', '') or bool(links) or known_empty
    if not known_search:
        raise TransportError('Supjav 搜索页结构未识别，不能当作无结果')
    has_next = any('next' in (a.attrs.get('rel', '') + ' ' + a.attrs.get('class', '')).split()
                   for a in scope.nodes('a'))
    return links, has_next


def detail_metadata(html, number):
    root = checked_page(html)
    scope = primary(root)
    heading = next(scope.nodes('h1'), None)
    title = heading.text() if heading else ''
    if not title or not contains_number(title, number):
        raise TransportError('Supjav 详情主标题缺少匹配的完整番号')
    categories = []
    meta = next((node for node in scope.nodes() if set(node.attrs.get('class', '').split())
                 & {'post-meta', 'entry-meta', 'post-categories'}), scope)
    category_row = next((node for node in meta.nodes() if 'cat' in node.attrs.get('class', '').split()), meta)
    for anchor in category_row.nodes('a'):
        href = urlsplit(urljoin('https://supjav.com/', anchor.attrs.get('href', '')))
        if href.hostname not in ('supjav.com', 'www.supjav.com'):
            continue
        if '/category/' in href.path or 'category' in anchor.attrs.get('rel', '').split():
            categories.append((anchor.text(), href.path))
    return title, categories
