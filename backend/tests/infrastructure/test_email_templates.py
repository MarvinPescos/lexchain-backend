from app.infrastructure.email import build_draft_activity_email


def test_comment_text_is_escaped():
    html, text = build_draft_activity_email(
        "New comment on 'Deed'",
        'cruz: <script>alert("x")</script> & 500,000',
        "Deed <A&B>",
        "https://docs.google.com/document/d/f1/edit",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp; 500,000" in html
    assert "Deed &lt;A&amp;B&gt;" in html
    assert 'href="https://docs.google.com/document/d/f1/edit"' in html
    assert "<script>alert" in text  # plain text is not HTML


def test_no_button_without_a_draft_link():
    html, _ = build_draft_activity_email("t", "b", "Deed", None)
    assert "Open the draft" not in html
