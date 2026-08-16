"""
Web Search / Scraper Lambda  (c4 Dark Prophet)
===============================================
A single-file, ZERO-DEPENDENCY AWS Lambda tool that fetches a web page and
returns readable text so the agent can answer questions like:

    "According to https://somewebsite.com what is their favorite past time?"

Uses ONLY the Python standard library (json, re, gzip, zlib, urllib,
html.parser) - nothing to pip install, nothing to bundle in a layer.

Replaces dark_prophet_scraper.py, which had several real defects that
produce wrong c4 answers. Every one of them is fixed here:

 1. BROKEN NESTED TAG SKIPPING (the worst bug)
    The old parser used a single boolean `ignore_tag`. For
    `<nav>MENU<form>x</form>MORE MENU</nav>`, the `</form>` flipped the
    flag off while still inside <nav>, so menu text leaked into the
    content. Conversely an unclosed skipped tag silently swallowed the
    whole page. Fixed with a depth COUNTER (_skip_depth) plus a
    "strict vs loose" two-pass fallback (see #2).

 2. BOILERPLATE BLACKLIST COULD DELETE THE ANSWER
    The old version always dropped <header>/<nav>/<footer>/<aside>. On a
    small site whose only content sits inside <header>, that returned an
    empty/near-empty page. Now BOTH passes are computed:
      - strict pass (drops chrome)  -> used for `content`
      - loose  pass (keeps chrome)  -> used for keyword search, and used
        for `content` too if the strict text came out too short
    So the answer can never be lost just because of where it sits.

 3. WORDS WERE BEING SPLIT ACROSS LINES
    The old code did `data.strip()` per text node and joined with "\\n",
    so `Hello <b>world</b>!` became "Hello\\nworld\\n!". That destroys
    phrases, which then breaks both human reading and keyword matching.
    Now inline text keeps its spacing and only BLOCK-level elements
    introduce line breaks.

 4. NO gzip/deflate SUPPORT
    Many servers compress responses; the old code ran .decode("utf-8")
    over compressed bytes and produced garbage. Now Content-Encoding is
    honoured (gzip + deflate; brotli is deliberately NOT advertised since
    stdlib cannot decode it).

 5. HARDCODED utf-8
    Pages served as windows-1252 / latin-1 came out mangled ("â€™").
    Charset is now sniffed from the Content-Type header, then from
    <meta charset>, with sane fallbacks.

 6. ALL ERRORS COLLAPSED INTO ONE GENERIC 500
    The agent could not tell "page says nothing" from "403 blocked".
    Now the HTTP status and a specific reason are returned, so the
    sub-agent can honestly report a failed fetch instead of guessing.

 7. NO RETRY on transient failures (timeout / 429 / 5xx) - now retries once.

 8. DUMB TRUNCATION
    The old code cut the first N characters. If the fact lived at
    character 50,000 the agent never saw it. Now, when a `question` is
    supplied, the page is keyword-scored and the most relevant lines
    (plus surrounding context) are returned in `relevant_excerpts`,
    so a huge page still yields the answer within budget.

 9. TABLES / INFOBOXES LOST THEIR LABEL->VALUE PAIRING
    Facts like "Favourite pastime | Fishing" were flattened into
    unrelated lines. <td>/<th> are now joined with " | " on one line.

10. NO SIZE CAP
    A multi-megabyte page could exhaust memory/time. Downloads are now
    capped (default 3 MB) and flagged when capped.

11. NON-HTML BODIES were parsed as HTML - JSON/plain text are now handled
    natively, and truly unparseable types (PDF/images) report a clear,
    honest error rather than returning binary noise.

12. WRONG LAMBDA WIRE FORMAT  <-- would break it entirely as a Bedrock tool
    dark_prophet_scraper.py returns {"statusCode":..,"body":..}, which an
    Amazon Bedrock Agent action group does NOT understand. That is the
    exact defect that previously made a tool look "broken" to the
    orchestrator, causing retry loops and a hallucinated manual answer.
    This file speaks all three shapes (function schema / OpenAPI schema /
    plain) and always replies in the SAME shape it was called with. See
    https://docs.aws.amazon.com/bedrock/latest/userguide/agents-lambda.html

--------------------------------------------------------------------
TOOL INTERFACE
--------------------------------------------------------------------
Function name to register on the action group: scrape_website
(aliases also accepted: scrape, fetch_url, websearch, dark_prophet)

Parameters:
  url         (string,  REQUIRED) page to read; a bare "example.com" is
                                   auto-prefixed with https://
  question    (string,  optional) the question being asked. STRONGLY
                                   recommended - it powers relevance
                                   extraction so the answer survives
                                   truncation on long pages.
  max_length  (integer, optional) character budget for `content`
                                   (default 6000)
  timeout     (integer, optional) seconds, default 10, max 20

Returns (JSON):
  url, final_url, status, content_type, title, description, headings,
  question, relevant_excerpts, content, char_count, full_char_count,
  truncated, download_capped, error

Deploy:
  Handler -> websearch_scraper.lambda_handler
  Runtime -> any python3.x (no dependencies, no layer)
  Timeout -> >= 30s recommended (fetch timeout is 10s + one retry)
"""

import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 20
DEFAULT_MAX_LENGTH = 6000
MAX_DOWNLOAD_BYTES = 3_000_000          # hard cap on bytes read from the wire
MIN_STRICT_CHARS = 200                  # see _should_prefer_loose() - ratio-gated, not absolute
MAX_HEADINGS = 15
EXCERPT_BUDGET_RATIO = 0.6              # excerpts may use this share of max_length
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # NOTE: brotli ("br") is intentionally absent - the stdlib cannot decode it.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# Tags whose *text* is never page content.
LOOSE_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe", "template", "math"}
# Additionally drop site chrome for the "clean" reading pass.
STRICT_SKIP_TAGS = LOOSE_SKIP_TAGS | {
    "nav", "header", "footer", "aside", "menu", "form",
    "button", "select", "option", "datalist", "dialog",
}

