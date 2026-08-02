import json
import re
import urllib.request
from html.parser import HTMLParser

class CleanHTMLParser(HTMLParser):
    """
    A lightweight HTML parser using only built-in Python modules.
    Strips out JavaScript, CSS, and extracts readable text.
    """
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.ignore_tag = False
        self.ignored_tags = {"script", "style", "nav", "footer", "header", "aside", "noscript", "form"}

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.ignored_tags:
            self.ignore_tag = True

    def handle_endtag(self, tag):
        if tag.lower() in self.ignored_tags:
            self.ignore_tag = False

    def handle_data(self, data):
        if not self.ignore_tag:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned)

    def get_text(self):
        return "\n".join(self.text_parts)


def lambda_handler(event, context):
    """
    AWS Lambda entry point.
    Accepts input: {"url": "https://example.com", "max_length": 4000}
    """
    # Parse input body or event dictionary
    if isinstance(event, str):
        body = json.loads(event)
    elif "body" in event and isinstance(event["body"], str):
        body = json.loads(event["body"])
    else:
        body = event if isinstance(event, dict) else {}

    url = body.get("url")
    max_length = body.get("max_length", 4000)

    if not url:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Missing required parameter 'url'."})
        }

    # Browser user-agent header to reduce basic blocking
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            html_bytes = response.read()
            html_content = html_bytes.decode("utf-8", errors="ignore")

        # Parse HTML using Python standard library
        parser = CleanHTMLParser()
        parser.feed(html_content)
        raw_text = parser.get_text()

        # Clean up double line breaks and empty spaces
        clean_text = re.sub(r"\n\s*\n", "\n\n", raw_text)

        # Truncate content for agent budget
        is_truncated = len(clean_text) > max_length
        final_text = clean_text[:max_length]

        return {
            "statusCode": 200,
            "body": json.dumps({
                "url": url,
                "content": final_text,
                "truncated": is_truncated,
                "char_count": len(final_text)
            })
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "url": url,
                "error": f"Failed to scrape website: {str(e)}"
            })
        }
