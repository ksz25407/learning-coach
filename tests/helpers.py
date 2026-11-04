"""Test helpers: build small real PDFs and fake HTTP responses (no network)."""
import requests


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one line of text per page ('' = page without text)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 10 Tf 20 700 Td ({safe}) Tj ET" if text else ""
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_no = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def article_html(words: str, repeat: int = 40) -> bytes:
    """An HTML page with enough article text for trafilatura to extract."""
    paras = "".join(f"<p>{words} Paragraph {i} explains the topic in plain, useful detail.</p>"
                    for i in range(repeat))
    return (f"<html><head><title>T</title></head><body><nav>menu</nav>"
            f"<article><h1>Guide</h1>{paras}</article></body></html>").encode()


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None, chunk=65536):
        self.body, self.status_code, self.chunk = body, status, chunk
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.bytes_read = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.body), self.chunk):
            part = self.body[i:i + self.chunk]
            self.bytes_read += len(part)
            yield part


class FakeHTTP:
    """Stands in for requests.get. `pages` maps URL -> FakeResponse or an exception to raise."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        r = self.pages.get(url, FakeResponse(status=404))
        if isinstance(r, Exception):
            raise r
        return r


Timeout = requests.Timeout