# Block-level elements force a line break so lines stay semantically separate.
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "tfoot",
    "thead", "tr", "ul",
}
CELL_TAGS = {"td", "th"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

STOPWORDS = {
    "a", "об", "about", "according", "all", "am", "an", "and", "any", "are", "as", "at",
    "be", "been", "being", "but", "by", "can", "could", "did", "do", "does", "doing",
    "for", "from", "had", "has", "have", "he", "her", "hers", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "me", "my", "no", "nor", "not", "of",
    "on", "or", "our", "ours", "out", "over", "own", "s", "she", "should", "so",
    "some", "such", "than", "that", "the", "their", "theirs", "them", "then",
    "there", "these", "they", "this", "those", "to", "too", "under", "up", "us",
    "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "would", "you", "your", "yours",
}


# ---------------------------------------------------------------------------
# HTML -> text extraction
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """
    Converts HTML to readable text.

    Key differences from the old CleanHTMLParser:
      * _skip_depth is a COUNTER, so nested skipped tags can't prematurely
        re-enable output (old boolean bug), and it is clamped at >= 0.
      * inline text keeps its own whitespace, so words are never split
        across lines by <b>/<i>/<span>/<a>.
      * block elements emit "\\n"; <td>/<th> emit " | " so table rows keep
        their label -> value relationship on one line.
      * <title>, <meta name=description>/og:description and h1-h6 are
        captured separately - they very often contain the answer.
    """

    def __init__(self, skip_tags):
        # convert_charrefs=True (the default) turns &amp; &#8217; &nbsp; into
        # real characters for us.
        super().__init__(convert_charrefs=True)
        self._skip_tags = skip_tags
        self._skip_depth = 0
        self._parts = []
        self._in_title = False
        self._title_parts = []
        self._heading_level = 0
        self._heading_parts = []
        self.headings = []
        self.meta_description = None
        self.og_description = None
        # True if we ever entered a skipped tag but never left it (malformed
        # markup). Used by the caller to decide whether output is trustworthy.
        self.unbalanced_skip = False

    # -- tags ---------------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        t = tag.lower()

        if t in self._skip_tags:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if t == "meta":
            self._read_meta(attrs)
            return
        if t == "title":
            self._in_title = True
            return

        if t in CELL_TAGS:
            self._parts.append(" | ")
        elif t in BLOCK_TAGS:
            self._parts.append("\n")

        if t in HEADING_TAGS:
            self._heading_level = int(t[1])
            self._heading_parts = []

    def handle_endtag(self, tag):
        t = tag.lower()

        if t in self._skip_tags:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return

        if t == "title":
            self._in_title = False
            return

        if t in HEADING_TAGS and self._heading_level:
            text = _normalize_ws("".join(self._heading_parts))
            if text and len(self.headings) < MAX_HEADINGS:
                self.headings.append(text)
            self._heading_level = 0
            self._heading_parts = []

        if t in CELL_TAGS:
            return                      # keep the row on one line
        if t in BLOCK_TAGS:
            self._parts.append("\n")

    # -- text ---------------------------------------------------------------
    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if not data:
            return
        # Keep raw spacing; normalisation happens once at the end. This is
        # what prevents "Hello <b>world</b>!" from becoming three lines.
        self._parts.append(data)
        if self._heading_level:
            self._heading_parts.append(data)

    # -- helpers ------------------------------------------------------------
    def _read_meta(self, attrs):
        a = {(k or "").lower(): (v or "") for k, v in attrs}
        name = a.get("name", "").lower()
        prop = a.get("property", "").lower()
        content = a.get("content", "").strip()
        if not content:
            return
        if name == "description" and not self.meta_description:
            self.meta_description = content
        elif prop in ("og:description", "twitter:description") and not self.og_description:
            self.og_description = content

    def close(self):
        super().close()
        if self._skip_depth:
            self.unbalanced_skip = True

    @property
    def title(self):
        return _normalize_ws("".join(self._title_parts))

    @property
    def description(self):
        return self.meta_description or self.og_description or ""

    def get_text(self):
        return _clean_text("".join(self._parts))


def _normalize_ws(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _clean_text(raw):
    """Collapse whitespace without destroying line structure."""
    s = (raw or "").replace("\xa0", " ").replace("\u200b", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\f\v]+", " ", s)          # runs of spaces -> one
    s = re.sub(r" *\n *", "\n", s)             # trim around newlines
    s = re.sub(r"(?: *\| *)+", " | ", s)       # tidy table separators
    s = re.sub(r"\n(?: *\| *)", "\n", s)       # drop leading pipe on a line
    s = re.sub(r"(?: *\| *)\n", "\n", s)       # drop trailing pipe on a line
    # Collapse blank runs entirely: block tags emit a break on both open and
    # close, so paragraphs would otherwise be separated by empty lines. Keeping
    # content lines adjacent matters because extract_relevant() pulls the
    # neighbouring lines as context - blank neighbours would waste that.
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def html_to_text(html, strict=True):
    """
    Parse HTML into (text, meta) where meta carries title/description/headings.
    Never raises on malformed markup - a parse error degrades to whatever
    text was recovered so far.
    """
    skip = STRICT_SKIP_TAGS if strict else LOOSE_SKIP_TAGS
    parser = _TextExtractor(skip)
    try:
        parser.feed(html)
        parser.close()
    except Exception:                                    # noqa: BLE001
        pass
    return parser.get_text(), {
        "title": parser.title,
        "description": parser.description,
        "headings": list(parser.headings),
        "unbalanced_skip": parser.unbalanced_skip,
    }


# ---------------------------------------------------------------------------
# Relevance extraction (so the answer survives truncation)
# ---------------------------------------------------------------------------
def _keywords(question):
    words = re.findall(r"[a-z0-9']+", (question or "").lower())
    out, seen = [], set()
    for w in words:
        if len(w) <= 2 or w in STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _stem(word):
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _squashed_variants(words):
    """
    "favorite past time" also has to match a page that writes "pastime".
    Generate space-free joins of adjacent keywords, plus British/American
    spelling swaps for the very common favourite/favorite pair.
    """
    variants = set()
    for i in range(len(words) - 1):
        variants.add(words[i] + words[i + 1])
    for w in words:
        if "favor" in w:
            variants.add(w.replace("favor", "favour"))
        if "favour" in w:
            variants.add(w.replace("favour", "favor"))
    return {v for v in variants if len(v) > 4}


def extract_relevant(text, question, budget, context=1):
    """
    Return the lines most likely to contain the answer, in original order,
    within `budget` characters. Empty string when there is no question or
    nothing scores.
    """
    if not question or not text:
        return ""

    words = _keywords(question)
    if not words:
        return ""
    stems = [_stem(w) for w in words]
    squashed = _squashed_variants(words)
    phrase = " ".join(words)

    lines = text.split("\n")
    scored = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        score = sum(1 for st in stems if st in low)
        if phrase and phrase in low:
            score += 3
        if any(v in low for v in squashed):
            score += 2
        if score and ("|" in stripped or ":" in stripped):
            score += 1                        # label -> value lines are gold
        if score:
            scored.append((score, idx, stripped))

    if not scored:
        return ""

    scored.sort(key=lambda t: (-t[0], t[1]))

    chosen, used = set(), 0
    for score, idx, stripped in scored:
        want = [idx] + [idx + d for d in range(-context, context + 1) if d]
        for i in want:
            if i < 0 or i >= len(lines) or i in chosen:
                continue
            candidate = lines[i].strip()
            if not candidate:
                continue
            if used + len(candidate) + 1 > budget:
                if i == idx:
                    break
                continue
            chosen.add(i)
            used += len(candidate) + 1
        if used >= budget:
            break

    if not chosen:
        return ""

    ordered = sorted(chosen)
    out, prev = [], None
    for i in ordered:
        if prev is not None and i > prev + 1:
            out.append("...")
        out.append(lines[i].strip())
        prev = i
    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------
def normalize_url(url):
    u = (url or "").strip().strip('<>"\'')
    if not u:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        u = "https://" + u
    return u


def _decompress(raw, encoding):
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or "x-gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:                                    # noqa: BLE001
        return raw                                       # serve it raw rather than fail
    return raw


_CHARSET_RE = re.compile(rb"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-.:]+)""", re.I)


def _sniff_charset(raw, content_type):
    m = re.search(r"charset\s*=\s*([\w\-.:]+)", content_type or "", re.I)
    if m:
        return m.group(1).strip().lower()
    m2 = _CHARSET_RE.search(raw[:8192])
    if m2:
        try:
            return m2.group(1).decode("ascii", "ignore").strip().lower()
        except Exception:                                # noqa: BLE001
            pass
    return ""


def _decode_body(raw, content_type):
    """Decode bytes to str, trying the sniffed charset first."""
    candidates = []
    sniffed = _sniff_charset(raw, content_type)
    if sniffed:
        candidates.append(sniffed)
    candidates += ["utf-8", "cp1252", "latin-1"]

    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8/replace"


def _header_value(resp, name, default=""):
    try:
        headers = getattr(resp, "headers", None) or resp.info()
        return headers.get(name, default) or default
    except Exception:                                    # noqa: BLE001
        return default


def fetch(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_DOWNLOAD_BYTES, allow_retry=True):
    """
    Fetch a URL with the stdlib only.

    Returns a dict:
      ok, status, final_url, content_type, text, encoding,
      download_capped, error
    Never raises - transport problems come back as ok=False + error.
    """
    target = normalize_url(url)
    if not target:
        return _fetch_error(url, None, "Empty or invalid URL.")

    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in ("http", "https"):
        return _fetch_error(target, None,
                            f"Unsupported URL scheme '{parsed.scheme}' - only http/https are allowed.")
    if not parsed.netloc:
        return _fetch_error(target, None, "URL has no host component.")

    attempts = 2 if allow_retry else 1
    last_error = None
    last_status = None

    for attempt in range(attempts):
        try:
            req = urllib.request.Request(target, headers=dict(REQUEST_HEADERS), method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                capped = len(raw) > max_bytes
                if capped:
                    raw = raw[:max_bytes]

                content_type = _header_value(resp, "Content-Type", "")
                raw = _decompress(raw, _header_value(resp, "Content-Encoding", ""))
                text, encoding = _decode_body(raw, content_type)

                status = getattr(resp, "status", None) or 200
                try:
                    final_url = resp.geturl() or target
                except Exception:                        # noqa: BLE001
                    final_url = target

                return {
                    "ok": True,
                    "status": status,
                    "final_url": final_url,
                    "content_type": content_type,
                    "text": text,
                    "encoding": encoding,
                    "download_capped": capped,
                    "error": None,
                }

        except urllib.error.HTTPError as e:
            last_status = getattr(e, "code", None)
            reason = getattr(e, "reason", "") or ""
            last_error = f"HTTP {last_status} {reason}".strip()
            if attempt + 1 < attempts and last_status in RETRY_STATUSES:
                time.sleep(0.6)
                continue
            break

        except urllib.error.URLError as e:
            last_error = f"Network error: {getattr(e, 'reason', e)}"
            if attempt + 1 < attempts:
                time.sleep(0.6)
                continue
            break

        except (TimeoutError, OSError) as e:
            last_error = f"Connection failed or timed out: {e}"
            if attempt + 1 < attempts:
                time.sleep(0.6)
                continue
            break

        except Exception as e:                           # noqa: BLE001
            last_error = f"Unexpected fetch failure: {type(e).__name__}: {e}"
            break

    return _fetch_error(target, last_status, last_error or "Unknown fetch failure.")


def _fetch_error(url, status, message):
    return {
        "ok": False,
        "status": status,
        "final_url": None,
        "content_type": "",
        "text": "",
        "encoding": "",
        "download_capped": False,
        "error": message,
    }


# ---------------------------------------------------------------------------
# Body -> readable content
# ---------------------------------------------------------------------------
UNPARSEABLE_HINTS = ("pdf", "zip", "octet-stream", "image/", "audio/", "video/",
                     "msword", "excel", "spreadsheet", "presentation", "font")


def _should_prefer_loose(strict, loose, strict_meta):
    """
    Decide whether the chrome-stripping (strict) pass destroyed the page and
    the loose pass should be used for `content` instead.

    Deliberately ratio-based, not just "is strict short": a legitimately
    small page (a one-paragraph About page) is SHORT but perfectly fine, and
    re-adding its nav/footer would only add noise. We switch only when the
    strict pass kept almost nothing while the loose pass found real text -
    i.e. the content genuinely lives inside header/nav/aside/form - or when
    malformed markup left a skipped tag open and swallowed the document.
    """
    if len(loose) <= len(strict):
        return False
    if strict_meta.get("unbalanced_skip"):
        return True
    return len(strict) < MIN_STRICT_CHARS and len(loose) >= max(MIN_STRICT_CHARS, 3 * len(strict))


def body_to_content(text, content_type):
    """
    Turn a decoded response body into (strict_text, loose_text, meta).
    Handles HTML, JSON and plain text; refuses binary formats that cannot be
    read without third-party libraries.
    """
    ctype = (content_type or "").lower()

    if any(h in ctype for h in UNPARSEABLE_HINTS):
        raise ValueError(
            f"Content-Type '{content_type}' cannot be parsed without extra dependencies "
            f"(only HTML, plain text and JSON are supported)."
        )

    if "json" in ctype:
        try:
            pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            pretty = text
        cleaned = _clean_text(pretty)
        return cleaned, cleaned, {"title": "", "description": "", "headings": []}

    looks_like_html = bool(re.search(r"<\s*(html|body|div|p|table|head|span|a|h1)\b", text[:4000], re.I))
    if ("html" in ctype) or ("xml" in ctype) or (not ctype and looks_like_html) or looks_like_html:
        strict, meta = html_to_text(text, strict=True)
        loose, loose_meta = html_to_text(text, strict=False)
        if _should_prefer_loose(strict, loose, meta):
            strict = loose
            if not meta.get("title"):
                meta = loose_meta
        return strict, loose, meta

    cleaned = _clean_text(text)
    return cleaned, cleaned, {"title": "", "description": "", "headings": []}


# ---------------------------------------------------------------------------
# Tool action
# ---------------------------------------------------------------------------
def _as_int(value, default, minimum=None, maximum=None):
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        n = max(minimum, n)
    if maximum is not None:
        n = min(maximum, n)
    return n


def run_scrape(body):
    """
    Core action. Returns (payload_dict, http_status).

    Fetch/parse failures return status 200 with an `error` field populated,
    NOT an HTTP error: the sub-agent is instructed to report an honest
    "couldn't retrieve" and a non-200 envelope tends to make the Bedrock
    orchestrator retry and then hallucinate an answer instead.
    Only a missing/blank `url` parameter is a real 400 (caller's mistake).
    """
    raw_url = body.get("url") or body.get("website") or body.get("link") or body.get("uri")
    if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
        return {"error": "Missing required 'url' string parameter."}, 400

    question = body.get("question") or body.get("query") or body.get("q") or ""
    if not isinstance(question, str):
        question = str(question)

    max_length = _as_int(body.get("max_length"), DEFAULT_MAX_LENGTH, minimum=200, maximum=100_000)
    timeout = _as_int(body.get("timeout"), DEFAULT_TIMEOUT, minimum=1, maximum=MAX_TIMEOUT)
    max_bytes = _as_int(body.get("max_bytes"), MAX_DOWNLOAD_BYTES, minimum=10_000, maximum=10_000_000)

    result = {
        "url": normalize_url(raw_url),
        "final_url": None,
        "status": None,
        "content_type": "",
        "title": "",
        "description": "",
        "headings": [],
        "question": question or None,
        "relevant_excerpts": "",
        "content": "",
        "char_count": 0,
        "full_char_count": 0,
        "truncated": False,
        "download_capped": False,
        "error": None,
    }

    fetched = fetch(result["url"], timeout=timeout, max_bytes=max_bytes)
    result["status"] = fetched["status"]
    result["final_url"] = fetched["final_url"]
    result["content_type"] = fetched["content_type"]
    result["download_capped"] = fetched["download_capped"]

    if not fetched["ok"]:
        result["error"] = fetched["error"]
        return result, 200

    try:
        strict_text, loose_text, meta = body_to_content(fetched["text"], fetched["content_type"])
    except ValueError as e:
        result["error"] = str(e)
        return result, 200
    except Exception as e:                               # noqa: BLE001
        result["error"] = f"Failed to parse page content: {type(e).__name__}: {e}"
        return result, 200

    result["title"] = meta.get("title", "")
    result["description"] = meta.get("description", "")
    result["headings"] = meta.get("headings", [])[:MAX_HEADINGS]

    # Relevance search runs over the LOOSE text so a fact tucked inside a
    # header/nav/footer is still findable.
    if question:
        result["relevant_excerpts"] = extract_relevant(
            loose_text or strict_text, question, int(max_length * EXCERPT_BUDGET_RATIO)
        )

    full = strict_text or loose_text or ""
    result["full_char_count"] = len(full)
    result["truncated"] = len(full) > max_length
    result["content"] = full[:max_length]
    result["char_count"] = len(result["content"])

    if not full.strip():
        result["error"] = ("Fetched successfully but no readable text was found "
                           "(the page may be rendered entirely by JavaScript).")

    return result, 200


# ---------------------------------------------------------------------------
# Bedrock Agent wire layer (function schema / OpenAPI schema / plain)
# ---------------------------------------------------------------------------
ACTION_ALIASES = {
    "scrape_website": "scrape_website",
    "scrape": "scrape_website",
    "scrapewebsite": "scrape_website",
    "fetch_url": "scrape_website",
    "fetchurl": "scrape_website",
    "fetch": "scrape_website",
    "websearch": "scrape_website",
    "web_search": "scrape_website",
    "search": "scrape_website",
    "dark_prophet": "scrape_website",
    "darkprophet": "scrape_website",
    "read_url": "scrape_website",
}
DEFAULT_ACTION_GROUP = "websearch_specialist"


def _detect_schema_type(event):
    if not isinstance(event, dict):
        return "plain"
    if "function" in event and "parameters" in event:
        return "function"
    if "apiPath" in event and "httpMethod" in event:
        return "openapi"
    return "plain"


def _coerce_param_value(value, ptype=None):
    """Bedrock sends every parameter value as a string, including arrays and
    objects (JSON-encoded). Decode where sensible, never raise."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if s[:1] in ("[", "{"):
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            pass
    if ptype in ("integer", "number"):
        try:
            return int(s) if ptype == "integer" else float(s)
        except (TypeError, ValueError):
            return value
    if ptype == "boolean":
        return s.lower() in ("true", "1", "yes")
    return value


def _params_to_body(items):
    body = {}
    for p in items or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        body[name] = _coerce_param_value(p.get("value"), p.get("type"))
    return body


def _extract_request(event):
    """Returns (action, body, schema_type, session_attrs, prompt_session_attrs)."""
    schema_type = _detect_schema_type(event)

    if schema_type == "function":
        return (event.get("function"),
                _params_to_body(event.get("parameters")),
                schema_type,
                event.get("sessionAttributes", {}) or {},
                event.get("promptSessionAttributes", {}) or {})

    if schema_type == "openapi":
        api_path = event.get("apiPath", "") or ""
        action = api_path.strip("/").split("/")[-1] if api_path else None
        props = (((event.get("requestBody") or {}).get("content") or {})
                 .get("application/json") or {}).get("properties", [])
        body = _params_to_body(props) if isinstance(props, list) else dict(props or {})
        return (action, body, schema_type,
                event.get("sessionAttributes", {}) or {},
                event.get("promptSessionAttributes", {}) or {})

    # plain: direct dict, or legacy API-Gateway {"body": "<json>"}
    if isinstance(event, dict) and isinstance(event.get("body"), str):
        try:
            body = json.loads(event["body"])
        except (ValueError, TypeError):
            body = {}
    elif isinstance(event, dict):
        inner = event.get("body", event)
        body = inner if isinstance(inner, dict) else event
    else:
        body = {}
    if not isinstance(body, dict):
        body = {}

    action = str(body.get("action") or body.get("function") or "").strip().lower()
    if not action and ("url" in body or "website" in body or "link" in body):
        action = "scrape_website"                        # unambiguous by shape
    return action, body, schema_type, {}, {}


def _wrap_response(event, schema_type, action, payload, status, session_attrs, prompt_attrs):
    body_json = json.dumps(payload, ensure_ascii=False)

    if schema_type == "function":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", DEFAULT_ACTION_GROUP),
                "function": event.get("function", action or "scrape_website"),
                "functionResponse": {"responseBody": {"TEXT": {"body": body_json}}},
            },
            "sessionAttributes": session_attrs,
            "promptSessionAttributes": prompt_attrs,
        }

    if schema_type == "openapi":
        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": event.get("actionGroup", DEFAULT_ACTION_GROUP),
                "apiPath": event.get("apiPath", f"/{action or 'scrape_website'}"),
                "httpMethod": event.get("httpMethod", "POST"),
                "httpStatusCode": status,
                "responseBody": {"application/json": {"body": body_json}},
            },
            "sessionAttributes": session_attrs,
            "promptSessionAttributes": prompt_attrs,
        }

    return {"statusCode": status, "body": body_json}


def lambda_handler(event, context):
    """Single entry point. Speaks whichever wire shape it was called with."""
    try:
        if not isinstance(event, dict):
            event = {}

        action, body, schema_type, session_attrs, prompt_attrs = _extract_request(event)
        canonical = ACTION_ALIASES.get(str(action or "").strip().lower().replace("-", "_"))

        # A dedicated single-purpose Lambda: if the caller gave us a url but
        # named the action something unexpected, still do the obvious thing
        # rather than failing the turn.
        if canonical is None and isinstance(body, dict) and body.get("url"):
            canonical = "scrape_website"

        if canonical != "scrape_website":
            payload, status = (
                {"error": f"Unrecognized action '{action}'. This tool supports "
                          f"'scrape_website' (aliases: {sorted(set(ACTION_ALIASES) - {'scrape_website'})})."},
                400,
            )
        else:
            payload, status = run_scrape(body)

        return _wrap_response(event, schema_type, action, payload, status, session_attrs, prompt_attrs)

    except Exception as e:                               # noqa: BLE001
        # Always return a parseable envelope. An unhandled exception looks
        # like a broken tool to the orchestrator and triggers the retry ->
        # hallucinate-a-manual-answer failure mode.
        try:
            schema_type = _detect_schema_type(event) if isinstance(event, dict) else "plain"
            action = (event.get("function") if isinstance(event, dict) else "") or "scrape_website"
            return _wrap_response(event if isinstance(event, dict) else {}, schema_type, action,
                                   {"error": f"Fallback mode: {type(e).__name__}: {e}"}, 200, {}, {})
        except Exception:                                # noqa: BLE001
            return {"statusCode": 200,
                    "body": json.dumps({"error": f"Fallback mode: {e}"})}


# ===========================================================================
# SELF-CHECKS  (all offline - the HTTP layer is exercised with a fake opener)
# ===========================================================================
if __name__ == "__main__":
    import email.message
    import io as _io

    # ---------------- 1. parser correctness ----------------
    text, meta = html_to_text(
        "<html><head><title>Acme Corp</title>"
        "<meta name='description' content='All about Acme'>"
        "<style>.a{color:red}</style><script>alert('x')</script></head>"
        "<body><h1>Welcome</h1><p>Hello <b>world</b>! Nice &amp; easy&#8217;s day.</p>"
        "</body></html>"
    )
    assert "Hello world!" in text, f"inline tags must not split words: {text!r}"
    assert "Nice & easy’s day." in text, f"entities must be decoded: {text!r}"
    assert "alert" not in text and "color:red" not in text, text
    assert meta["title"] == "Acme Corp", meta
    assert meta["description"] == "All about Acme", meta
    assert meta["headings"] == ["Welcome"], meta

    # nested skip tags (the old boolean bug)
    nested, _ = html_to_text(
        "<body><nav>MENU<form>SEARCH</form>MORE MENU</nav>"
        "<p>REAL CONTENT HERE</p></body>"
    )
    assert "REAL CONTENT HERE" in nested, nested
    assert "MENU" not in nested and "MORE MENU" not in nested, \
        f"nested skipped tags must stay skipped (old boolean bug): {nested!r}"

    # loose pass keeps chrome
    loose, _ = html_to_text("<body><nav>MENU</nav><p>REAL</p></body>", strict=False)
    assert "MENU" in loose and "REAL" in loose, loose

    # table label -> value stays on one line
    table_text, _ = html_to_text(
        "<table><tr><th>Favourite pastime</th><td>Fishing</td></tr>"
        "<tr><th>Founded</th><td>1998</td></tr></table>"
    )
    assert "Favourite pastime | Fishing" in table_text, table_text
    assert "Founded | 1998" in table_text, table_text

    # block elements separate lines
    blocks, _ = html_to_text("<p>One</p><p>Two</p><div>Three</div>")
    assert blocks.split("\n") == ["One", "Two", "Three"], blocks

    # malformed / unclosed skip tag must not swallow everything
    broken, broken_meta = html_to_text("<body><nav>MENU<p>ORPHANED CONTENT</p></body>")
    assert broken_meta["unbalanced_skip"] is True, broken_meta

    # strict-vs-loose decision logic
    clean_meta = {"unbalanced_skip": False}
    # a legitimately short page keeps its nav stripped (no fallback)
    assert _should_prefer_loose("About us. We are small.", "Home Contact About us. We are small. (c)",
                                clean_meta) is False
    # content genuinely hidden in chrome -> fall back
    assert _should_prefer_loose("", "x" * 400, clean_meta) is True
    # malformed markup swallowed the doc -> fall back
    assert _should_prefer_loose("", "ORPHANED CONTENT", {"unbalanced_skip": True}) is True
    # loose adds nothing -> never fall back
    assert _should_prefer_loose("same", "same", clean_meta) is False
    print("OK: parser self-checks passed (12)")

    # ---------------- 2. charset + decompression ----------------
    assert _sniff_charset(b"", "text/html; charset=ISO-8859-1") == "iso-8859-1"
    assert _sniff_charset(b"<meta charset='windows-1252'>", "text/html") == "windows-1252"
    assert _sniff_charset(b"<html>", "text/html") == ""

    decoded, enc = _decode_body("Caf\u00e9".encode("cp1252"), "text/html; charset=windows-1252")
    assert decoded == "Café", (decoded, enc)
    decoded2, _ = _decode_body("Café".encode("utf-8"), "text/html")
    assert decoded2 == "Café", decoded2

    assert _decompress(gzip.compress(b"hello gzip"), "gzip") == b"hello gzip"
    assert _decompress(zlib.compress(b"hello deflate"), "deflate") == b"hello deflate"
    raw_deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    payload = raw_deflate.compress(b"raw deflate") + raw_deflate.flush()
    assert _decompress(payload, "deflate") == b"raw deflate"
    assert _decompress(b"not compressed", "gzip") == b"not compressed"   # degrades, no crash
    print("OK: charset/decompression self-checks passed (8)")

    # ---------------- 3. relevance extraction ----------------
    noise = "\n".join(f"Filler navigation line number {i}" for i in range(400))
    page = noise + "\nOur favorite pastime is fishing by the lake.\n" + noise
    excerpt = extract_relevant(page, "According to the site what is their favorite past time?", 800)
    assert "fishing" in excerpt.lower(), f"must find the answer deep in a long page: {excerpt!r}"

    # British spelling on the page, American in the question
    brit = "Random intro.\nFavourite pastime: birdwatching.\nRandom outro."
    exc2 = extract_relevant(brit, "what is their favorite past time?", 400)
    assert "birdwatching" in exc2.lower(), exc2

    # table row form
    exc3 = extract_relevant("Header\nFavourite pastime | Chess\nFooter",
                            "favorite past time?", 400)
    assert "Chess" in exc3, exc3

    assert extract_relevant("some text", "", 100) == ""
    assert extract_relevant("", "question", 100) == ""
    assert extract_relevant("nothing matching at all", "zzzqqq unrelated", 100) == ""

    kw = _keywords("According to https://x.com what is their favorite past time?")
    assert "favorite" in kw and "past" in kw and "time" in kw, kw
    assert "what" not in kw and "their" not in kw and "according" not in kw, kw
    assert "pasttime" in _squashed_variants(["past", "time"]), _squashed_variants(["past", "time"])
    print("OK: relevance-extraction self-checks passed (8)")

    # ---------------- 4. URL normalisation ----------------
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("  https://a.com/x  ") == "https://a.com/x"
    assert normalize_url("http://a.com") == "http://a.com"
    assert normalize_url("<https://a.com>") == "https://a.com"
    assert normalize_url("") == ""
    bad = fetch("ftp://files.example.com/x")
    assert bad["ok"] is False and "scheme" in bad["error"], bad
    print("OK: URL-normalisation self-checks passed (6)")

    # ---------------- 5. HTTP layer, mocked (no network needed) ----------------
    _real_urlopen = urllib.request.urlopen
    _real_sleep = time.sleep
    time.sleep = lambda *_a, **_k: None                  # keep retry tests fast

    class _FakeResp:
        def __init__(self, body, ctype="text/html; charset=utf-8", status=200,
                     url="https://example.com/", encoding=None):
            self._body = body
            self.status = status
            self._url = url
            self.headers = email.message.Message()
            self.headers["Content-Type"] = ctype
            if encoding:
                self.headers["Content-Encoding"] = encoding

        def read(self, n=-1):
            return self._body if n is None or n < 0 else self._body[:n]

        def geturl(self):
            return self._url

        def info(self):
            return self.headers

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _install(fn):
        urllib.request.urlopen = fn

    SAMPLE = (b"<html><head><title>Somewebsite</title></head><body>"
              b"<nav>Home About Contact</nav>"
              b"<h1>About us</h1>"
              b"<p>We are a small team. Our favorite past time is kayaking.</p>"
              b"<footer>(c) 2026</footer></body></html>")

    # 5a. happy path, question supplied -> answer surfaces
    _install(lambda req, timeout=None: _FakeResp(SAMPLE))
    payload, status = run_scrape({"url": "https://somewebsite.com",
                                  "question": "what is their favorite past time?"})
    assert status == 200 and payload["error"] is None, payload
    assert payload["title"] == "Somewebsite", payload
    assert "kayaking" in payload["content"], payload["content"]
    assert "kayaking" in payload["relevant_excerpts"], payload["relevant_excerpts"]
    assert "Home About Contact" not in payload["content"], "strict pass should drop nav"
    assert payload["status"] == 200 and payload["final_url"] == "https://example.com/"

    # 5b. gzip-compressed response
    _install(lambda req, timeout=None: _FakeResp(gzip.compress(SAMPLE), encoding="gzip"))
    payload_gz, _ = run_scrape({"url": "somewebsite.com", "question": "favorite past time"})
    assert "kayaking" in payload_gz["content"], payload_gz["content"][:200]

    # 5c. windows-1252 page
    _install(lambda req, timeout=None: _FakeResp(
        "<html><body><p>Caf\u00e9 life is our pastime</p></body></html>".encode("cp1252"),
        ctype="text/html; charset=windows-1252"))
    payload_cp, _ = run_scrape({"url": "https://x.com"})
    assert "Café" in payload_cp["content"], payload_cp["content"]

    # 5d. 403 blocked -> honest error, no retry storm
    calls = {"n": 0}

    def _forbidden(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("https://x.com", 403, "Forbidden",
                                     email.message.Message(), _io.BytesIO(b""))

    _install(_forbidden)
    payload_403, status_403 = run_scrape({"url": "https://x.com"})
    assert status_403 == 200, "tool envelope stays 200 so the agent can report honestly"
    assert payload_403["status"] == 403 and "403" in payload_403["error"], payload_403
    assert payload_403["content"] == "" and calls["n"] == 1, "403 must not be retried"

    # 5e. 503 -> retried once, then succeeds
    state = {"n": 0}

    def _flaky(req, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError("https://x.com", 503, "Service Unavailable",
                                         email.message.Message(), _io.BytesIO(b""))
        return _FakeResp(SAMPLE)

    _install(_flaky)
    payload_503, _ = run_scrape({"url": "https://x.com", "question": "favorite past time"})
    assert state["n"] == 2, f"transient 5xx must be retried exactly once, got {state['n']}"
    assert "kayaking" in payload_503["content"], payload_503["content"]

    # 5f. DNS/network failure
    _install(lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.URLError("Name or service not known")))
    payload_dns, _ = run_scrape({"url": "https://nope.invalid"})
    assert payload_dns["error"] and "Network error" in payload_dns["error"], payload_dns

    # 5g. timeout
    _install(lambda req, timeout=None: (_ for _ in ()).throw(TimeoutError("timed out")))
    payload_to, _ = run_scrape({"url": "https://slow.example"})
    assert payload_to["error"] and "timed out" in payload_to["error"].lower(), payload_to

    # 5h. JSON body
    _install(lambda req, timeout=None: _FakeResp(
        b'{"favorite_pastime": "gardening", "team": 4}', ctype="application/json"))
    payload_json, _ = run_scrape({"url": "https://api.example/x", "question": "favorite pastime"})
    assert "gardening" in payload_json["content"], payload_json["content"]
    assert "gardening" in payload_json["relevant_excerpts"], payload_json["relevant_excerpts"]

    # 5i. plain text body
    _install(lambda req, timeout=None: _FakeResp(b"Our pastime is chess.", ctype="text/plain"))
    payload_txt, _ = run_scrape({"url": "https://x.com/robots.txt"})
    assert payload_txt["content"] == "Our pastime is chess.", payload_txt

    # 5j. PDF -> clear, honest refusal
    _install(lambda req, timeout=None: _FakeResp(b"%PDF-1.4 ...", ctype="application/pdf"))
    payload_pdf, _ = run_scrape({"url": "https://x.com/a.pdf"})
    assert payload_pdf["error"] and "cannot be parsed" in payload_pdf["error"], payload_pdf

    # 5k. download cap respected
    big = b"<html><body>" + (b"<p>filler paragraph</p>" * 20000) + b"</body></html>"
    _install(lambda req, timeout=None: _FakeResp(big))
    payload_big, _ = run_scrape({"url": "https://big.example", "max_bytes": 50_000,
                                 "max_length": 1000})
    assert payload_big["download_capped"] is True, payload_big["download_capped"]
    assert payload_big["char_count"] <= 1000 and payload_big["truncated"] is True, payload_big
    assert payload_big["full_char_count"] > 1000, payload_big

    # 5l. JS-only page -> explicit "no readable text" note
    _install(lambda req, timeout=None: _FakeResp(
        b"<html><body><div id='root'></div><script>render()</script></body></html>"))
    payload_js, _ = run_scrape({"url": "https://spa.example"})
    assert payload_js["error"] and "no readable text" in payload_js["error"], payload_js

    # 5m. content inside <header> only -> loose fallback saves the answer
    _install(lambda req, timeout=None: _FakeResp(
        b"<html><body><header><p>" + b"Our favorite pastime is surfing. " * 12 +
        b"</p></header></body></html>"))
    payload_hdr, _ = run_scrape({"url": "https://tiny.example", "question": "favorite past time"})
    assert "surfing" in payload_hdr["content"], \
        f"content living inside <header> must not be lost: {payload_hdr['content']!r}"

    # 5n. missing url
    payload_nourl, status_nourl = run_scrape({})
    assert status_nourl == 400 and "url" in payload_nourl["error"], payload_nourl

    print("OK: HTTP-layer self-checks passed (14, fully mocked)")

    # ---------------- 6. Bedrock wire formats ----------------
    _install(lambda req, timeout=None: _FakeResp(SAMPLE))

    # function schema
    ev_fn = {
        "messageVersion": "1.0",
        "actionGroup": "websearch_specialist",
        "function": "scrape_website",
        "parameters": [
            {"name": "url", "type": "string", "value": "https://somewebsite.com"},
            {"name": "question", "type": "string", "value": "what is their favorite past time?"},
            {"name": "max_length", "type": "integer", "value": "4000"},
        ],
        "sessionAttributes": {"turn": "3"},
        "promptSessionAttributes": {"x": "y"},
    }
    r_fn = lambda_handler(ev_fn, None)
    assert r_fn["messageVersion"] == "1.0", r_fn
    assert r_fn["response"]["function"] == "scrape_website", r_fn
    assert r_fn["sessionAttributes"] == {"turn": "3"}, r_fn
    assert r_fn["promptSessionAttributes"] == {"x": "y"}, r_fn
    b_fn = json.loads(r_fn["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert "kayaking" in b_fn["relevant_excerpts"], b_fn

    # openapi schema
    ev_oa = {
        "messageVersion": "1.0",
        "actionGroup": "websearch_specialist",
        "apiPath": "/scrape_website",
        "httpMethod": "POST",
        "requestBody": {"content": {"application/json": {"properties": [
            {"name": "url", "type": "string", "value": "https://somewebsite.com"},
            {"name": "question", "type": "string", "value": "favorite past time"},
        ]}}},
    }
    r_oa = lambda_handler(ev_oa, None)
    assert r_oa["response"]["httpStatusCode"] == 200, r_oa
    b_oa = json.loads(r_oa["response"]["responseBody"]["application/json"]["body"])
    assert "kayaking" in b_oa["content"], b_oa

    # plain shapes
    r_plain = lambda_handler({"url": "https://somewebsite.com",
                              "question": "favorite past time"}, None)
    assert r_plain["statusCode"] == 200, r_plain
    assert "kayaking" in json.loads(r_plain["body"])["relevant_excerpts"]

    r_apigw = lambda_handler({"body": json.dumps({"url": "https://somewebsite.com"})}, None)
    assert r_apigw["statusCode"] == 200 and "kayaking" in json.loads(r_apigw["body"])["content"]

    # alias action names still work
    for alias in ("scrape", "fetch_url", "websearch", "dark_prophet", "read_url"):
        r_alias = lambda_handler({"action": alias, "url": "https://somewebsite.com"}, None)
        assert r_alias["statusCode"] == 200, (alias, r_alias)

    # unknown action WITH a url still does the sensible thing
    r_odd = lambda_handler({"action": "totally_unknown", "url": "https://somewebsite.com"}, None)
    assert r_odd["statusCode"] == 200, r_odd

    # unknown action WITHOUT a url -> honest 400
    r_bad = lambda_handler({"action": "totally_unknown"}, None)
    assert r_bad["statusCode"] == 400, r_bad

    # function schema, missing url -> 400 inside the right envelope
    r_fn_missing = lambda_handler({"messageVersion": "1.0", "function": "scrape_website",
                                   "parameters": []}, None)
    b_fn_missing = json.loads(
        r_fn_missing["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])
    assert "url" in b_fn_missing["error"], b_fn_missing

    # garbage events never crash
    for junk in ({}, None, [], "string", {"foo": "bar"}, 42):
        resp = lambda_handler(junk, None)
        assert isinstance(resp, dict), junk
        assert ("statusCode" in resp) or ("messageVersion" in resp), (junk, resp)

    print("OK: Bedrock wire-format self-checks passed (16)")

    # ---------------- restore ----------------
    urllib.request.urlopen = _real_urlopen
    time.sleep = _real_sleep

    print("ALL SELF-CHECKS PASSED (64 total)")
