# Credit-KeepAlive

Keeps a family's credit cards in use so banks don't close them for inactivity. Cards are
grouped by owner, and each person gets a reminder on their own phone or by email when a card is due.

**Beta.** This project is under active development and may change often. Expect frequent updates.

Created entirely by AI (Claude). Tested, but not in every situation. Do your own due diligence
before exposing it to the internet.

It runs as one container: the web app plus a built-in phone notification server. Your data stays on your server.

## Install on Unraid

Search for **Credit-KeepAlive** in Community Applications and install it. The template is also
in this repository at `unraid/my-Credit-Keepalive.xml`.

### Settings

| Setting | Required? | What to enter |
|---|---|---|
| Phone server address | Yes, for phone alerts | `http://YOUR-SERVER-IP:2586`, or your proxy hostname |
| App web address | Yes, for Mark used buttons | `http://YOUR-SERVER-IP:8686`, or your proxy hostname |
| Login password | No | Leave blank to choose it on your first visit |
| Time zone | No | Defaults to `America/New_York` |
| Behind a reverse proxy (`TRUST_PROXY`) | No | Leave blank unless a reverse proxy or Cloudflare Tunnel is in front of the app. See Security. |

Leave the ports (`8686` for the web interface, `2586` for phone notifications) and the app data
folder at their defaults unless you need to change them.

**First run**

1. Open `http://YOUR-SERVER-IP:8686` and set a password, if you didn't set one in the template.
2. Go to **Settings** and check that **Phone server status** says **ready**.
3. Set up your phone (below).

Your data is in `/mnt/user/appdata/credit-keepalive`: the database (`cards.db`) and card pictures
(`card-images`). Back up that folder.

**Using a hostname instead of an IP?** If you reach the server through a reverse proxy
(Nginx Proxy Manager, Traefik, Caddy) or Cloudflare Tunnel, enter the hostname for both addresses,
for example `https://ntfy.example.com`. Point the phone address at port `2586` and the web address at
port `8686`. Turn on WebSocket support for the phone address, use HTTPS for anything reached from
outside your home network, and set `TRUST_PROXY` to `1`.

## Set up your phone

1. Install the free **ntfy** app (App Store or Google Play).
2. Add the server using the **Phone server address** from the template.
3. Log in with username `phone` and the password shown in **Settings**.
4. Subscribe to topics. Topic names keep each person's reminders separate:
   - In **Settings**, the default topic is used for cards with no owner.
   - In **People**, open **Alerts** for each person to see their topic.

**Test it:** **Settings → Send test to default alerts**, or **Send test** on a person in **People**.
"Test sent" means the phone server accepted the message. If nothing arrives within a minute, check that
the phone is logged in as `phone` and subscribed to that topic.

Away from home, the phone needs a VPN such as Tailscale to reach the server.

iPhone instant alerts go through the public ntfy service. Only poll requests go there; message content stays on your server.

## Phone server logins

The phone server refuses anyone without a login. The container creates three on every start, and their
passwords are kept in the app data folder:

| Login | Used by | Can |
|---|---|---|
| `phone` | Family phones, in the ntfy app | Read alerts. Can't send. |
| `admin` | You, on the ntfy web page at the phone server address | Full control of the phone server. |
| `publisher` | Credit-KeepAlive itself | Send alerts. |

**Settings** shows the `phone` and `admin` passwords. **Make a new phone password** (or admin password)
changes it right away; then log in again on each phone with the new password.

If you're upgrading from a version without logins, log in to the ntfy app on each phone with the `phone` login.

## Using the app

- **Cards** show the issuer, last 4 digits, owner and a progress bar for the current cycle. Add cards from the
  dropdowns, or choose **+ Add a new issuer** or **+ Add a new card**.
- **Mark used today** resets the clock. **Other date** back-dates it. Filters: All, Due, Paused. **Use next**
  shows the most urgent card.
- **Card pictures** are optional. Upload a PNG, JPG, GIF or WEBP up to 4 MB on the add or edit page. Until then
  each card uses a generated picture. Real issuer card art is not included; Settings → **Upload pictures** takes
  files, folders or ZIPs for many cards at once.
- **Back up pictures** with Settings → **Export pictures (ZIP)**. Import them on a new installation with **Import pictures**.
- **People** controls each person's reminders. Turn them off without deleting anyone.
- **History** lists every logged use. Undo removes an entry.
- **Reminders** go out once a day at the hour set in Settings. Overdue cards repeat. **Send due reminders now**
  sends them immediately.
- **Import** a CSV with the columns `Issuer,Card,Last4,Owner`. A check list appears before anything is saved.
  Full card numbers are rejected. A sample file is on the import page.
- **Email** works through Apprise. Add an address in Settings, for example
  `mailto://yourname:app-password@gmail.com?to=you@example.com`. See the [Apprise docs](https://github.com/caronc/apprise) for other services.
- **Appearance** (Settings): 10 colour themes, light, dark or follow your device, plus corner rounding, font, shadows and page width. Saved on the server, so every device matches.

## Security

- Keep the app on your home network, or behind a reverse proxy with HTTPS.
- The dashboard, People, History and Settings share one login. Five wrong passwords from one address lock it for five minutes.
- Mark used links in notifications work without a login by design. Each carries a secret token and can only mark a card as used.
- Only the last 4 digits of each card are stored. Alert addresses and passwords are stored in `cards.db`, so protect the appdata folder.
- Forms check a CSRF token, and pages send security headers (no framing, no MIME sniffing, strict Content-Security-Policy).

## Run without Unraid

Put the files from this repository in one folder, change the two addresses in `docker-compose.yml`, then run:

    docker compose up -d --build

## Version

The footer of every page shows the build version, for example `2026.10.29-42-abc1234`. A copy run from source shows `dev`.

## For the repository owner

- Images are published to `ghcr.io/rcodehub/credit-keepalive` by the **Build and publish image** workflow on each push to `main`.
- The package must be public for Unraid to pull it (GitHub → Packages → credit-keepalive → Package settings → Change visibility).
- Each successful build also updates the template in `rcodehub/Unraid-CA-Apps` through **Sync Community Applications template**.
