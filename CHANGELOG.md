# Changelog

Newest day first. Within a day, the latest change is listed first.

### 2026-11-26

- Send Test Due Card (Settings → Tests): sends a fake due-card reminder with the same Mark used button, to the phone and email alerts. Check Test Result shows whether the button reached the app. The test changes no card, and its link expires after 30 minutes.
- Backup and restore (Settings): download one zip with every card, setting, card picture and the phone server's logins. Restore it on the same or a new server after confirming. The zip contains logins and secrets, so keep it private.
- Restore checks the zip first: only the expected files are accepted, paths that try to leave the data folder are refused, and the database must pass an integrity check. Nothing is changed if a check fails. Restart the phone server after a restore.
- Forms other than a backup restore are still capped at 6 MB.
- Theme preview: choosing a colour, font, shadow, width, corner rounding or light/dark in the Theme menu shows the change at once. Closing the menu without saving puts the saved look back.
- Find Card Names (Card Settings): reads an issuer's card list page and up to 12 card pages on the same site, then lists the card names for you to tick. Names already in the list are skipped.
- Picker thumbnails now load through this server. The page's image rules only allow this server, so the remote pictures were blocked before.
- Headings, navigation, buttons, table headers, menu options and page titles use Proper Case. Field labels, help text and messages stay in sentence case.
- Import: download an Excel template with dropdowns for the issuer, the card (listed for the chosen issuer) and the owner. Excel (.xlsx) and CSV files both import.
- Find pictures reads pages up to 12 MB. Larger pages are searched from the top, and you are told when only part was searched (it used to refuse pages over 3 MB).
- Find pictures shows a progress bar and elapsed time while it works.
- If the search finds no matching pictures, every picture on the page is listed so you can tick the ones you want and choose the card for each.
- Pictures from a page are refused if their address points to a private network.
- Settings page redesigned: sections for reminders, phone alerts (four numbered steps), email alerts, login and tests.
- New Card settings page: due dates, card pictures, and the issuer and card lists moved off Settings.
- Theme options moved to a Theme menu in the top bar, available on every page.
- Phone setup: the phone server address and the app's web address are found automatically and can be changed in Settings. The Unraid template no longer asks for them.
- Phone topics: choose one household topic for every phone, or an own topic per person. You can switch at any time in Settings.
- Settings shows four numbered steps for setting up each phone, with separate guidance for home networks and for Cloudflare Tunnel or Nginx Proxy Manager (https, no port).
- Phone logins can use your own passwords (8 to 64 characters), or a generated one.
- The phone server always sends the iPhone instant-push poll requests to ntfy.sh (the message content stays on your server).
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
