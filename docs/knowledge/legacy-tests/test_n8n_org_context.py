from __future__ import annotations

import io
import json
import sys

import pytest


def _build(transport, *, url="https://n8n.example.com/webhook/org-context", token="tok-secret-value"):  # noqa: E501
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows

    return build_n8n_fetch_rows(url=url, token=token, transport=transport, timeout=5.0)


def test_posts_org_uuids_and_returns_rows():
    calls = []
    rows = [
        {"org_uuid": "a", "contract_version": "org-context-v1", "open_ar_band": "0"},
        {"org_uuid": "b", "contract_version": "org-context-v1", "open_ar_band": "1k_5k"},
    ]

    def transport(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return json.dumps(rows)

    fetch_rows = _build(transport)
    result = fetch_rows(["a", "b"])

    assert result == rows
    assert len(calls) == 1
    _, _, payload, _ = calls[0]
    assert payload == {"org_uuids": ["a", "b"]}


def test_token_is_sent_as_header_not_in_url():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return json.dumps([])

    fetch_rows = _build(transport, token="super-secret-token")
    fetch_rows(["a"])

    assert "super-secret-token" not in captured["url"]
    header_values = " ".join(f"{k}:{v}" for k, v in captured["headers"].items())
    assert "super-secret-token" in header_values


def test_non_2xx_raises_org_context_source_error():
    """fp=None. NOTE: on Python 3.13+, ``HTTPError(..., fp=None)``
    auto-substitutes an empty ``io.BytesIO`` for ``fp``, so ``exc.read()``
    actually succeeds here and returns ``b""`` -- this does NOT exercise the
    ``except Exception: raw_body = ""`` fallback in ``n8n_org_context.py``
    (that fallback needs a ``read()`` call that raises, not one that returns
    empty; see ``test_read_failure_on_httperror_falls_back_to_empty_body``
    below for a test that actually reaches it). This test only pins the
    basic "no body" behavior end to end."""
    import urllib.error

    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    def transport(url, headers, payload, timeout):
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert message != "[]"
    assert "500" in message
    assert message.startswith("n8n webhook returned HTTP 500")


def test_read_failure_on_httperror_falls_back_to_empty_body():
    """A ``read()`` that actually raises (rather than an ``fp=None`` that
    Python 3.13+ silently substitutes an empty ``BytesIO`` for) is what
    genuinely drives the ``except Exception: raw_body = ""`` fallback at
    ``n8n_org_context.py`` inside the ``HTTPError`` handler."""
    import urllib.error

    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    class _UnreadableHTTPError(urllib.error.HTTPError):
        def read(self, *args, **kwargs):
            raise OSError("body already consumed")

    def transport(url, headers, payload, timeout):
        raise _UnreadableHTTPError(url, 503, "Service Unavailable", None, None)

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert message.startswith("n8n webhook returned HTTP 503")


def test_non_2xx_with_real_body_scrubs_token():
    """With a real ``fp`` (io.BytesIO), ``exc.read()`` succeeds and the
    read-and-scrub branch actually runs -- the specific leak path this
    module exists to close. A previous version of this test used fp=None,
    which raises AttributeError on read() and silently falls back to the
    no-body branch, never touching this code at all."""
    import urllib.error

    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "leak-me-if-you-can-1234567890"
    body = io.BytesIO(
        f'{{"error": "unauthorized", "hint": "token was {token}"}}'.encode("utf-8")
    )

    def transport(url, headers, payload, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, body)

    fetch_rows = _build(transport, token=token)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert token not in message
    assert "401" in message
    assert "unauthorized" in message


def test_timeout_raises_org_context_source_error():
    import socket

    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    def transport(url, headers, payload, timeout):
        raise socket.timeout("timed out")

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert message.startswith("n8n fetch failed:")
    assert "timed out" in message


def test_unparseable_json_raises():
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    def transport(url, headers, payload, timeout):
        return "not json at all {"

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError):
        fetch_rows(["a"])


def test_non_list_json_body_raises():
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    def transport(url, headers, payload, timeout):
        return json.dumps({"message": "bad request"})

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError):
        fetch_rows(["a"])


def test_error_message_does_not_contain_the_token():
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "leak-me-if-you-can-1234567890"

    def transport(url, headers, payload, timeout):
        raise RuntimeError(f"connection failed while authenticating with {token}")

    fetch_rows = _build(transport, token=token)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert token not in str(excinfo.value)


