# LexChain — front-end hand-off: inviting people without an account

For the front-end agent. Backend contract as built on `feat/document-lifecycle`
(2026-11-03). `BASE_URL` is `https://api.lexchain.me`; the live schema is at
`{BASE_URL}/openapi.json`. Extends *Sharing (lawyer) and invitations
(client)* in `frontend-handoff.md`; everything there still holds.

## What changed

A lawyer can now add a party by **any email**, not only an existing LexChain
user's. If the email has no account:

1. The backend saves a pending invitation and emails a sign-up link.
2. The invitee creates an account **with that same email**.
3. On their first sign-in after confirming the email, they are added to the
   document automatically: already **accepted**, no accept step. If the
   document has a Google Docs draft, they get access to it too.

The invitation expires **7 days** after it was sent. The lawyer can resend
it, which emails the link again and restarts the 7 days.

People who already have an account are unchanged: they get a pending
invitation in the app and accept or decline it as before.

## Lawyer screens

### Add party — `POST /documents/{id}/parties`

Body unchanged: `{email, role}`. Don't require the email to come from
`GET /users/search` any more; a free-typed address is fine.

| Response | Meaning | UI |
|---|---|---|
| 201, `user_id` set | Existing user invited (as before) | "Invitation sent" |
| 201, `user_id: null` | No account; email invitation sent | "Invitation emailed to {email}. They'll be added when they create an account." |
| 400 | `role` is `signer`/`editor` for an address with no account | Only `viewer` can be invited by email. Hide or disable the other roles once you know the address has no account, or show `message` |
| 409 | That address already has a pending, unexpired invitation on this document | Show `message`; offer **Resend** instead |

Adding an address whose invitation **expired** works: it reopens the same
invitation (same `id`), sends a new email, and restarts the 7 days.

### Party list — `GET /documents/{id}/parties`

Each party gained `expires_at`; `user_id` may be `null`:

```json
{
  "id": "8f0c…",              // party id: use this for resend/remove when user_id is null
  "user_id": null,            // null until the invitee signs up and signs in
  "email": "newcomer@example.com",
  "f_name": "", "l_name": "", // empty until they have an account
  "role": "viewer",
  "status": "pending",        // pending | accepted | declined | expired
  "responded_at": null,
  "expires_at": "2026-11-10T08:12:00Z", // email invitations only, else null
  "created_at": "2026-11-03T08:12:00Z"
}
```

Show a row with `user_id: null` as an invited email, e.g. the address with a
"Waiting for sign-up" badge and "expires {relative expires_at}". `status:
"expired"` only ever appears on these rows: show "Invitation expired" with
**Resend** as the main action.

| Row | Actions |
|---|---|
| `user_id: null`, `pending` | Resend, Remove |
| `user_id: null`, `expired` | Resend, Remove |
| `user_id` set | Remove (as before) |

### Resend — `POST /documents/{id}/parties/{party_id}/resend` (new)

No body. Use the party **`id`**. Only for rows with `user_id: null`; any
other id answers 404. Returns the updated party (`status: "pending"`, new
`expires_at`). Toast: "Invitation resent to {email}".

| Status | Meaning |
|---|---|
| 200 | Resent |
| 404 | Not an email invitation on this document, or not yours |

### Remove — `DELETE /documents/{id}/parties/{party_user_id}`

Unchanged for real users (pass `user_id`). For an email invitation pass the
party **`id`** in the same slot; the response message is "Invitation
removed". The sign-up link then no longer adds them.

## Sign-up from the email link

The email's button opens the web register page:

```
https://lexchain.me/register?email=newcomer%40example.com
```

(Backend setting `SIGN_UP_URL`, `https://lexchain.me/register` by default.)

It's an https page, not a `lexchain://` deep link, because Gmail and most mail
clients strip custom-scheme links so they can't be clicked. If the mobile app
should open instead, make this URL an App Link / Universal Link for the app.

- The sign-up page must **pre-fill the email** from `?email=` (URL-decoded).
  Keep the field editable, but the document is only added if they sign up
  with that exact address (case doesn't matter).
- Admin invitations link to the same page with `?token=` instead. Handle
  both; either, both, or neither may be present.
- Sign-up itself is unchanged (`POST /auth/signup`). After the user confirms
  their email and signs in (`POST /auth/signin`), the document is already in
  `GET /documents`. No extra call is needed.
- Suggested copy after sign-up: "Confirm your email, then sign in. The
  document you were invited to will be waiting for you."

## Edge cases

- **Signed up but never confirmed the email:** nothing is added until they
  confirm and sign in.
- **Invitation expired before they signed in:** they get an account but no
  document. The lawyer sees `expired` and can resend. Once the new link is
  sent, their next sign-in adds them.
- **Invited to several documents:** all of them are added at the same
  sign-in.
- **Notifications:** the invitee gets the email only. There is no in-app
  notification, because they have no account yet.
