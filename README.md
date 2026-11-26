# Credit-KeepAlive

Keeps a family's credit cards in use so banks don't close them for inactivity. Cards are grouped by owner,
and each person gets a reminder on their own phone or by email when a card is due.

**Beta.** Under active development and updated often.

Created entirely by AI (Claude). Tested, but not in every situation. Do your own due diligence before
exposing it to the internet.

## Install on Unraid

Search for **Credit-KeepAlive** in Community Applications and install it. The template is also in this
repository at `unraid/my-Credit-Keepalive.xml`.

| Setting | Required? | What to enter |
|---|---|---|
| Phone server address | Yes, for phone alerts | `http://YOUR-SERVER-IP:2586`, or your proxy hostname |
| App web address | Yes, for Mark used buttons | `http://YOUR-SERVER-IP:8686`, or your proxy hostname |
| Login password | No | Leave blank to choose it on your first visit |
| Time zone | No | Defaults to `America/New_York` |
| Behind a reverse proxy (`TRUST_PROXY`) | No | Leave blank unless a reverse proxy or Cloudflare Tunnel is in front of the app |

Keep the default ports (`8686` web, `2586` phone) and app data folder unless you need to change them.
Your data lives in `/mnt/user/appdata/credit-keepalive` (`cards.db` and `card-images`). Back up that folder.

**First run:** open `http://YOUR-SERVER-IP:8686`, set a password if you didn't in the template, then check
that **Settings → Phone server status** says **ready**.

**Behind a reverse proxy or Cloudflare Tunnel:** enter your hostname for both addresses (for example
`https://ntfy.example.com`), point the phone address at port `2586` and the web address at `8686`, turn on
WebSocket support, use HTTPS, and set `TRUST_PROXY` to `1`.

## Set up your phone

1. Install the free **ntfy** app.
2. Add the server using the **Phone server address**.
3. Log in with username `phone` and the password shown in **Settings**.
4. Subscribe to topics: the default topic in **Settings**, and each person's topic under **People → Alerts**.

Test it with **Settings → Send test to default alerts**. "Test sent" means the phone server accepted the
message. If nothing arrives within a minute, check the phone is logged in and subscribed to the topic.
Away from home, the phone needs a VPN such as Tailscale to reach the server.

The phone server requires a login for everything. The container creates three logins on each start:
`phone` (for phones, read-only), `admin` (for the ntfy web page) and `publisher` (used by the app).
**Settings** shows the `phone` and `admin` passwords and can make new ones. After a new phone password,
log in again on each phone.

## Using the app

- **Cards** show the issuer, last 4 digits, owner and progress through the current cycle. Add a card from the dropdowns.
- **Mark used today** resets the clock. **Other date** back-dates it. **Use next** shows the most urgent card.
- **Card pictures** are optional (PNG, JPG, GIF or WEBP, up to 4 MB). Each card uses a generated picture until you add one.
- **People** controls each person's reminders. Turn them off without deleting anyone.
- **History** lists every logged use. Undo removes an entry.
- **Reminders** go out once a day at the hour set in Settings. Overdue cards repeat.
- **Import** a CSV with the columns `Issuer,Card,Last4,Owner`. You review everything before it's saved.
  Full card numbers are rejected.
- **Email** works through Apprise. Add an address in Settings, for example
  `mailto://yourname:app-password@gmail.com?to=you@example.com`.
- **Appearance** (Settings): colour theme, light or dark, corner rounding, font, shadows and page width.

## Security

- Keep the app on your home network, or behind a reverse proxy with HTTPS.
- Five wrong passwords from one address lock the login for five minutes.
- Mark used links in notifications work without a login. Each carries a secret token and can only mark a card as used.
- Only the last 4 digits of each card are stored. Alert addresses and passwords are stored in `cards.db`,
  so protect the appdata folder.

## Run without Unraid

Put the files from this repository in one folder, change the two addresses in `docker-compose.yml`, then run:

    docker compose up -d --build
