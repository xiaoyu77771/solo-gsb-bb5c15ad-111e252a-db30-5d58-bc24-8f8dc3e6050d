"""展示正文选择（GET /messages/{id}/display-body）测试。

覆盖：alternative 的 prefer/最后可展示分支、related 的 start 定位与
重复命中 422、mixed 的首个可展示子件、attachment 与 message/rfc822
排除、未知 multipart 子类型按 mixed 处理、轨迹内容、404/422、
以及“查询不改写记录”。
"""

import base64

from conftest import make_multipart

# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def plain_part(text: str, *, disposition: str | None = None) -> bytes:
    head = "Content-Type: text/plain; charset=utf-8\r\n"
    if disposition:
        head += f"Content-Disposition: {disposition}\r\n"
    return head.encode() + b"\r\n" + text.encode()


def html_part(
    html: str,
    *,
    cid: str | None = None,
    disposition: str | None = None,
    cte: str | None = None,
) -> bytes:
    head = "Content-Type: text/html; charset=utf-8\r\n"
    if cid:
        head += f"Content-ID: <{cid}>\r\n"
    if disposition:
        head += f"Content-Disposition: {disposition}\r\n"
    if cte:
        head += f"Content-Transfer-Encoding: {cte}\r\n"
    body = html.encode()
    if cte == "base64":
        body = base64.b64encode(body)
    return head.encode() + b"\r\n" + body


def typed_part(ctype: str, body: str = "x") -> bytes:
    return f"Content-Type: {ctype}\r\n\r\n{body}".encode()


def blob_part(
    ctype: str = "application/octet-stream",
    data: bytes = b"\x00\x01\x02",
    *,
    cid: str | None = None,
    disposition: str | None = None,
) -> bytes:
    head = f"Content-Type: {ctype}\r\n"
    if cid:
        head += f"Content-ID: <{cid}>\r\n"
    if disposition:
        head += f"Content-Disposition: {disposition}\r\n"
    head += "Content-Transfer-Encoding: base64\r\n"
    return head.encode() + b"\r\n" + base64.b64encode(data)


def rfc822_part(inner: bytes) -> bytes:
    return b"Content-Type: message/rfc822\r\n\r\n" + inner


def _container(ctype: str, parts: list[bytes], boundary: str,
               extra_params: str = "") -> bytes:
    head = (
        f"Content-Type: {ctype}; boundary={boundary}{extra_params}"
        "\r\n\r\n"
    ).encode()
    return head + make_multipart(parts, boundary)


def alternative(parts: list[bytes], boundary="ALT") -> bytes:
    return _container("multipart/alternative", parts, boundary)


def mixed(parts: list[bytes], boundary="MIX") -> bytes:
    return _container("multipart/mixed", parts, boundary)


def related(parts: list[bytes], boundary="REL", start: str | None = None) -> bytes:
    extra = f'; start="{start}"' if start else ""
    return _container("multipart/related", parts, boundary, extra)