def test_rows_are_passed_through_unfiltered():
    rows = [{"org_uuid": "a", "some_unrecognized_key": "surprise"}]

    def transport(url, headers, payload, timeout):
        return json.dumps(rows)

    fetch_rows = _build(transport)
    result = fetch_rows(["a"])
    assert result == rows
    assert result[0]["some_unrecognized_key"] == "surprise"


def test_token_with_trailing_newline_is_stripped_not_rejected():
    """The canonical case: a secret pasted into Railway with a trailing
    newline (``token = "abc123def\\n"``). Without a fix, this reaches
    ``http.client.putheader`` unstripped, which raises using the BYTES REPR
    of the value (``b'Bearer abc123def\\n'``) -- the literal token never
    appears in that message for value-based scrubbing to match, so it leaks
    in full. Stripping the token before use removes the trailing newline
    entirely, so the request proceeds normally instead of ever reaching
    that unredactable error path."""
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["headers"] = headers
        return json.dumps([])

    fetch_rows = _build(transport, token="abc123def\n")
    fetch_rows(["a"])
    assert captured["headers"]["Authorization"] == "Bearer abc123def"


def test_url_with_trailing_newline_is_stripped_not_rejected():
    """MINOR 3: the same Railway-paste mistake wave 1 fixed for the token
    (a trailing newline) used to be a hard build refusal for the url,
    because only the token was stripped. Strip the url too, so the same
    operator mistake has the same forgiving outcome on both fields."""
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        return json.dumps([])

    fetch_rows = _build(
        transport, url="https://n8n.example.com/webhook/org-context\n"
    )
    fetch_rows(["a"])
    assert captured["url"] == "https://n8n.example.com/webhook/org-context"


def test_url_with_interior_whitespace_is_still_rejected():
    """Stripping the url (MINOR 3) must only remove leading/trailing
    whitespace -- interior whitespace remains a hard refusal, same as
    before (see ``test_url_with_embedded_space_is_rejected_without_leaking``
    for the un-stripped case)."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError):
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org context",
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )


def test_token_with_embedded_control_character_is_rejected_without_leaking():
    """A control character that ``strip()`` cannot remove because it is
    embedded, not trailing -- this is the case that must be refused
    outright, since it would otherwise reach ``http.client.putheader`` and
    leak via the bytes-repr error message the same way the trailing-newline
    case would."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "abc123\ndef456secretvalue"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token=token,
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "abc123" not in message
    assert "def456secretvalue" not in message
    assert "\n" not in message


def test_url_with_embedded_control_character_is_rejected_without_leaking():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    bad_url = "https://n8n.example.com/webhook/org-context\r\nX-Injected: 1"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=bad_url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "X-Injected" not in message
    assert "\r" not in message and "\n" not in message


def test_empty_token_still_scrubs_environment_secrets(monkeypatch):
    """``scrub(secrets=[...])`` replaces the environment sweep rather than
    adding to it, so passing ``secrets=[token]`` with an empty token used to
    leave the candidate list empty and scrub NOTHING. An env-sourced secret
    (e.g. a Snowflake account identifier echoed back by n8n) must still be
    redacted even when the explicit token candidate is empty.

    ``build_n8n_fetch_rows`` now refuses an empty token at build time (see
    ``test_blank_token_is_refused_at_build_time``), so this drives
    ``_scrub_message`` directly rather than through the public build
    function -- it is exercising the scrub-layering property, not the
    build-time validation."""
    from pathfinder.action_console.n8n_org_context import _scrub_message

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "hcp-secret-account-id-12345")

    message = _scrub_message(
        "bad account: hcp-secret-account-id-12345",
        token="",
        url="https://n8n.example.com/webhook/org-context",
    )
    assert "hcp-secret-account-id-12345" not in message


def test_url_is_included_in_scrub_candidates():
    """The webhook URL is an unguessable capability path; it should not be
    echoed back verbatim in an error message either."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://n8n.example.com/webhook/super-secret-capability-path"

    def transport(u, headers, payload, timeout):
        raise RuntimeError(f"could not reach {url}")

    fetch_rows = _build(transport, url=url)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert url not in str(excinfo.value)
    assert "super-secret-capability-path" not in str(excinfo.value)


def test_url_with_embedded_space_is_rejected_without_leaking():
    """A raw SPACE passes ``_HEADER_ILLEGAL_RE`` (it's legal in a header
    value) but is illegal in an HTTP request target: reusing the header rule
    for URL validation let this through to ``http.client.putrequest``, which
    raises quoting the request target -- i.e. the secret path -- verbatim
    and unscrubbably. Must be caught at build time instead."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    bad_url = "https://n8n.example.com/webhook/cap secret-path-xyz"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=bad_url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "secret-path-xyz" not in message
    assert "cap secret-path-xyz" not in message


