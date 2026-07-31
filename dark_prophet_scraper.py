import json
import re
import ssl
import gzip
import logging
import time
import traceback
from typing import Dict, Any, List, Tuple
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TIMEOUT = 20
MAX_PAGE_SIZE = 5 * 1024 * 1024
MAX_RETRIES = 2
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
INVISIBLE_TAGS = {'script', 'style', 'noscript', 'template', 'svg', 'path', 'symbol'}
NOISE_TAGS = {'nav', 'footer', 'aside'}
BLOCK_TAGS = {'p', 'div', 'section', 'article', 'main', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr',
              'blockquote', 'pre', 'table', 'ul', 'ol', 'dl', 'dt', 'dd', 'figcaption', 'figure', 'br',
              'hr', 'address', 'details', 'summary', 'span'}
BLOCKED_HOSTS = {'localhost', '127.0.0.1', '0.0.0.0', '::1'}
BLOCKED_PREFIXES = ('169.254.', '10.', '192.168.', '172.16.', '172.17.', '172.18.')

# Generic English stopwords ONLY - nothing tied to any specific past question/topic.
# This is what makes the tool re-usable across ANY question, on ANY map (practice or tournament).
STOPWORDS = {
    'what', 'is', 'are', 'how', 'do', 'does', 'did', 'can', 'the', 'a', 'an', 'to', 'in', 'on', 'for',
    'with', 'this', 'that', 'of', 'it', 'be', 'have', 'has', 'had', 'will', 'would', 'should', 'could',
    'from', 'about', 'their', 'they', 'which', 'who', 'whom', 'when', 'where', 'why', 'was', 'were',
    'up', 'many', 'much', 'and', 'or', 'but', 'if', 'as', 'at', 'by', 'com', 'www', 'https', 'http',
    'you', 'your', 'me', 'my', 'i', 'we', 'our', 'us', 'tell', 'give', 'me', 'find', 'according'
}

DATE_PATTERN = re.compile(
    r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4}|'
    r'\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{2,4}|\b\d{4}\b)',
    re.IGNORECASE
)
NUMBER_PATTERN = re.compile(r'[\$€£]?\s?\d[\d,]*\.?\d*\s?(?:%|percent|million|billion|thousand|k|m|b)?', re.IGNORECASE)
PROPER_NOUN_PATTERN = re.compile(r'\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3}\b')


class PageParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.title = ''
        self.meta_description = ''
        self.og_data = {}
        self.headings = []
        self.paragraphs = []
        self.list_items = []
        self.table_rows = []
        self.code_blocks = []
        self.links = []
        self.text_parts = []
        self._tag_stack = []
        self._invisible = 0
        self._noise = 0
        self._buf = ''
        self._in_title = False
        self._heading_level = 0
        self._in_pre = False
        self._in_li = False
        self._in_td = False
        self._row_cells = []
        self._link_href = ''
        self._link_buf = ''

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        a = dict(attrs) if attrs else {}
        self._tag_stack.append(tag)
        if tag in INVISIBLE_TAGS:
            self._invisible += 1
            return
        if tag in NOISE_TAGS:
            self._noise += 1
            return
        if self._invisible > 0:
            return
        if tag == 'meta':
            name = a.get('name', '').lower()
            prop = a.get('property', '').lower()
            content = a.get('content', '')
            if name == 'description' and content:
                self.meta_description = content
            elif prop.startswith('og:') and content:
                self.og_data[prop] = content
        elif tag == 'title':
            self._in_title = True
            self._buf = ''
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._flush()
            self._heading_level = int(tag[1])
            self._buf = ''
        elif tag == 'p':
            self._flush()
            self._buf = ''
        elif tag == 'li':
            self._flush()
            self._in_li = True
            self._buf = ''
        elif tag in ('td', 'th'):
            self._in_td = True
            self._buf = ''
        elif tag == 'tr':
            self._row_cells = []
        elif tag == 'pre':
            self._flush()
            self._in_pre = True
            self._buf = ''
        elif tag == 'a':
            self._link_href = a.get('href', '')
            self._link_buf = ''
        elif tag == 'br':
            self._buf += '\n'
        elif tag in BLOCK_TAGS:
            self._flush()
            self._buf = ''

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag in INVISIBLE_TAGS:
            self._invisible = max(0, self._invisible - 1)
            return
        if tag in NOISE_TAGS:
            self._noise = max(0, self._noise - 1)
            return
        if self._invisible > 0:
            return
        if tag == 'title':
            self._in_title = False
            self.title = self._buf.strip()
            self._buf = ''
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            text = self._buf.strip()
            if text:
                self.headings.append((self._heading_level, text))
                hashes = '#' * self._heading_level
                self.text_parts.append(f'\n{hashes} {text}\n')
            self._heading_level = 0
            self._buf = ''
        elif tag == 'p':
            text = self._buf.strip()
            if text:
                self.paragraphs.append(text)
                self.text_parts.append(text + '\n')
            self._buf = ''
        elif tag == 'li':
            text = self._buf.strip()
            if text:
                self.list_items.append(text)
                self.text_parts.append(f'  - {text}\n')
            self._in_li = False
            self._buf = ''
        elif tag in ('td', 'th'):
            text = self._buf.strip()
            if text:
                self._row_cells.append(text)
            self._in_td = False
            self._buf = ''
        elif tag == 'tr':
            if self._row_cells:
                row = ' | '.join(self._row_cells)
                self.table_rows.append(row)
                self.text_parts.append(row + '\n')
            self._row_cells = []
        elif tag == 'pre':
            text = self._buf.strip()
            if text:
                self.code_blocks.append(text)
                self.text_parts.append(f'```\n{text}\n```\n')
            self._in_pre = False
            self._buf = ''
        elif tag == 'a':
            text = self._link_buf.strip()
            href = self._link_href
            if text and href and (not href.startswith(('#', 'javascript:', 'mailto:'))):
                self.links.append((href, text))
            self._link_href = ''
            self._link_buf = ''
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._invisible > 0:
            return
        if self._noise > 0:
            return
        if self._in_pre:
            self._buf += data
        else:
            self._buf += re.sub('[ \\t]+', ' ', data)
        if self._link_href:
            self._link_buf += data

    def handle_entityref(self, name):
        entities = {'amp': '&', 'lt': '<', 'gt': '>', 'quot': '"', 'apos': "'", 'nbsp': ' ', 'mdash': '—',
                    'ndash': '–', 'copy': '©', 'reg': '®', 'trade': '™'}
        self.handle_data(entities.get(name, f'&{name};'))

    def handle_charref(self, name):
        try:
            char = chr(int(name[1:], 16) if name.startswith('x') else int(name))
            self.handle_data(char)
        except (ValueError, OverflowError):
            pass

    def _flush(self):
        text = self._buf.strip()
        if text and len(text) > 1:
            self.text_parts.append(text + '\n')
        self._buf = ''

    def finalize(self):
        self._flush()

    def get_all_text(self) -> str:
        text = ''.join(self.text_parts)
        text = re.sub('\\n{3,}', '\n\n', text)
        return text.strip()


