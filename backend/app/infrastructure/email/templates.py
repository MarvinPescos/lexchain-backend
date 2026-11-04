def build_invitation_email(role: str, magic_link: str) -> tuple[str, str]:
    """Returns (html, text) for an invitation email."""

    role_label = "lawyer" if role == "lawyer" else role
    expiry_note = "This link expires in 7 days."

    text = (
        f"You've been invited to join LexChain as a {role_label}.\n\n"
        f"Click the link below to set up your account:\n"
        f"{magic_link}\n\n"
        f"{expiry_note}"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
<table width="100%%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 0;">
  <tr>
    <td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">

        <!-- Header -->
        <tr>
          <td style="padding:32px 40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <h1 style="margin:0;font-size:24px;font-weight:700;color:#111827;">LexChain</h1>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:0 40px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.6;color:#374151;">
            <p style="margin:0 0 16px;">You've been invited to join <strong>LexChain</strong> as a <strong>{role_label}</strong>.</p>
            <p style="margin:0 0 16px;">As a {role_label}, you can upload legal documents, record them on-chain, and invite witnesses.</p>
          </td>
        </tr>

        <!-- Button -->
        <tr>
          <td align="center" style="padding:0 40px 24px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="background-color:#111827;border-radius:8px;">
                  <a href="{magic_link}" target="_blank" style="display:block;padding:14px 36px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;white-space:nowrap;">
                    Accept Invitation
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="padding:0 40px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.5;color:#9ca3af;">
            <p style="margin:0 0 8px;">{expiry_note}</p>
            <p style="margin:0;">If the button above doesn't work, copy and paste this link:</p>
            <p style="margin:4px 0 0;word-break:break-all;color:#6b7280;">{magic_link}</p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html, text


def build_upgrade_email(role: str) -> tuple[str, str]:
    """Returns (html, text) for an account upgrade notification."""

    role_label = "lawyer" if role == "lawyer" else role

    text = (
        f"Your LexChain account has been upgraded to {role_label}.\n\n"
        f"You now have the ability to upload legal documents, record them on-chain, and invite witnesses.\n\n"
        f"Log in to your account to get started."
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
<table width="100%%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 0;">
  <tr>
    <td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">

        <tr>
          <td style="padding:32px 40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <h1 style="margin:0;font-size:24px;font-weight:700;color:#111827;">LexChain</h1>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.6;color:#374151;">
            <p style="margin:0 0 16px;">Your account has been upgraded to <strong>{role_label}</strong>.</p>
            <p style="margin:0 0 16px;">You can now upload legal documents, record them on-chain, and invite witnesses to collaborate.</p>
            <p style="margin:0;">Log in to your account to get started.</p>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.5;color:#9ca3af;">
            <p style="margin:0;">This is an automated notification from LexChain.</p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html, text


def build_draft_activity_email(
    title: str, body: str, document_name: str, draft_url: str | None
) -> tuple[str, str]:
    """Returns (html, text) for activity on a working draft in Google Docs.

    `body` carries comment text written by people, so it is escaped.
    """
    from html import escape

    text = f"{title}\n\n{body}\n\n"
    if draft_url:
        text += f"Open the draft: {draft_url}\n"

    button = (
        f"""
        <tr>
          <td align="center" style="padding:0 40px 24px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="background-color:#111827;border-radius:8px;">
                  <a href="{escape(draft_url, quote=True)}" target="_blank" style="display:block;padding:14px 36px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;white-space:nowrap;">
                    Open the draft
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""
        if draft_url
        else ""
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 0;">
  <tr>
    <td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">

        <tr>
          <td style="padding:32px 40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <h1 style="margin:0;font-size:24px;font-weight:700;color:#111827;">LexChain</h1>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.6;color:#374151;">
            <p style="margin:0 0 8px;font-weight:600;color:#111827;">{escape(title)}</p>
            <p style="margin:0 0 16px;padding:12px 16px;background-color:#f9fafb;border-left:3px solid #d1d5db;white-space:pre-wrap;">{escape(body)}</p>
            <p style="margin:0;font-size:14px;color:#6b7280;">On the working draft of <strong>{escape(document_name)}</strong>.</p>
          </td>
        </tr>
{button}
        <tr>
          <td style="padding:0 40px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.5;color:#9ca3af;">
            <p style="margin:0;">You are receiving this because you are on this document in LexChain. Google Docs may also email you about the same activity.</p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html, text


def build_ecopy_email(document_name: str, lawyer_name: str) -> tuple[str, str]:
    """Returns (html, text) for an approved e-copy, sent with the PDF attached.

    No link to the file: the copy travels as the attachment, and the app
    serves it again only to a signed-in client who still has access.
    """
    from html import escape

    text = (
        f"{lawyer_name} approved your request for an e-copy of "
        f"'{document_name}'.\n\n"
        "The signed copy is attached. You can also download it in LexChain "
        "while you have access to the document.\n"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 0;">
  <tr>
    <td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">

        <tr>
          <td style="padding:32px 40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <h1 style="margin:0;font-size:24px;font-weight:700;color:#111827;">LexChain</h1>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.6;color:#374151;">
            <p style="margin:0 0 16px;"><strong>{escape(lawyer_name)}</strong> approved your request for an e-copy of <strong>{escape(document_name)}</strong>.</p>
            <p style="margin:0;">The signed copy is attached. You can also download it in LexChain while you have access to the document.</p>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.5;color:#9ca3af;">
            <p style="margin:0;">You are receiving this because you requested this copy in LexChain.</p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html, text


def build_document_invite_email(
    owner_name: str, document_name: str, magic_link: str
) -> tuple[str, str]:
    """Returns (html, text) for a document invitation to someone with no account.

    Signing up with the invited address is what claims it, so the link only
    has to bring them to sign-up.
    """
    from html import escape

    text = (
        f"{owner_name} invited you to '{document_name}' on LexChain.\n\n"
        "Create your account with this email address and the document will be "
        "added for you:\n"
        f"{magic_link}\n\n"
        "This invitation expires in 7 days.\n"
    )

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:40px 0;">
  <tr>
    <td align="center">
      <table width="480" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;">

        <tr>
          <td style="padding:32px 40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
            <h1 style="margin:0;font-size:24px;font-weight:700;color:#111827;">LexChain</h1>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:16px;line-height:1.6;color:#374151;">
            <p style="margin:0 0 16px;"><strong>{escape(owner_name)}</strong> invited you to <strong>{escape(document_name)}</strong>.</p>
            <p style="margin:0;">Create your account with this email address and the document will be added for you.</p>
          </td>
        </tr>

        <tr>
          <td align="center" style="padding:0 40px 24px;">
            <table cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="background-color:#111827;border-radius:8px;">
                  <a href="{escape(magic_link, quote=True)}" target="_blank" style="display:block;padding:14px 36px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;white-space:nowrap;">
                    Create my account
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:0 40px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:13px;line-height:1.5;color:#9ca3af;">
            <p style="margin:0;">This invitation expires in 7 days. If you weren't expecting it, you can ignore this email.</p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html, text