def test_url_with_embedded_tab_is_rejected_without_leaking():
    """Same as the space case, but with an embedded TAB. TAB (\\x09) is
    technically legal inside an HTTP *header* field-value, which is why
    ``_HEADER_ILLEGAL_RE`` deliberately allows it -- but it is illegal in a
    request target, so it must be caught by the URL-specific check, not the
    header one."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    bad_url = "https://n8n.example.com/webhook/cap\tsecret-path-xyz"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=bad_url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "secret-path-xyz" not in message
    assert "\t" not in message


def test_url_selector_leak_is_scrubbed_even_when_full_url_does_not_match():
    """The failure mode Important A closes: an error message that quotes
    only the path/selector portion of the URL, not the scheme+host, used to
    survive scrubbing because ``scrub()`` was only given the full URL as a
    candidate and ``str.replace`` on the full URL matches nothing against a
    message containing just the selector."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://n8n.example.com/webhook/cap/super-secret-capability-path"

    def transport(u, headers, payload, timeout):
        # Simulates a low-level error that quotes only the request target,
        # not the full URL -- exactly what http.client's own errors do.
        raise RuntimeError(
            "URL can't contain control characters. "
            "'/webhook/cap/super-secret-capability-path' (found at least ' ')"
        )

    fetch_rows = _build(transport, url=url)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert "super-secret-capability-path" not in message
    assert "/webhook/cap" not in message


def test_none_token_raises_org_context_source_error_not_attribute_error():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError):
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token=None,
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )


def test_none_url_raises_org_context_source_error_not_attribute_error():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError):
        build_n8n_fetch_rows(
            url=None,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )


def test_non_latin1_token_is_rejected_without_leaking():
    """A token containing a character outside Latin-1 passes the
    header-illegal-character check (it isn't a control character) and then
    fails per-request with a cryptic ``UnicodeEncodeError`` when
    ``http.client`` tries to encode the header value. Must be refused up
    front instead."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "tok-secret-☃-value"  # snowman, outside Latin-1
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token=token,
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "tok-secret-" not in message
    assert "☃" not in message


def test_default_transport_wraps_real_urlopen_failure():
    """MINOR C: ``_default_transport`` itself had zero test coverage, which
    is exactly where Important A's leak hid (the real ``urllib`` behavior
    diverges from any hand-rolled fake transport). Drives the actual
    transport built by ``_default_transport()`` -- no injected fake -- with
    a URL that fails DNS resolution, requiring no network access to fail
    fast and deterministically."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    fetch_rows = build_n8n_fetch_rows(
        url="https://this-host-does-not-exist.invalid/webhook/org-context",
        token="tok-secret-value",
        timeout=2.0,
    )
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert message.startswith("n8n fetch failed:")
    assert "tok-secret-value" not in message


def test_oversized_response_body_is_truncated():
    import urllib.error

    from pathfinder.action_console.n8n_org_context import _MAX_ERROR_TEXT_CHARS
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    huge_body = io.BytesIO(b"x" * (_MAX_ERROR_TEXT_CHARS * 5))

    def transport(url, headers, payload, timeout):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", None, huge_body)

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert len(message) < _MAX_ERROR_TEXT_CHARS * 5
    assert "truncated" in message


def test_non_https_url_is_rejected_without_leaking():
    """IMPORTANT 1, scheme half: a plain ``http://`` URL sends the bearer
    token in cleartext (and, with ``http_proxy`` set, to the proxy). There
    was no scheme check at all; refuse anything but ``https`` at build
    time, naming the scheme problem, never the url."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "http://n8n.example.com/webhook/super-secret-capability-path"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "https" in message
    assert url not in message
    assert "super-secret-capability-path" not in message


def test_non_https_scheme_other_than_http_is_also_rejected():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError):
        build_n8n_fetch_rows(
            url="ftp://n8n.example.com/webhook/org-context",
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )


def test_malformed_url_gets_its_own_message_not_a_scheme_complaint():
    """MINOR 4: ``urlsplit`` itself raises ``ValueError`` on a genuinely
    malformed URL (e.g. an unclosed IPv6-literal bracket). That used to be
    swallowed into ``scheme = ""``, so a URL that plainly starts with
    ``https://`` was refused with the misleading "must use the https
    scheme" message. Fail-closed is correct either way; the message must be
    accurate instead. Still names no part of the URL."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://[bad/webhook/abc"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "https scheme" not in message
    assert "malformed" in message
    assert url not in message


