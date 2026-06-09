"""The diagnostics bundle is meant to be safe to paste into a public GitHub issue, so the
redaction is a privacy GUARANTEE — these lock it against regressions. Adversarial inputs go
well beyond what Mate actually logs (it only ever logs the VIN); we scrub them anyway."""
import diagnostics as D


def test_mask_vin():
    assert D.mask_vin("LFZA5AE24SE008234") == "LFZ…8234"
    assert D.mask_vin(None) == "—"
    assert D.mask_vin("short") == "…"


def test_redact_vin_even_inside_a_log_line():
    out = D._redact("2026-06-09 [INFO] poller: Polling VIN WBADT43452G296000 model B10")
    assert "WBADT43452G296000" not in out
    assert "WBA…6000" in out


def test_redact_credentials():
    out = D._redact("auth password=hunter2 token=ghp_FAKE1234567890 pin=4321 secret=abcd api_key=zzz")
    for leak in ("hunter2", "ghp_FAKE1234567890", "4321", "abcd", "zzz"):
        assert leak not in out, leak
    assert out.count("***") >= 5


def test_redact_authorization_header_and_bearer():
    # The token AFTER "Authorization: Bearer" must not survive (the gap the live test caught).
    out = D._redact("login failed Authorization: Bearer abc.def.ghi trailing")
    assert "abc.def.ghi" not in out
    # A standalone bearer token too.
    out2 = D._redact("sent Bearer xyz.123.tok here")
    assert "xyz.123.tok" not in out2


def test_redact_email():
    out = D._redact("login user=test@example.com failed")
    assert "test@example.com" not in out
    assert "***@***" in out


def test_redact_json_style_credentials():
    # The gap the adversarial review caught: a JSON/config dump in a log line.
    for blob, leak in [
        ('{"password": "secret123"}', "secret123"),
        ("{'token': 'abc123xyz'}", "abc123xyz"),
        ('config secret: "hunter2"', "hunter2"),
    ]:
        out = D._redact(blob)
        assert leak not in out, blob


def test_redact_compound_key_names():
    # private_key / access_token etc. — underscore used to defeat the \bkey\b boundary.
    for blob, leak in [
        ("private_key=AKIA_DEADBEEF", "AKIA_DEADBEEF"),
        ("access_token: tok_9988", "tok_9988"),
        ("api_key=zzz999", "zzz999"),
    ]:
        out = D._redact(blob)
        assert leak not in out, blob


def test_redact_quoted_value_with_spaces():
    out = D._redact('pass="my long secret phrase" done')
    assert "my long secret phrase" not in out
    assert "done" in out          # we stop at the closing quote, not the whole line


def test_redact_device_id_and_masked_email():
    # Real leak Silvio's own download exposed: the API auth line logs device_id in the clear,
    # and the email keeps its domain (the app pre-masks only the local part).
    line = ("authenticating as account: sil***@dxc.com | "
            "device_id: ee059adfc09342859867423cccf53afc")
    out = D._redact(line)
    assert "ee059adfc09342859867423cccf53afc" not in out
    assert "dxc.com" not in out
    assert "***@***" in out


def test_redact_does_not_overmask_innocent_words():
    # "monkey"/"passenger" must survive — they only *contain* a secret word, not equal one.
    line = "the monkey passed the passenger a key-shaped cookie"
    assert D._redact(line) == line


def test_redact_keeps_normal_lines_intact():
    line = "2026-06-09 [INFO] leapmotor_mate: SOC 92.3% | Range 400 km | State: parked_active"
    assert D._redact(line) == line