def fetch(url: str, timeout: int = TIMEOUT, attempt: int = 0) -> Tuple[str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'Bad scheme: {parsed.scheme}')
    if not parsed.netloc:
        raise ValueError('No hostname')
    hostname = (parsed.hostname or '').lower()
    if hostname in BLOCKED_HOSTS or any(hostname.startswith(p) for p in BLOCKED_PREFIXES):
        raise ValueError('Blocked address')
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'DNT': '1'
    }
    req = Request(url, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urlopen(req, timeout=timeout, context=ctx)
        raw = resp.read(MAX_PAGE_SIZE)
        enc = resp.headers.get('Content-Encoding', '')
        if 'gzip' in enc.lower():
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        ct = resp.headers.get('Content-Type', '')
        charset = 'utf-8'
        m = re.search('charset=([^\\s;]+)', ct, re.IGNORECASE)
        if m:
            charset = m.group(1).strip()
        try:
            html = raw.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            html = raw.decode('utf-8', errors='replace')
        return html, resp.getcode()
    except HTTPError as e:
        if e.code in (403, 429, 503) and attempt < MAX_RETRIES:
            time.sleep(1 + attempt)
            return fetch(url, timeout, attempt + 1)
        raise Exception(f'HTTP {e.code}: {url}')
    except (URLError, TimeoutError) as e:
        if attempt < MAX_RETRIES:
            time.sleep(1)
            return fetch(url, timeout + 5, attempt + 1)
        raise Exception(f'Fetch failed: {e}')
    except Exception as e:
        if attempt < MAX_RETRIES:
            time.sleep(1)
            return fetch(url, timeout, attempt + 1)
        raise Exception(f'Error: {e}')


def _detect_intent(question: str) -> Dict[str, bool]:
    """Detect generically what KIND of answer the question wants, so scoring can
    boost the right kind of content no matter what the actual topic is."""
    q = question.lower()
    return {
        'wants_number': bool(re.search(r'\bhow (many|much|old|long|far|tall)\b|\bnumber of\b|\bcost\b|\bprice\b|\bpercent', q)),
        'wants_date': bool(re.search(r'\bwhen\b|\bwhat (year|date|day)\b|\bhow (old|long)\b', q)),
        'wants_person': bool(re.search(r'\bwho\b|\bwhose\b', q)),
        'wants_place': bool(re.search(r'\bwhere\b|\blocated?\b|\baddress\b', q)),
        'wants_list': bool(re.search(r'\blist\b|\ball\b|\bwhich (ones|options)\b|\bexamples?\b', q)),
        'wants_reason': bool(re.search(r'\bwhy\b|\breason\b|\bbecause\b', q)),
    }


def _extract_phrases(text: str) -> set:
    """Extract single words AND meaningful 2-word phrases so multi-word entities
    (e.g. 'favorite pastime', 'founding date') can match too."""
    words = [w.lower() for w in re.findall(r"\b[a-zA-Z']{2,}\b", text)]
    keep = [w for w in words if w not in STOPWORDS]
    phrases = set(keep)
    for i in range(len(words) - 1):
        if words[i] not in STOPWORDS or words[i + 1] not in STOPWORDS:
            phrases.add(f'{words[i]} {words[i + 1]}')
    return phrases


def find_relevant(paragraphs: List[str], list_items: List[str], table_rows: List[str],
                   headings: List[Tuple[int, str]], question: str) -> str:
    """Generic relevance ranking - works for ANY question about ANY page.
    No topic-specific keywords are hardcoded here, so this keeps working
    whether it's the practice map or the tournament map, round 1 or round 2."""
    all_content = (
        [(h[1], 'heading') for h in headings] +
        [(p, 'p') for p in paragraphs] +
        [(li, 'li') for li in list_items] +
        [(tr, 'tr') for tr in table_rows]
    )
    if not question:
        return '\n'.join(paragraphs[:20]) if paragraphs else '\n'.join(t for t, _ in all_content[:20])

    q_phrases = _extract_phrases(question)
    if not q_phrases:
        return '\n'.join(paragraphs[:15])

    intent = _detect_intent(question)
    scored = []
    for text, src in all_content:
        t_phrases = _extract_phrases(text)
        overlap = q_phrases & t_phrases
        score = 0.0
        for phrase in overlap:
            score += 2.0 if ' ' in phrase else 1.0

        if not score:
            continue  # no lexical overlap at all -> not relevant, skip

        # Generic intent-based boosts (based on the SHAPE of the question, not its topic)
        if intent['wants_number'] and NUMBER_PATTERN.search(text):
            score += 2
        if intent['wants_date'] and DATE_PATTERN.search(text):
            score += 2
        if intent['wants_person'] and PROPER_NOUN_PATTERN.search(text):
            score += 1.5
        if intent['wants_place'] and re.search(r'\b(street|st\.|avenue|ave\.|city|state|country|located|address)\b', text, re.IGNORECASE):
            score += 1.5
        if src == 'heading':
            score += 1.5  # headings often summarize the key fact
        if src == 'li' or src == 'tr':
            score += 0.5  # lists/tables often hold discrete facts

        scored.append((score, text))

    scored.sort(key=lambda x: x[0], reverse=True)
    if scored:
        # de-duplicate near-identical lines while keeping order
        seen = set()
        out = []
        for _, text in scored:
            key = text[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= 15:
                break
        return '\n\n'.join(out)
    return '\n'.join(paragraphs[:15]) if paragraphs else ''


def scrape_page(url: str, question: str = '') -> Dict[str, Any]:
    html, status = fetch(url)
    parser = PageParser()
    parser.feed(html)
    parser.finalize()
    all_text = parser.get_all_text()
    relevant = find_relevant(parser.paragraphs, parser.list_items, parser.table_rows, parser.headings, question)
    return {
        'url': url,
        'title': parser.title or parser.og_data.get('og:title', ''),
        'description': parser.meta_description,
        'all_text': all_text,
        'relevant': relevant,
        'word_count': len(all_text.split())
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    start = time.time()
    req_id = getattr(context, 'aws_request_id', 'local') if context else 'local'
    try:
        if 'body' in event and ('httpMethod' in event or 'requestContext' in event):
            body = event.get('body', '{}')
            params = json.loads(body) if isinstance(body, str) else body or {}
        elif 'queryStringParameters' in event and event.get('queryStringParameters'):
            params = event['queryStringParameters']
        else:
            params = event

        url = (params.get('url') or '').strip()
        question = (params.get('question') or '').strip()

        if not url:
            return _resp(400, {'success': False, 'error': "Missing 'url'", 'content': None})
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        logger.info(f'[{req_id}] Scraping: {url}')
        result = scrape_page(url, question)
        elapsed = round(time.time() - start, 2)

        page_url = result['url']
        page_title = result['title']
        page_desc = result['description']

        output = []
        output.append(f'URL: {page_url}')
        output.append(f'TITLE: {page_title}')
        if page_desc:
            output.append(f'DESCRIPTION: {page_desc}')
        output.append('')
        output.append('==== MOST RELEVANT CONTENT (read this first) ====')
        output.append(result['relevant'][:20000])
        output.append('')
        output.append('==== FULL PAGE CONTENT (fallback if answer not above) ====')
        output.append(result['all_text'][:40000])

        return _resp(200, {
            'success': True,
            'url': page_url,
            'title': page_title,
            'description': page_desc,
            'content': '\n'.join(output),
            'relevant_excerpt': result['relevant'],
            'word_count': result['word_count'],
            'elapsed_seconds': elapsed
        })
    except Exception as e:
        logger.error(f'[{req_id}] {traceback.format_exc()}')
        return _resp(500, {'success': False, 'error': str(e), 'content': None})


def _resp(code: int, body: Dict) -> Dict[str, Any]:
    return {
        'statusCode': code,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body, ensure_ascii=False, default=str)
    }
