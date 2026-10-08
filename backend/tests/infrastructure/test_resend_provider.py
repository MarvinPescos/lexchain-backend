import resend

from app.infrastructure.email.base import EmailMessage
from app.infrastructure.email.resend_provider import ResendEmailProvider


async def test_sends_params_as_one_dict(monkeypatch):
    """resend.Emails.send(params) — passing **kwargs failed for every email."""
    sent = []
    monkeypatch.setattr(resend.Emails, "send", lambda params: sent.append(params) or {"id": "x"})

    ok = await ResendEmailProvider().send(
        EmailMessage(to="a@example.com", subject="Hi", html="<p>Hi</p>", text="Hi")
    )

    assert ok is True
    assert sent[0]["to"] == ["a@example.com"] and sent[0]["subject"] == "Hi"
    assert sent[0]["from"].startswith("LexChain <")