def test_redirect_handler_refuses_rather_than_follows():
    """IMPORTANT 1, redirect half. Stands up two local HTTP servers: the
    first replies 302 to the second, echoing the ``Authorization`` header
    it received back in a custom header (so we can tell if it forwarded the
    real token) and the second records whether it ever received a request
    at all. Drives the actual opener built by ``_default_transport`` (not a
    hand-rolled fake), calling it directly rather than through
    ``build_n8n_fetch_rows`` -- the new https-only check means a
    ``127.0.0.1`` HTTP url can no longer reach the public API."""
    import http.server
    import threading

    from pathfinder.action_console.n8n_org_context import _default_transport

    captured_second = {"hit": False}

    class _TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            captured_second["hit"] = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"stolen")

        def log_message(self, *a):
            pass

    target_server = http.server.HTTPServer(("127.0.0.1", 0), _TargetHandler)
    target_thread = threading.Thread(target=target_server.serve_forever)
    target_thread.start()
    target_port = target_server.server_address[1]

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/stolen")
            self.end_headers()

        def log_message(self, *a):
            pass

    redirect_server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    redirect_thread = threading.Thread(target=redirect_server.serve_forever)
    redirect_thread.start()
    redirect_port = redirect_server.server_address[1]

    try:
        import urllib.error

        transport = _default_transport()
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            transport(
                f"http://127.0.0.1:{redirect_port}/webhook/cap-secret",
                {"Authorization": "Bearer TOKEN-SUPER-SECRET"},
                {"org_uuids": ["a"]},
                5.0,
            )
        assert excinfo.value.code == 302
        assert captured_second["hit"] is False
    finally:
        target_server.shutdown()
        target_thread.join()
        redirect_server.shutdown()
        redirect_thread.join()


def test_non_str_token_raises_org_context_source_error_naming_type():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token=12345678,
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    assert "int" in str(excinfo.value)


def test_non_str_url_raises_org_context_source_error_naming_type():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=12345678,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    assert "int" in str(excinfo.value)


def test_bytes_token_and_url_are_rejected_naming_type():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token=b"tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    assert "bytes" in str(excinfo.value)

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=b"https://n8n.example.com/webhook/org-context",
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    assert "bytes" in str(excinfo.value)


def test_non_ascii_url_is_rejected_at_build_time():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://n8n.example.com/webhook/café-secret"
    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url=url,
            token="tok-secret-value",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "caf" not in message
    assert "secret" not in message


def test_short_url_selector_is_not_scrubbed_as_explicit_candidate():
    """IMPORTANT 2: ``_url_selector`` returns ``"/"`` for a bare base URL
    with no webhook path, and previously that short string was passed as an
    explicit scrub candidate, so ``scrub()`` (which applies no minimum
    length to explicit candidates) replaced every ``/`` in the message --
    shredding the one diagnostic an operator has for the most likely
    misconfiguration: a base URL pasted without its webhook path."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://n8n.example.com/"

    def transport(u, headers, payload, timeout):
        raise RuntimeError("connection refused talking to n8n/example/host")

    fetch_rows = _build(transport, url=url)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    # The message must survive intact -- no `/` shredded into `***`.
    assert "connection refused talking to n8n/example/host" in message


def test_one_char_token_is_refused_at_build_time():
    """IMPORTANT 1: a token this short used to be scrubbed by NOTHING --
    the explicit-candidate length floor (introduced to stop a short
    ``_url_selector`` from shredding messages) was wrongly applied to the
    token too, and the environment sweep's own floor (8) doesn't cover it
    either. Fixed at the source: refuse it at build time instead of relying
    on scrubbing a short token after the fact."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token="a",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "must be at least" in message
    assert "8" in message  # states the length requirement


def test_four_char_token_is_refused_at_build_time():
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token="s3cr",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    message = str(excinfo.value)
    assert "s3cr" not in message
    assert "8" in message