def post(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def body(client, mid: int, prefer: str | None = None):
    url = f"/messages/{mid}/display-body"
    if prefer:
        url += f"?prefer={prefer}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def rules(resp) -> list[str]:
    return [t["rule"] for t in resp["trace"]]


# ---------------------------------------------------------------------------
# alternative：prefer 与最后可展示分支
# ---------------------------------------------------------------------------


def test_alternative_prefers_html_by_default(client):
    msg = alternative([plain_part("plain"), html_part("<p>hi</p>")])
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "2"
    assert resp["content_type"] == "text/html"
    assert resp["text"] == "<p>hi</p>"
    assert rules(resp) == ["alternative.prefer", "leaf"]
    top = resp["trace"][0]
    assert top["node_path"] == ""
    assert top["chosen"] == "2"
    assert top["candidates"] == ["1", "2"]
    assert top["detail"] == {"prefer": "html"}


def test_alternative_prefer_plain(client):
    msg = alternative([plain_part("plain"), html_part("<p>hi</p>")])
    mid = post(client, msg)
    resp = body(client, mid, prefer="plain")
    assert resp["node_path"] == "1"
    assert resp["content_type"] == "text/plain"
    assert resp["text"] == "plain"
    assert rules(resp) == ["alternative.prefer", "leaf"]
    assert resp["trace"][0]["detail"] == {"prefer": "plain"}


def test_alternative_no_match_takes_last_displayable(client):
    # prefer=html 但没有任何 text/html：取最后一个可展示分支
    msg = alternative([plain_part("p"), typed_part("text/enriched", "e")])
    mid = post(client, msg)
    resp = body(client, mid)  # 默认 prefer=html
    assert resp["node_path"] == "2"
    assert resp["content_type"] == "text/enriched"
    assert rules(resp) == ["alternative.last_displayable", "leaf"]


def test_alternative_skips_attachment_branch(client):
    # html 分支是 attachment：不可展示，回退到 plain
    msg = alternative(
        [plain_part("p"), html_part("<p>a</p>", disposition="attachment")]
    )
    mid = post(client, msg)
    resp = body(client, mid)  # prefer=html
    assert resp["node_path"] == "1"
    assert resp["content_type"] == "text/plain"
    assert rules(resp) == ["alternative.last_displayable", "leaf"]
    assert resp["trace"][0]["candidates"] == ["1"]


def test_nested_alternative_prefer_matches_descendant(client):
    # 内层 alternative 的 html 通过外层 prefer 命中
    inner = alternative([plain_part("p2"), html_part("<p>h</p>")], boundary="ALT2")
    msg = alternative([plain_part("p1"), inner])
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "2/2"
    assert resp["content_type"] == "text/html"
    assert rules(resp) == [
        "alternative.prefer",
        "alternative.prefer",
        "leaf",
    ]
    assert [t["node_path"] for t in resp["trace"]] == ["", "2", "2/2"]


# ---------------------------------------------------------------------------
# mixed：首个可展示子件
# ---------------------------------------------------------------------------


def test_mixed_first_displayable_skips_blob_and_attachment(client):
    msg = mixed(
        [
            blob_part(),
            plain_part("att", disposition="attachment"),
            plain_part("hello"),
        ]
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "3"
    assert resp["text"] == "hello"
    assert rules(resp) == ["mixed.first_displayable", "leaf"]
    assert resp["trace"][0]["candidates"] == ["3"]


def test_mixed_with_nested_alternative(client):
    msg = mixed(
        [alternative([plain_part("p"), html_part("<p>h</p>")]), blob_part()]
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "1/2"
    assert rules(resp) == [
        "mixed.first_displayable",
        "alternative.prefer",
        "leaf",
    ]
    assert [t["chosen"] for t in resp["trace"]] == ["1", "1/2", None]


def test_unknown_multipart_subtype_treated_as_mixed(client):
    # multipart/signed：未特化的子类型按 mixed 处理
    msg = _container(
        "multipart/signed",
        [plain_part("signed text"), blob_part("application/pgp-signature")],
        "SIG",
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "1"
    assert resp["text"] == "signed text"
    top = resp["trace"][0]
    assert top["rule"] == "mixed.first_displayable"
    assert top["content_type"] == "multipart/signed"


# ---------------------------------------------------------------------------
# related：start 定位、重复命中 422
# ---------------------------------------------------------------------------


def test_related_without_start_takes_first_child(client):
    msg = related([html_part("<p>root</p>"), blob_part(cid="img@x")])
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "1"
    assert resp["content_type"] == "text/html"
    assert rules(resp) == ["related.first", "leaf"]
    assert resp["trace"][0]["detail"] == {}


def test_related_start_selects_matching_content_id(client):
    msg = related(
        [
            blob_part("image/png", cid="img@x"),
            html_part("<p>root</p>", cid="root@x"),
        ],
        start="<root@x>",
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "2"
    assert resp["content_type"] == "text/html"
    top = resp["trace"][0]
    assert top["rule"] == "related.start"
    assert top["chosen"] == "2"
    assert top["detail"] == {"start": "<root@x>"}


def test_related_start_unmatched_falls_back_to_first_child(client):
    msg = related(
        [html_part("<p>first</p>"), blob_part(cid="img@x")],
        start="<missing@x>",
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "1"
    top = resp["trace"][0]
    assert top["rule"] == "related.first"
    assert top["detail"] == {"start": "<missing@x>", "start_matched": False}


def test_related_start_duplicate_returns_422(client):
    msg = related(
        [
            html_part("<p>a</p>", cid="dup@x"),
            html_part("<p>b</p>", cid="dup@x"),
        ],
        start="<dup@x>",
    )
    mid = post(client, msg)
    r = client.get(f"/messages/{mid}/display-body")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "AMBIGUOUS_RELATED_START"
    assert err["node_path"] == ""
    assert err["detail"]["start"] == "<dup@x>"
    assert err["detail"]["candidates"] == ["1", "2"]


def test_related_start_duplicate_nested_locates_related_node(client):
    inner = related(
        [
            html_part("<p>a</p>", cid="dup@x"),
            html_part("<p>b</p>", cid="dup@x"),
        ],
        boundary="REL2",
        start="<dup@x>",
    )
    mid = post(client, mixed([inner]))
    r = client.get(f"/messages/{mid}/display-body")
    assert r.status_code == 422
    assert r.json()["error"]["node_path"] == "1"


def test_related_start_angle_brackets_optional(client):
    # start 不带尖括号、Content-ID 带尖括号，规范化后仍匹配
    msg = related(
        [blob_part("image/png", cid="img@x"), html_part("<p>r</p>", cid="root@x")],
        start="root@x",
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "2"
    assert resp["trace"][0]["rule"] == "related.start"


# ---------------------------------------------------------------------------
# message/rfc822 与 attachment 排除
# ---------------------------------------------------------------------------


def test_rfc822_inner_body_is_not_a_candidate(client):
    inner_msg = b"Subject: inner\r\n\r\ninner body"
    msg = mixed([rfc822_part(inner_msg), plain_part("outer")])
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["node_path"] == "2"
    assert resp["text"] == "outer"


def test_only_rfc822_part_returns_404(client):
    inner_msg = b"Subject: inner\r\n\r\ninner body"
    mid = post(client, mixed([rfc822_part(inner_msg)]))
    r = client.get(f"/messages/{mid}/display-body")
    assert r.status_code == 404


def test_root_rfc822_returns_404(client):
    # 整封邮件就是 message/rfc822 容器：内部正文不作候选
    msg = b"Content-Type: message/rfc822\r\n\r\nSubject: x\r\n\r\nbody"
    mid = post(client, msg)
    assert client.get(f"/messages/{mid}/display-body").status_code == 404


def test_attachment_only_message_returns_404(client):
    msg = mixed([plain_part("att", disposition="attachment")])
    mid = post(client, msg)
    assert client.get(f"/messages/{mid}/display-body").status_code == 404


def test_alternative_with_no_displayable_branch_returns_404(client):
    msg = alternative([blob_part(), rfc822_part(b"Subject: x\r\n\r\nb")])
    mid = post(client, msg)
    assert client.get(f"/messages/{mid}/display-body").status_code == 404


# ---------------------------------------------------------------------------
# 响应形状、解码、错误与“不改写记录”
# ---------------------------------------------------------------------------


def test_simple_message_response_shape(client):
    mid = post(client, b"Subject: s\r\n\r\nhello body\r\n")
    created = client.get(f"/messages/{mid}").json()
    resp = body(client, mid)
    assert resp["id"] == mid
    assert resp["fingerprint"] == created["fingerprint"]
    assert resp["node_path"] == ""
    assert resp["content_type"] == "text/plain"
    assert resp["text"] == "hello body\n"
    assert resp["trace"] == [
        {
            "node_path": "",
            "content_type": "text/plain",
            "rule": "leaf",
            "chosen": None,
            "candidates": [],
            "detail": {},
        }
    ]


def test_text_is_transfer_decoded(client):
    html = "<p>你好，世界</p>"
    msg = alternative(
        [plain_part("p"), html_part(html, cte="base64")], boundary="ALT"
    )
    mid = post(client, msg)
    resp = body(client, mid)
    assert resp["content_type"] == "text/html"
    assert resp["text"] == html


def test_message_not_found_404(client):
    assert client.get("/messages/999/display-body").status_code == 404


def test_invalid_prefer_returns_422(client):
    mid = post(client, b"Subject: s\r\n\r\nx\r\n")
    assert client.get(f"/messages/{mid}/display-body?prefer=xml").status_code == 422


def test_query_does_not_rewrite_records(client):
    msg = mixed(
        [alternative([plain_part("p"), html_part("<p>h</p>")]), blob_part()]
    )
    mid = post(client, msg)
    before_messages = client.get("/messages").json()
    before_nodes = client.get(f"/messages/{mid}/nodes").json()
    body(client, mid)
    body(client, mid, prefer="plain")
    assert client.get("/messages").json() == before_messages
    assert client.get(f"/messages/{mid}/nodes").json() == before_nodes
