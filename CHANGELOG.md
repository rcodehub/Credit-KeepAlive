# Changelog

Newest day first. Within a day, the latest change is listed first.

### 2026-11-26

- Security: changing the password signs out every other browser; pages with card details or passwords are no longer cached; the login redirect refuses backslash and control-character paths.
- Appearance settings: 10 colour themes, light, dark or follow device, corner rounding, font, shadows and page width. Saved in the database.
- Test messages now check that the phone server stored the message, and say what to check on the phone if nothing arrives.
- README: explains the test buttons and what a successful test does and doesn't prove.

### 2026-10-29

- Fixed first start: the ntfy server now starts, creates its auth file, then the logins are set up before it restarts. Earlier builds tried this too early and failed with "auth-file does not exist".
- Version number shown in the footer of every page, set at build time (date, run number and commit).
- deploy.bat now copies ntfy_accounts.py and fixes the template copy message.
- Phone server logins: admin, phone and publisher accounts. Anonymous access is off. Settings shows the phone and admin passwords and can make new ones.
- Unraid template: "Behind a reverse proxy" setting (TRUST_PROXY) added under advanced settings.
- Security: CSRF tokens on every form, security headers, login lockout per address (5 failures in 5 minutes), Mark used rate limit (10 bad tokens in 10 minutes), Secure cookie over HTTPS.
