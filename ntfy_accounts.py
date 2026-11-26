"""Accounts and access rules for the built-in ntfy server.

Three accounts. Each password is generated once and kept in the app data folder, so nothing
secret goes into the Unraid template.

  admin      The hoster. Logs in to the ntfy web page (Settings shows the password).
             Admin role, so it can see every topic.
  phone      The family's phones. Read-only on every topic. Enter this in the ntfy app.
  publisher  Credit Keepalive itself. Read and write on every topic. Its token is saved in
             the app data folder and used only for publishing on the built-in server.

The server runs with NTFY_AUTH_DEFAULT_ACCESS=deny-all, so nothing can be read or published
without one of these logins.

Run at startup by entrypoint.sh (python3 ntfy_accounts.py) before the ntfy server starts.
The app imports this module to show the logins and to regenerate passwords.
"""
import os
import re
import secrets
import string
import subprocess
import sys

NTFY_BIN = os.environ.get("NTFY_BIN", "ntfy")
DATA_DIR = os.path.dirname(os.environ.get("NTFY_AUTH_FILE", "/data/ntfy/auth.db")) or "/data/ntfy"

ADMIN, PHONE, PUBLISHER = "admin", "phone", "publisher"
ROLES = {ADMIN: "admin", PHONE: "user", PUBLISHER: "user"}
LOGIN_USERS = (ADMIN, PHONE)  # shown on the Settings page and can be regenerated there
TOKEN_FILE = os.path.join(DATA_DIR, "publisher.token")
TOKEN_RE = re.compile(r"tk_[A-Za-z0-9]{29}")


class AccountError(RuntimeError):
    pass


def password_file(user):
    return os.path.join(DATA_DIR, f"{user}.password")


def read_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_secret(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    os.replace(tmp, path)


def new_password():
    alphabet = string.ascii_letters + string.digits  # easy to type on a phone
    return "".join(secrets.choice(alphabet) for _ in range(22))


def _ntfy(args, password=None):
    env = dict(os.environ)
    if password is not None:
        env["NTFY_PASSWORD"] = password  # read by the ntfy CLI instead of prompting
    # stdin is closed so a prompt can't hang the start-up.
    return subprocess.run([NTFY_BIN, *args], capture_output=True, text=True, timeout=60,
                          env=env, stdin=subprocess.DEVNULL)


def _check(result, what):
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AccountError(f"{what} failed: {detail or 'no details from ntfy'}")


def password_for(user):
    return read_secret(password_file(user))


def set_password(user, value):
    """Set the password on the ntfy server, then save it. Raises AccountError on failure."""
    _check(_ntfy(["user", "change-pass", user], password=value), f"Setting the {user} password")
    write_secret(password_file(user), value)


def create_token():
    result = _ntfy(["token", "add", "--label=credit-keepalive", PUBLISHER])
    _check(result, "Creating the publisher token")
    found = TOKEN_RE.search(result.stdout or "")
    if not found:
        raise AccountError("Creating the publisher token: ntfy did not print a token.")
    return found.group(0)


def publisher_token():
    return read_secret(TOKEN_FILE)


def provision():
    """Create or update the three accounts and their access rules. Safe to run on every start."""
    os.makedirs(DATA_DIR, exist_ok=True)
    for user, role in ROLES.items():
        password = password_for(user) or new_password()
        # "user add" fails harmlessly when the account already exists.
        _ntfy(["user", "add", f"--role={role}", user], password=password)
        set_password(user, password)
    _check(_ntfy(["access", PUBLISHER, "*", "read-write"]), "Giving the publisher access")
    _check(_ntfy(["access", PHONE, "*", "read-only"]), "Giving the phone access")
    if not publisher_token():
        write_secret(TOKEN_FILE, create_token())


def regenerate(user):
    """New password for a login shown on the Settings page."""
    if user not in LOGIN_USERS:
        raise AccountError("Unknown login.")
    set_password(user, new_password())


if __name__ == "__main__":
    try:
        provision()
        print("ntfy accounts are ready.")
    except AccountError as e:
        print(f"ntfy accounts: {e}", file=sys.stderr)
        sys.exit(1)
