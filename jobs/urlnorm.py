"""URL normalization for dedup keys: strips tracking params, m./amp. prefixes, trailing slash."""
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_KEYS = {"gclid", "fbclid", "igshid", "ref", "from", "spm", "sc", "rankingSectionId", "rankingSeq"}


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()

    netloc = parsed.netloc.lower()
    if netloc.startswith("m."):
        netloc = netloc[2:]
    if netloc.startswith("amp."):
        netloc = netloc[4:]

    path = parsed.path or "/"
    if path.endswith("/amp"):
        path = path[: -len("/amp")] or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept_params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES) and k.lower() not in _TRACKING_PARAM_KEYS
    ]
    query = urlencode(kept_params)

    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))
