# Transactional email live verification

GrooveMap uses Resend to deliver password-reset messages. Unit tests cover the
request body sent to Resend, but only an operator can prove that the production
account, DNS, secret, deployed configuration, and recipient path work together.

This procedure changes a real account password and sends a real email. Complete
the preflight locally, then obtain approval before deploying configuration or
requesting the message. Never paste an API key, reset token, or password into a
bead, terminal transcript, screenshot, or committed file.

## Live prerequisites

The operator needs all of the following:

- access to the GrooveMap Resend account;
- a sending domain that Resend reports as verified, including its required SPF
  and DKIM records;
- a Resend API key that is permitted to send from that verified domain;
- the public HTTPS origin of Explore for `APP_BASE_URL`;
- a sender address on the verified domain for `RESEND_SENDER_EMAIL` (the
  production default is `noreply@groovemap.music`);
- an external recipient inbox the operator can inspect, including the raw
  message or link target; and
- a dedicated GrooveMap test user at that address whose password may be
  changed.

The original pre-migration work item named a legacy domain. That is not the
current deployment default. Verify the domain used by `RESEND_SENDER_EMAIL`; do
not add legacy DNS merely to satisfy the old wording.

## Safe preflight

On the deployment host, provision the API key through the existing file-secret
contract. `scripts/create-secrets.sh` creates an empty placeholder when
`RESEND_API_KEY` is absent, so file existence alone is not enough.

```bash
test -s secrets/resend_api_key.txt
test "${APP_BASE_URL#https://}" != "$APP_BASE_URL"
test -n "${RESEND_SENDER_EMAIL:-noreply@groovemap.music}"
```

These checks print no secret. Confirm separately that:

- `APP_BASE_URL` is the externally reachable Explore origin, with no path,
  query, or fragment; it must not be localhost or the internal `API_BASE_URL`;
- the domain part of `RESEND_SENDER_EMAIL` is the domain shown as verified in
  Resend; and
- the production render is valid:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet
```

After a separately approved deployment or API restart, verify only the presence
of the mounted secret; do not print its content:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec api sh -c 'test -n "$APP_BASE_URL" && test -s "$RESEND_API_KEY_FILE"'
```

## Approval-required live test

1. In Resend, confirm the sender domain is still verified and note the status
   without capturing DNS values or the API key.
2. Open the public Explore origin in a private browser window. Use **Forgot
   password?** for the dedicated test user's exact email address.
3. Confirm the external inbox receives one message with subject **Reset your
   GrooveMap password**. Check spam or quarantine before declaring failure.
4. Inspect the raw message or copy the button's link address. It must begin with
   the exact configured `APP_BASE_URL`, followed by `/?reset_token=`. The link
   target must not be a Resend or other click-tracking redirect. Do not record
   the token.
5. Click the link from the mail client. Confirm Explore opens the reset form,
   accepts a new test password, removes the token from the browser URL after
   success, and permits login with the new password.
6. Confirm the old password no longer works. If practical, rotate the dedicated
   test account back to its normal operator-managed credential after recording
   the result.

Because reset requests deliberately return the same response for known and
unknown addresses, a success message in Explore is not delivery evidence. A
received message and a completed reset are both required.

## Record the outcome

Add a note to bead gm-deployment-v7l — the tracked live-verification work item
this runbook was split from — with this non-secret evidence:

```text
Live verification: PASS | FAIL
UTC time:
Explore origin:
Sender domain verified in Resend: yes | no
External inbox received message: yes | no
Raw link used exact Explore origin with no tracking redirect: yes | no
Reset completed and new password login succeeded: yes | no
Configuration correction required: none | follow-up bead <id>
Operator:
```

If a setting must change, make it through the normal reviewed deployment flow
and link the follow-up bead. Do not leave the only record in an untracked host
file or the Resend dashboard.