def test_token_at_floor_length_is_accepted():
    """A token exactly at ``_MIN_TOKEN_LEN`` (8, matching
    ``secret_scrub._MIN_SECRET_LEN``) must build successfully -- the floor
    refuses anything SHORTER than it, not tokens at it."""
    from pathfinder.action_console.n8n_org_context import _MIN_TOKEN_LEN

    token = "s3cretzz"
    assert len(token) == _MIN_TOKEN_LEN

    def transport(url, headers, payload, timeout):
        return json.dumps([])

    fetch_rows = _build(transport, token=token)
    assert fetch_rows(["a"]) == []


def test_min_token_len_matches_secret_scrub_floor():
    """Pins the invariant ``_MIN_TOKEN_LEN == secret_scrub._MIN_SECRET_LEN``,
    documented only in a comment until now. A buildable token is, by
    construction, at or above ``_MIN_TOKEN_LEN``; every redaction test above
    therefore exercises a token that already clears the explicit-scrub
    candidate path too and cannot detect the two constants drifting apart.
    If ``_MIN_TOKEN_LEN`` ever drops below ``secret_scrub._MIN_SECRET_LEN``,
    a token in that gap would be protected only by the explicit scrub
    candidates in ``_scrub_message`` -- not by the environment sweep -- with
    nothing else in the suite left to catch the regression.
    """
    from pathfinder.action_console import secret_scrub
    from pathfinder.action_console.n8n_org_context import _MIN_TOKEN_LEN

    assert _MIN_TOKEN_LEN == secret_scrub._MIN_SECRET_LEN


def test_min_length_token_is_redacted_from_error_message():
    """IMPORTANT: pins the fix a prior wave regressed. ``_MIN_TOKEN_LEN`` now
    equals ``secret_scrub._MIN_SECRET_LEN`` (8), so a token this short is
    right at the environment sweep's own floor -- but this test's token is
    never an actual environment variable, so the env sweep never sees it
    regardless of length. It is caught ONLY by the un-floored explicit scrub
    path in ``_scrub_message`` (token/url are passed as explicit candidates
    with no minimum length). A previous wave wrongly applied
    ``_MIN_EXPLICIT_SCRUB_LEN`` to the token too, which floor-gated it and
    let a short token leak through unredacted. This must fail if that
    regression ever reappears."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "s3cretzz"
    assert len(token) == 8

    def transport(u, headers, payload, timeout):
        raise RuntimeError(f"auth failed for token {token}")

    fetch_rows = _build(transport, token=token)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert token not in str(excinfo.value)


def test_nine_char_token_is_redacted_from_error_message():
    """Same regression window as the floor-length case above, one character
    wider."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "s3cretzz1"
    assert len(token) == 9

    def transport(u, headers, payload, timeout):
        raise RuntimeError(f"auth failed for token {token}")

    fetch_rows = _build(transport, token=token)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert token not in str(excinfo.value)


