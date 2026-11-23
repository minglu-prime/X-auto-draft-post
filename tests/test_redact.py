from xdraft.redact import redact


def test_redacts_common_secrets():
    text = (
        "key sk-ant-api03-abcdefghijklmnop and sk-proj-abcdefghijklmnopqrstuvwx "
        "gh ghp_abcdefghijklmnopqrstuvwxyz0123 aws AKIAABCDEFGHIJKLMNOP "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz "
        "OPENAI_API_KEY=supersecretvalue "
        '{"password": "hunter2hunter2"} '
        "postgres://user:pw@db.example.com/x "
        "mail me at someone@example.com "
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    )
    out = redact(text)
    for secret in ["sk-ant-api03", "sk-proj-abc", "ghp_abc", "AKIAABCD", "abcdefghijklmnopqrstuvwxyz ",
                   "supersecretvalue", "hunter2", "user:pw", "someone@example.com", "eyJhbGci"]:
        assert secret not in out, secret
    assert "OPENAI_API_KEY=[REDACTED]" in out


def test_redacts_home_paths_and_private_keys():
    out = redact("/home/alice/code/x and /Users/bob/y C:\\Users\\carol\\z\n"
                 "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    assert "alice" not in out and "bob" not in out and "carol" not in out
    assert "~/code/x" in out
    assert "[REDACTED_PRIVATE_KEY]" in out


def test_leaves_normal_text_and_extra_patterns():
    text = "Use the desk-lamp-controller tokenizer with 3 tokens"
    assert redact(text) == text
    assert redact("project ACME-42 rocks", ["ACME-\\d+"]) == "project [REDACTED] rocks"
