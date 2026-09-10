from support_agent.redaction import contains_placeholder, redact_text


def test_card_phone_email_redacted_but_sender_kept():
    r = redact_text(
        "card 4111 1111 1111 1111, call (415) 555-0134, cc dev@corp.example, me me@x.example",
        keep_email="me@x.example",
    )
    assert "4111" not in r.text and "555-0134" not in r.text and "dev@corp.example" not in r.text
    assert "me@x.example" in r.text
    assert set(r.placeholders.values()) == {"credit_card", "phone", "email"}
    assert contains_placeholder(r.text)


def test_order_and_tracking_numbers_survive_luhn_check():
    r = redact_text("order NW-10077 tracking 9400111899223033005411")
    assert "9400111899223033005411" in r.text  # fails Luhn -> not a card
    assert "NW-10077" in r.text


def test_password_line_redacted():
    r = redact_text("my password is hunter2 please")
    assert "hunter2" not in r.text and "[[PASSWORD_1]]" in r.text
