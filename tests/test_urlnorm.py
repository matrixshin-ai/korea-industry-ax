from urlnorm import normalize_url


def test_strips_utm_params():
    a = normalize_url("https://www.example.com/article/123?utm_source=naver&utm_medium=news")
    b = normalize_url("https://www.example.com/article/123")
    assert a == b


def test_strips_mobile_and_amp_prefix():
    a = normalize_url("https://m.example.com/article/123")
    b = normalize_url("https://amp.example.com/article/123")
    c = normalize_url("https://example.com/article/123")
    assert a == c
    assert b == c


def test_strips_trailing_slash():
    a = normalize_url("https://www.example.com/article/123/")
    b = normalize_url("https://www.example.com/article/123")
    assert a == b


def test_keeps_non_tracking_query_params():
    a = normalize_url("https://www.example.com/article?id=123")
    b = normalize_url("https://www.example.com/article?id=456")
    assert a != b


def test_root_path_slash_preserved():
    assert normalize_url("https://www.example.com/") == normalize_url("https://www.example.com")


def test_empty_url():
    assert normalize_url("") == ""