def test_blank_token_is_refused_at_build_time():
    """MINOR 2: ``token=""`` used to be accepted, sending a live request
    with an empty ``Authorization: Bearer `` header. Refuse it fail-closed,
    same as a too-short token (blank strips to length 0)."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    with pytest.raises(OrgContextSourceError) as excinfo:
        build_n8n_fetch_rows(
            url="https://n8n.example.com/webhook/org-context",
            token="",
            transport=lambda *a: json.dumps([]),
            timeout=5.0,
        )
    assert "8" in str(excinfo.value)


def test_whitespace_only_token_is_refused_at_build_time():
    """MINOR 2: whitespace-only tokens (``"   "``, ``"\\n"``) strip to an
    empty string and must be refused the same way a blank token is."""
    from pathfinder.action_console.n8n_org_context import build_n8n_fetch_rows
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    for bad_token in ("   ", "\n"):
        with pytest.raises(OrgContextSourceError) as excinfo:
            build_n8n_fetch_rows(
                url="https://n8n.example.com/webhook/org-context",
                token=bad_token,
                transport=lambda *a: json.dumps([]),
                timeout=5.0,
            )
        assert "8" in str(excinfo.value)


def test_genuinely_secret_length_token_is_still_redacted():
    """The length floor must not weaken real-secret redaction -- only
    short, non-secret-length explicit candidates are skipped."""
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    token = "a-genuinely-long-secret-token-value"

    def transport(u, headers, payload, timeout):
        raise RuntimeError(f"auth failed for token {token}")

    fetch_rows = _build(transport, token=token)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert token not in str(excinfo.value)


def test_genuinely_secret_length_selector_is_still_redacted():
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    url = "https://n8n.example.com/webhook/super-secret-capability-path"

    def transport(u, headers, payload, timeout):
        raise RuntimeError(
            "failed calling /webhook/super-secret-capability-path upstream"
        )

    fetch_rows = _build(transport, url=url)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    assert "super-secret-capability-path" not in str(excinfo.value)


def test_oversized_httperror_body_is_a_loud_error_not_silent_truncation():
    """MINOR 5, error path: ``exc.read()`` used to pull the entire body
    into memory before ``_truncate`` ever ran. A body that hits the cap
    must raise a clear, loud error naming the size problem, not silently
    truncate into a confusing downstream message."""
    import urllib.error

    from pathfinder.action_console.n8n_org_context import _MAX_RESPONSE_BYTES
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    huge_body = io.BytesIO(b"x" * (_MAX_RESPONSE_BYTES + 1))

    def transport(url, headers, payload, timeout):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", None, huge_body)

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert "502" in message
    assert str(_MAX_RESPONSE_BYTES) in message


def test_httperror_body_survives_a_no_argument_read():
    """MINOR 5: some ``HTTPError.fp`` shapes expose a ``read()`` that
    accepts no argument at all. ``_read_capped`` used to call
    ``read(cap + 1)`` unconditionally, which raised ``TypeError`` on such a
    shape; the ``HTTPError`` handler in ``fetch_rows`` treats ANY exception
    from that read as "no body", so the diagnostic body was silently lost.
    It must fall back to a bare ``read()`` and keep the body instead."""
    import urllib.error

    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    class _NoArgReadFp:
        def __init__(self, data: bytes):
            self._data = data

        def read(self):  # deliberately takes no argument
            return self._data

        def close(self):
            pass

    body = _NoArgReadFp(b'{"error": "unauthorized"}')

    def transport(url, headers, payload, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, body)

    fetch_rows = _build(transport)
    with pytest.raises(OrgContextSourceError) as excinfo:
        fetch_rows(["a"])
    message = str(excinfo.value)
    assert "unauthorized" in message


def test_oversized_success_body_is_never_fully_buffered():
    """MINOR 5, success path: ``resp.read()`` inside ``_default_transport``
    used to pull the entire body into memory. Drives the real transport
    (not an injected fake) against a local server streaming a body past the
    cap, and confirms it raises loudly rather than returning a truncated
    body for ``json.loads`` to choke on silently."""
    import http.server
    import threading

    from pathfinder.action_console.n8n_org_context import (
        _MAX_RESPONSE_BYTES,
        _default_transport,
    )
    from pathfinder.action_console.snowflake_org_context import OrgContextSourceError

    chunk = b"[" + b"1," * 1000

    class _HugeBodyHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            written = 0
            target = _MAX_RESPONSE_BYTES + len(chunk) * 2
            while written < target:
                self.wfile.write(chunk)
                written += len(chunk)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _HugeBodyHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    port = server.server_address[1]

    try:
        transport = _default_transport()
        with pytest.raises(OrgContextSourceError) as excinfo:
            transport(
                f"http://127.0.0.1:{port}/webhook/org-context",
                {"Authorization": "Bearer tok"},
                {"org_uuids": ["a"]},
                5.0,
            )
        assert str(_MAX_RESPONSE_BYTES) in str(excinfo.value)
    finally:
        server.shutdown()
        thread.join()


def test_no_snowflake_module_is_ever_imported():
    """Project constraint: no ``snowflake`` connector import may enter
    Pathfinder. Previously only verified by hand; this pins it permanently
    by making any such import fail loudly during the test."""

    class _ForbidSnowflakeFinder:
        def find_spec(self, name, path=None, target=None):
            if name == "snowflake" or name.startswith("snowflake."):
                raise ImportError(
                    f"test guard: import of {name!r} is forbidden in Pathfinder"
                )
            return None

    module_names = (
        "pathfinder.action_console.n8n_org_context",
        "pathfinder.action_console.snowflake_org_context",
        "pathfinder.action_console.org_context_contract",
        "pathfinder.action_console.secret_scrub",
        "pathfinder.action_console.models",
    )
    # Drop any cached copies first so import machinery -- and therefore the
    # finder above -- actually runs for these modules, rather than trivially
    # "succeeding" by returning an already-imported module from cache.
    saved = {name: sys.modules.pop(name, None) for name in module_names}

    finder = _ForbidSnowflakeFinder()
    sys.meta_path.insert(0, finder)
    try:
        import importlib

        for module_name in module_names:
            importlib.import_module(module_name)
    finally:
        sys.meta_path.remove(finder)
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
