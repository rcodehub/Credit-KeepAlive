"""Credit-KeepAlive

Tracks family credit cards (grouped by owner) and reminds the right people when
a card is due for use again. Flask + SQLite, password login, a built-in
scheduler, ntfy push (with tap-to-mark-used buttons), and Apprise for email and
other alerts. Each person can have their own alerts and be turned off.
"""
import csv
from collections import Counter
import io
import json
import shutil
import os
import subprocess
import re
import secrets
from urllib.parse import quote
import sqlite3
import time
import zipfile
import ipaddress
import socket
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import apprise
import ntfy_accounts as ntfy_acct
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   send_from_directory, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash
import ipaddress

DB_PATH = os.environ.get("DB_PATH", "/data/cards.db")
# Uploaded card images live next to the database, so they are part of the app data folder.
CARD_IMG_DIR = os.path.join(os.path.dirname(DB_PATH), "card-images")
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_ZIP_BYTES = 40 * 1024 * 1024
MAX_ZIP_UNPACKED = 200 * 1024 * 1024
MAX_PICTURES = 200
PICTURE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
IMPORT_MAX_ROWS = 500
IMPORT_MAX_BYTES = 256 * 1024
# Address of the ntfy server that runs in this same container.
NTFY_INTERNAL = os.environ.get("NTFY_INTERNAL_URL", "http://127.0.0.1:2586").rstrip("/")
# Saved topics store this prefix, not the address, so the server can move without breaking them.
BUILTIN_PREFIX = "builtin:"
LEGACY_PREFIX = "http://ntfy/"  # topics saved by earlier versions

DEFAULTS = {
    # Default alerts: used for cards with no owner, or owners with no alerts set up.
    "ntfy_url": os.environ.get("NTFY_URL", ""),
    "ntfy_token": os.environ.get("NTFY_TOKEN", ""),
    "notify_urls": os.environ.get("APPRISE_URLS", "").replace(",", "\n"),
    "notify_hour": os.environ.get("NOTIFY_HOUR", "9"),
    "renag_days": os.environ.get("RENAG_DAYS", "3"),
    "default_interval": os.environ.get("DEFAULT_INTERVAL_DAYS", "45"),
    # How far ahead of its cycle a card may come up, as a percent of the cycle, so cards don't all fall on one day.
    "stagger_percent": "25",
    # Phone topics: "household" (one topic for everyone) or "person" (each person's own). Blank = worked out from the people.
    "topic_mode": "",
    # Addresses for the phone app and the Mark used links. Blank = found automatically.
    "phone_address": "",
    "app_address": "",
    # Appearance (see the Appearance section of Settings)
    "look_mode": "auto",
    "look_theme": "default",
    "look_radius": "18",
    "look_font": "",
    "look_shadow": "soft",
    "look_width": "standard",
}
APPEARANCE_DEFAULTS = {k: v for k, v in DEFAULTS.items() if k.startswith("look_")}



def app_address():
    """Web address of this app, used by the Mark used links. Saved in Settings, or filled in on first login."""
    return (get_setting("app_address") or os.environ.get("BASE_URL", "")).rstrip("/")


def seen_address():
    """The address this page was opened at, such as http://192.168.1.50:8686 or https://keepalive.example.com."""
    scheme = request.scheme
    if TRUST_PROXY and request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        scheme = "https"
    return f"{scheme}://{request.host}"


def phone_server():
    """Address the ntfy app connects to. Saved in Settings, or taken from NTFY_BASE_URL in older setups."""
    return (get_setting("phone_address") or os.environ.get("NTFY_BASE_URL", "")).rstrip("/")


def phone_guess():
    """Starting point when no phone address is saved: this server's address on the ntfy port."""
    return f"http://{request.host.split(':')[0]}:2586"


def host_is_name():
    """True when the page was opened by a name such as ntfy.example.com rather than an IP address."""
    import ipaddress
    try:
        ipaddress.ip_address(request.host.split(":")[0])
        return False
    except ValueError:
        return True


def topic_mode():
    """'household': every alert goes to the household topic. 'person': each person's own topic when they have one."""
    mode = get_setting("topic_mode")
    if mode in ("household", "person"):
        return mode
    conn = db()
    has_own = conn.execute("SELECT 1 FROM people WHERE ntfy_url != '' LIMIT 1").fetchone() is not None
    conn.close()
    return "person" if has_own else "household"

THEMES = [
    ("graphite", "Graphite"), ("ocean", "Ocean"), ("ember", "Ember"), ("forest", "Forest"),
    ("violet", "Violet"), ("gold", "Gold"), ("rose", "Rose"), ("slate", "Slate"),
]
THEME_KEYS = {k for k, _ in THEMES}

# Starting lists. Anyone can add more from the card form; these are only the first choices.
SEED_ISSUERS = [
    # Large national issuers
    "Chase", "American Express", "Citi", "Capital One", "Discover", "Bank of America",
    "Wells Fargo", "U.S. Bank", "Barclays", "Synchrony", "Bread Financial", "Goldman Sachs",
    "Apple Card", "TD Bank", "HSBC", "PNC", "Truist", "Fifth Third", "Regions", "KeyBank",
    "Huntington", "Citizens", "BMO", "M&T Bank",
    # Online and fintech issuers
    "SoFi", "Upgrade", "Petal", "Mission Lane", "Credit One Bank", "Merrick Bank", "Deserve",
    "Imprint", "Surge", "OpenSky", "First Premier Bank", "Cardless",
    # Credit unions
    "Navy Federal Credit Union", "USAA", "PenFed", "Alliant Credit Union",
    "Other",
]
SEED_CARDS = {
    "Chase": ["Sapphire Preferred", "Sapphire Reserve", "Freedom Unlimited", "Freedom Flex", "Freedom Student", "Freedom Rise", "Ink Business Cash", "Ink Business Unlimited", "Ink Business Preferred", "Ink Business Premier", "Amazon Prime Visa", "Southwest Rapid Rewards Priority", "Southwest Premier", "Southwest Premier Business", "Southwest Plus", "Southwest Performance Business", "United Explorer", "United Quest", "United Gateway", "United Business", "United Club Business", "United Club Infinite", "Marriott Bonvoy Boundless", "Marriott Bonvoy Bold", "Marriott Bonvoy Bountiful", "Disney Premier Visa", "World of Hyatt", "World of Hyatt Business", "Slate Edge", "Freedom", "Southwest Rapid Rewards Plus", "Southwest Rapid Rewards Premier"],
    "American Express": ["Gold Card", "Platinum Card", "Green Card", "Blue Cash Preferred", "Blue Cash Everyday", "Blue Business Plus", "Delta SkyMiles Gold", "Delta SkyMiles Platinum", "Hilton Honors", "Hilton Honors Surpass", "Marriott Bonvoy Brilliant", "Everyday", "Everyday Preferred", "Delta SkyMiles Reserve", "Hilton Honors Aspire", "Blue Business Cash", "Business Gold", "Business Platinum"],
    "Citi": ["Double Cash", "Premier", "Strata Premier", "Custom Cash", "Costco Anywhere Visa", "AAdvantage Platinum Select", "Simplicity", "Strata", "Strata Elite", "Prestige", "AAdvantage Executive", "AAdvantage Globe", "AAdvantage MileUp", "Costco Anywhere Visa Business", "Best Buy Visa"],
    "Capital One": ["Venture", "Venture X", "VentureOne", "Quicksilver", "SavorOne", "Platinum", "Savor", "Savor Student", "QuicksilverOne", "Quicksilver Student", "Quicksilver Secured", "Platinum Secured", "Spark Cash Plus", "Spark Miles for Business", "Venture X Business"],
    "Discover": ["it Cash Back", "it Miles", "it Student", "it Chrome", "it Student Chrome", "it Student Cash Back", "it Secured"],
    "Bank of America": ["Customized Cash Rewards", "Travel Rewards", "Premium Rewards", "Unlimited Cash Rewards", "Premium Rewards Elite", "BankAmericard", "Customized Cash Rewards for Students", "Business Advantage Travel Rewards World Mastercard"],
    "Wells Fargo": ["Autograph", "Active Cash", "Attune", "Reflect", "One Key", "Choice Privileges", "Signify Business Cash"],
    "U.S. Bank": ["Altitude Reserve", "Cash+", "Visa Platinum", "Altitude Connect", "Altitude Go", "Smartly", "Shopper Cash Rewards", "SKYPASS Select", "SKYPASS", "Amazon Business World Elite", "Prime Business World Elite", "Kroger Rewards World Elite"],
    "Barclays": ["Aviator Red", "Aviator", "JetBlue Plus", "JetBlue Card", "JetBlue Premier", "Wyndham Rewards Earner", "Wyndham Rewards Earner Plus", "Wyndham Rewards Earner Premier", "Barclaycard Arrival Plus", "Barclays Arrival Premier", "Barclaycard Rewards", "Barclaycard CashForward", "Barclaycard Ring", "Priceline VIP Rewards", "AARP Essential Rewards", "AARP Travel Rewards", "Carnival Rewards", "Princess Cruises Rewards", "Holland America Line Rewards", "GM Rewards", "RCI Elite Rewards", "Upromise", "Xbox", "Barnes & Noble", "Frontier Airlines World", "Hawaiian Airlines World Elite", "Lufthansa Miles & More World Elite", "Emirates Skywards Rewards World Elite", "Emirates Skywards Premium World Elite", "Breeze Easy", "Gap Encore", "Old Navy Encore", "Banana Republic Encore", "Athleta Encore", "AAdvantage Aviator Red World Elite"],
    "Apple Card": ["Apple Card"],
    "PNC": ["Cash Rewards", "Points", "Cash Unlimited Visa Signature", "Spend Wise Visa", "Secured Visa"],
    "Synchrony": ["Premier World Mastercard", "Plus World Mastercard", "Preferred World Mastercard", "HOME", "CareCredit Rewards Mastercard", "CareCredit", "Outdoors", "MyLowe's Rewards", "OnePay CashRewards", "Sam's Club Mastercard", "PayPal Cashback Mastercard", "Venmo", "Verizon Visa", "Amazon Store Card", "American Eagle", "Belk", "Cathay Pacific", "Chevron", "Dick's Sporting Goods", "Discount Tire", "Guitar Center", "Harbor Freight", "JCPenney", "J. Crew", "myWalgreens Mastercard", "Pep Boys", "Rooms to Go", "Sun Country Airlines", "TJX Rewards Platinum Mastercard", "Virgin Red", "Car Care"],
    "Bread Financial": ["Bread Cashback American Express"],
    "Upgrade": ["Upgrade Card"],
    "Petal": ["Petal 2"],
    "Mission Lane": ["Mission Lane Visa"],
    "Credit One Bank": ["Platinum Visa"],
    "Merrick Bank": ["Double Your Line"],
    "Deserve": ["EDU Mastercard", "Pro Mastercard"],
    "Surge": ["Platinum Mastercard"],
    "OpenSky": ["Secured Visa"],
    "Cardless": ["Credit Card"],
    "Navy Federal Credit Union": ["cashRewards", "GO REWARDS", "Visa Signature Flagship Rewards"],
    "USAA": ["Rewards American Express", "Cash Rewards Mastercard"],
    "PenFed": ["Platinum Rewards Visa", "Pathfinder Rewards Visa Signature", "Promise Visa"],
    "Alliant Credit Union": ["Cashback Visa Signature", "Platinum Visa"],
    "TD Bank": ["Target Circle Card", "TD Cash", "TD Double Up"],
    "HSBC": ["Cash Rewards Mastercard", "Premier Mastercard"],
    "Truist": ["Enjoy Cash", "Enjoy Travel", "Future", "Enjoy Cash Secured", "Enjoy Beyond"],
    "Fifth Third": ["Preferred Cash/Back", "1.67% Cash/Back", "1% Cash/Back", "TRIO", "TRIO Preferred", "Truly Simple", "Secured"],
    "Regions": ["Cash Rewards", "Prestige", "Premium Visa Signature"],
    "KeyBank": ["Latitude", "Key Cashback", "Key Secured", "Key2More Rewards", "Business Cash Rewards"],
    "Huntington": ["Huntington Cashback", "Huntington Voice Rewards", "Huntington Voice", "Huntington Secured", "Ohio State Voice"],
    "Citizens": ["Amp Mastercard", "Spring World Mastercard", "Summit World Mastercard", "Summit Reserve World Elite Mastercard", "Cash Back Plus World Mastercard", "Private Client World Elite Mastercard", "Clear Value Mastercard"],
    "M&T Bank": ["M&T Signature", "M&T Rewards", "M&T Visa", "M&T Secured"],
    "SoFi": ["SoFi Everyday Cash Rewards", "SoFi Essential"],
}

THEMES = [
    ("graphite", "Graphite"), ("ocean", "Ocean"), ("ember", "Ember"), ("forest", "Forest"),
    ("violet", "Violet"), ("gold", "Gold"), ("rose", "Rose"), ("slate", "Slate"),
]
THEME_KEYS = {k for k, _ in THEMES}

# Plain-text suggestions for the issuer box. These are only names, not logos.
ISSUER_SUGGESTIONS = [
    "Chase", "American Express", "Citi", "Capital One", "Discover", "Bank of America",
    "Wells Fargo", "U.S. Bank", "Barclays", "Synchrony", "Apple Card", "Other",
]


# ---------------------------------------------------------------- database
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname TEXT NOT NULL,
            issuer TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            last4 TEXT NOT NULL DEFAULT '',
            theme TEXT NOT NULL DEFAULT 'graphite',
            interval_days INTEGER NOT NULL DEFAULT 45,
            last_used TEXT,
            last_notified TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            used_on TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'dashboard',
            logged_at TEXT,
            FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            enabled INTEGER NOT NULL DEFAULT 1,
            ntfy_url TEXT NOT NULL DEFAULT '',
            apprise_urls TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS issuers (
            name TEXT PRIMARY KEY COLLATE NOCASE
        );
        CREATE TABLE IF NOT EXISTS card_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer TEXT NOT NULL COLLATE NOCASE,
            name TEXT NOT NULL COLLATE NOCASE,
            image TEXT NOT NULL DEFAULT '',
            UNIQUE (issuer, name)
        );
        """
    )
    # Upgrade databases created by earlier versions.
    cc = _columns(conn, "cards")
    if "issuer" not in cc:
        conn.execute("ALTER TABLE cards ADD COLUMN issuer TEXT NOT NULL DEFAULT ''")
    if "theme" not in cc:
        conn.execute("ALTER TABLE cards ADD COLUMN theme TEXT NOT NULL DEFAULT 'graphite'")
    if "due_on" not in cc:
        conn.execute("ALTER TABLE cards ADD COLUMN due_on TEXT")
    uc = _columns(conn, "usage_log")
    if "source" not in uc:
        conn.execute("ALTER TABLE usage_log ADD COLUMN source TEXT NOT NULL DEFAULT 'dashboard'")
    if "logged_at" not in uc:
        conn.execute("ALTER TABLE usage_log ADD COLUMN logged_at TEXT")
    # Every existing owner becomes a person (enabled, no alerts yet).
    conn.execute(
        "INSERT OR IGNORE INTO people(name) SELECT DISTINCT owner FROM cards WHERE TRIM(owner) != ''"
    )
    gone_issuers, gone_cards = removed_catalog()
    for name in SEED_ISSUERS:
        if name.lower() not in gone_issuers:
            conn.execute("INSERT OR IGNORE INTO issuers(name) VALUES(?)", (name,))
    for issuer, names in SEED_CARDS.items():
        if issuer.lower() in gone_issuers:
            continue
        for name in names:
            if (issuer.lower(), name.lower()) not in gone_cards:
                conn.execute("INSERT OR IGNORE INTO card_types(issuer, name) VALUES(?,?)", (issuer, name))
    # Once only: issuers and cards entered before the catalog existed become catalog entries too.
    # Running it again would bring back anything the user removed from the list.
    if not conn.execute("SELECT 1 FROM settings WHERE key='catalog_migrated'").fetchone():
        conn.execute("INSERT OR IGNORE INTO issuers(name) SELECT DISTINCT issuer FROM cards WHERE TRIM(issuer) != ''")
        conn.execute("INSERT OR IGNORE INTO card_types(issuer, name) SELECT DISTINCT issuer, nickname "
                     "FROM cards WHERE TRIM(issuer) != '' AND TRIM(nickname) != ''")
        conn.execute("INSERT INTO settings(key, value) VALUES('catalog_migrated', '1')")
    conn.commit()
    conn.close()
    if get_setting("token") is None:
        set_setting("token", secrets.token_urlsafe(16))
    if get_setting("secret_key") is None:
        set_setting("secret_key", secrets.token_hex(24))
    env_pw = os.environ.get("APP_PASSWORD", "")
    if env_pw and get_setting("password_hash") is None:
        set_setting("password_hash", generate_password_hash(env_pw))


def get_setting(key):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key)


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def people_rows():
    conn = db()
    rows = conn.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return rows


def ensure_person(name):
    name = (name or "").strip()
    if not name:
        return
    conn = db()
    conn.execute("INSERT OR IGNORE INTO people(name) VALUES(?)", (name,))
    conn.commit()
    conn.close()


def list_issuers():
    conn = db()
    names = [r["name"] for r in conn.execute("SELECT name FROM issuers")]
    conn.close()
    return sorted(names, key=lambda n: (n == "Other", n.lower()))


def list_card_types():
    conn = db()
    rows = conn.execute("SELECT issuer, name FROM card_types ORDER BY issuer COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Real card art is not shipped with the app (issuer art is trademarked). Users can download a zip of it
# from this link and import it on the Settings page. Leave empty to hide the link.
REAL_ART_URL = ""

DEFAULT_PICTURE_SIZE = (600, 380)
DEFAULT_PICTURE_COLORS = {  # same palette as the card themes
    "graphite": ("#4a5366", "#141821"), "ocean": ("#2c8cf0", "#0c3576"), "ember": ("#ff8a4c", "#9a2e12"),
    "forest": ("#36b67c", "#0d4a33"), "violet": ("#9b75ff", "#3a1c86"), "gold": ("#f0c75e", "#8e6512"),
    "rose": ("#ff6fae", "#8c1d55"), "slate": ("#8a96aa", "#2c3443"),
}


def _mix(a, b, t):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


GENERIC_PICTURE_MARK = "credit-keepalive-generic"
GENERIC_PICTURE_VERSION = "3"


def _default_font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: small built-in font
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_default_picture(issuer, name, path):
    """Draw a generic card picture: a colored card with the issuer and card name printed on it.

    No issuer artwork is used. The file is marked so the app can tell it apart from a real picture when redrawing.
    """
    from PIL import Image, ImageDraw, PngImagePlugin
    w, h = DEFAULT_PICTURE_SIZE
    keys = sorted(DEFAULT_PICTURE_COLORS)
    key = keys[sum(map(ord, issuer or "card")) % len(keys)]  # the same issuer always gets the same color
    top, bottom = DEFAULT_PICTURE_COLORS[key]
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_mix(top, bottom, y / (h - 1)))
    ink = (33, 25, 10) if key == "gold" else (255, 255, 255)
    draw.rounded_rectangle([40, 120, 120, 170], radius=8, fill=_mix(top, "#ffffff", 0.45))  # chip
    draw.text((40, 40), (issuer or "").strip()[:40], font=_default_font(34), fill=ink)
    card_font = _default_font(44)
    lines = _wrap(draw, (name or "").strip(), card_font, w - 80)[:2]
    y = h - 40 - 52 * len(lines)
    for line in lines:
        draw.text((40, y), line, font=card_font, fill=ink)
        y += 52
    meta = PngImagePlugin.PngInfo()
    meta.add_text(GENERIC_PICTURE_MARK, GENERIC_PICTURE_VERSION)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG", optimize=True, pnginfo=meta)


def redraw_generic_pictures():
    """Redraw generic pictures made by an older version. Real pictures are not touched.

    A generic picture is recognized by its size and its PNG mark, so only the app's own files are redrawn.
    Returns how many were redrawn.
    """
    from PIL import Image
    conn = db()
    rows = conn.execute("SELECT issuer, name, image FROM card_types").fetchall()
    conn.close()
    named = _named_pictures()
    made = 0
    for r in rows:
        fn = card_picture(r["issuer"], r["name"], r["image"] or "", named)
        if not fn or not fn.lower().endswith(".png"):
            continue
        path = os.path.join(CARD_IMG_DIR, fn)
        try:
            with Image.open(path) as im:
                generic = im.size == DEFAULT_PICTURE_SIZE and im.info.get(GENERIC_PICTURE_MARK) != GENERIC_PICTURE_VERSION
        except OSError:
            continue
        if generic:
            draw_default_picture(r["issuer"], r["name"], path)
            made += 1
    return made


def ensure_default_picture(issuer, name):
    """Draw a generic picture for a card that has none. Never replaces a picture that is already there."""
    if not (issuer or "").strip() or not (name or "").strip():
        return False
    if card_picture(issuer, name, "") is not None:
        return False
    draw_default_picture(issuer, name, os.path.join(CARD_IMG_DIR, f"{picture_name(issuer, name)}.png"))
    return True


def ensure_default_pictures():
    """Make sure every card in the list has a picture. Returns how many were drawn."""
    conn = db()
    rows = conn.execute("SELECT issuer, name FROM card_types").fetchall()
    conn.close()
    named = _named_pictures()
    made = 0
    for r in rows:
        if card_picture(r["issuer"], r["name"], "", named) is None and r["issuer"].strip() and r["name"].strip():
            draw_default_picture(r["issuer"], r["name"],
                                 os.path.join(CARD_IMG_DIR, f"{picture_name(r['issuer'], r['name'])}.png"))
            made += 1
    return made


def ensure_card_type(issuer, name):
    """Add an issuer and a card to the catalog, so they show up in the dropdowns."""
    issuer, name = (issuer or "").strip(), (name or "").strip()
    if not issuer:
        return
    conn = db()
    conn.execute("INSERT OR IGNORE INTO issuers(name) VALUES(?)", (issuer,))
    if name:
        conn.execute("INSERT OR IGNORE INTO card_types(issuer, name) VALUES(?,?)", (issuer, name))
    conn.commit()
    conn.close()
    if name:
        ensure_default_picture(issuer, name)


def _named_pictures():
    """{picture name lowercased: file name} for every picture in the data folder, e.g. chase-sapphire-preferred.png."""
    out = {}
    try:
        names = sorted(os.listdir(CARD_IMG_DIR))
    except OSError:
        return out
    for fn in names:
        stem, ext = os.path.splitext(fn)
        if ext.lower() in PICTURE_EXTS and os.path.isfile(os.path.join(CARD_IMG_DIR, fn)):
            out.setdefault(stem.lower(), fn)
    return out


def card_picture(issuer, name, legacy="", named=None):
    """File name of a card's picture in the data folder, or None.

    A file named after the card (Chase-Sapphire-Preferred.png) wins, so you can replace it by dropping in a
    file with that name. Older pictures saved under random names still work until the card's picture is replaced.
    """
    if named is None:
        named = _named_pictures()
    fn = named.get(picture_name(issuer, name).lower())
    if fn:
        return fn
    if legacy and os.path.isfile(os.path.join(CARD_IMG_DIR, legacy)):
        return legacy
    return None


def image_map():
    """{(issuer, card) lowercased: picture file} for every card whose picture is in the data folder."""
    named = _named_pictures()
    conn = db()
    rows = conn.execute("SELECT issuer, name, image FROM card_types").fetchall()
    conn.close()
    out = {}
    for r in rows:
        fn = card_picture(r["issuer"], r["name"], r["image"] or "", named)
        if fn:
            out[(r["issuer"].lower(), r["name"].lower())] = fn
    return out


def picture_health():
    """(saved picture count, names of cards whose saved picture file is missing from the data folder)."""
    named = _named_pictures()
    conn = db()
    rows = conn.execute("SELECT issuer, name, image FROM card_types").fetchall()
    conn.close()
    count, missing = 0, []
    for r in rows:
        if card_picture(r["issuer"], r["name"], r["image"] or "", named):
            count += 1
        if r["image"] and not os.path.isfile(os.path.join(CARD_IMG_DIR, r["image"])):
            # the saved picture is gone, even if a generic one is covering for it
            missing.append(f"{r['issuer']} {r['name']}")
    return count, missing


_GENERIC_SEEN = {}


def picture_is_generic(fn):
    """True when the picture is one the app drew: it carries the generic mark, or it is a PNG at the generic size.

    The size check also catches generic pictures drawn by earlier versions, which had no mark.
    """
    if not fn:
        return False
    path = os.path.join(CARD_IMG_DIR, fn)
    try:
        key = (fn, os.path.getmtime(path))
    except OSError:
        return False
    if key not in _GENERIC_SEEN:
        from PIL import Image
        try:
            with Image.open(path) as im:
                _GENERIC_SEEN[key] = (GENERIC_PICTURE_MARK in im.info
                                      or (im.format == "PNG" and im.size == DEFAULT_PICTURE_SIZE))
        except OSError:
            _GENERIC_SEEN[key] = False
    return _GENERIC_SEEN[key]


def card_image_url(images, issuer, name):
    # Built directly, not with url_for, because the reminder check runs outside a web request.
    fn = (images or {}).get(((issuer or "").lower(), (name or "").lower()))
    return "/card-images/" + quote(fn) if fn else None


def slug(text):
    """Lowercase words joined by single hyphens: 'U.S. Bank' -> 'u-s-bank', 'Cash+' -> 'cash-plus'."""
    t = (text or "").lower().replace("&", " and ").replace("+", " plus ")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def picture_name(issuer, card):
    """Expected picture file name (without extension) for a card: issuer-card."""
    return f"{slug(issuer)}-{slug(card)}"


# Words that don't identify a card. "business" is handled separately (see match_picture).
PICTURE_NOISE = {
    "card", "cards", "credit", "the", "of", "and", "visa", "mastercard", "master", "image", "img",
    "art", "cardart", "logo", "hero", "desktop", "mobile", "png", "jpg", "jpeg", "webp", "gif", "svg",
    "copy", "final", "new", "web", "product", "alt", "thumb", "large", "small", "version", "v2",
    "x2", "halo", "ribbon", "jan", "vs", "clickable", "links", "link", "product", "page", "registered",
    "trademark", "different", "ways", "earn", "every", "quarter", "to", "for", "with", "in", "on", "a",
    "an", "your", "is", "at", "from", "images",
}
# How issuers are written in file names.
PICTURE_ALIASES = {
    "American Express": ["amex"], "Bank of America": ["bofa", "boa"], "Capital One": ["capone", "cap1"],
    "U.S. Bank": ["usbank"], "Wells Fargo": ["wf"], "Citi": ["citibank"], "Discover": ["discovercard"],
    "Apple Card": ["applecard"],
}
PICTURE_SYNONYMS = {"biz": "business", "swa": "southwest"}
# Other names a card's picture may use.
PICTURE_CARD_ALIASES = {
    "Amazon Prime Visa": ["Amazon Visa", "Prime Visa"],
    "Southwest Rapid Rewards Priority": ["Southwest Priority"],
}


def _words_of(text):
    """Split a name into lowercase pieces: CamelCase, '+' and '&' are kept as words, other marks split."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    text = text.replace("+", " plus ").replace("&", " and ")
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", text) if t]


def _split_glued(token, vocab):
    """Split a run-together word such as 'preferredcard' into known words. Returns None if it can't."""
    n = len(token)
    best = [None] * (n + 1)
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(i + 2, n + 1):
            piece = token[i:j]
            if piece in vocab and (best[j] is None or len(best[i]) + 1 < len(best[j])):
                best[j] = best[i] + [piece]
    return best[n] if best[n] and len(best[n]) > 1 else None


def picture_tokens(text, vocab=None):
    """The words in a name that identify a card. With a vocab, run-together words are split into known words."""
    out = set()
    for t in _words_of(text):
        if t.isdigit() or re.fullmatch(r"\d+x\d*", t):      # 2, 1200x630
            continue
        if vocab and t not in vocab:
            m = re.fullmatch(r"([a-z]{3,}?)\d+", t)           # card2, preferredcard2026
            if m:
                t = m.group(1)
            parts = _split_glued(t, vocab) if t not in vocab else None
            if parts:
                out.update(parts)
                continue
        out.add(t)
    return {PICTURE_SYNONYMS.get(t, t) for t in out} - PICTURE_NOISE


def _picture_vocab(catalog):
    """Every word the catalog and aliases use. Only these can be pulled out of run-together names."""
    names = [c["issuer"] for c in catalog] + [c["name"] for c in catalog]
    for c in catalog:
        names += PICTURE_CARD_ALIASES.get(c["name"], [])
    for aliases in PICTURE_ALIASES.values():
        names += aliases
    vocab = set(PICTURE_NOISE) | set(PICTURE_SYNONYMS) | set(PICTURE_SYNONYMS.values())
    for n in names:
        vocab |= set(_words_of(n))
    return {w for w in vocab if len(w) >= 2}


def _bank_keys(issuer):
    keys = [picture_tokens(issuer)] + [picture_tokens(a) for a in PICTURE_ALIASES.get(issuer, [])]
    return [k for k in keys if k]


def match_picture(stem, catalog):
    """Find the card a picture file is for.

    Returns (card or None, note, sure). Only a sure match is selected for the user. Anything else
    is shown with a note so the user picks it.
    """
    vocab = _picture_vocab(catalog)
    T = picture_tokens(stem, vocab)
    if not T:
        return None, "No card words in the file name", False
    banks = {c["issuer"] for c in catalog}
    mentioned = {b for b in banks if any(k <= T for k in _bank_keys(b))}
    if len(mentioned) > 1:
        return None, "The name mentions more than one bank", False
    bank_words = set()
    for b in mentioned:
        for k in _bank_keys(b):
            bank_words |= k
    file_biz = "business" in T
    card_words = T - bank_words - {"business"}
    scored = []
    for c in catalog:
        if mentioned and c["issuer"] not in mentioned:
            continue
        best = None
        for name in [c["name"]] + PICTURE_CARD_ALIASES.get(c["name"], []):
            words = picture_tokens(name)
            card_biz = "business" in words
            if file_biz and not card_biz:
                continue                       # "biz" in the file means a business card
            hard = words - {"business"}
            if not hard or not hard <= card_words:
                continue
            score = len(hard) * 10 + (5 if c["issuer"] in mentioned else 0)
            if card_biz and not file_biz:
                score -= 1                     # a personal card is preferred when the file doesn't say biz
            if best is None or score > best[0]:
                best = (score, hard)
        if best:
            scored.append((best[0], c, best[1]))
    if not scored:
        return None, "No matching card in the list", False
    scored.sort(key=lambda x: -x[0])
    top = [x for x in scored if x[0] == scored[0][0]]
    if len(top) > 1:
        return None, "Could be: " + ", ".join(f"{x[1]['issuer']} {x[1]['name']}" for x in top), False
    _, card, hard = top[0]
    extra = sorted(card_words - hard)
    if extra:
        return None, (f"Not sure: the name also has {', '.join(extra)}. "
                      f"Closest is {card['issuer']} {card['name']}"), False
    return card, "Matched automatically", True


def _pick_value(card):
    return f"{card['issuer']}\t{card['name']}"


def _image_ext(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_card_image(issuer, name, upload):
    """Save an uploaded picture for this card type in the data folder. Returns an error message or None."""
    data = upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return "That image is larger than 4 MB, so it was not saved."
    ext = _image_ext(data[:16])
    if ext is None:
        return "Use a PNG, JPG, GIF or WEBP image. That file was not saved."
    conn = db()
    row = conn.execute("SELECT id, image FROM card_types WHERE issuer=? AND name=?", (issuer, name)).fetchone()
    if row is None:
        conn.close()
        return "Could not find that card to attach the image to."
    os.makedirs(CARD_IMG_DIR, exist_ok=True)
    fn = f"{picture_name(issuer, name)}.{ext}"
    # remove the card's older picture(s): an old random name, or the same name with another extension
    named = _named_pictures()
    old = {row["image"] or "", named.get(picture_name(issuer, name).lower(), "")} - {"", fn}
    for other in old:
        _delete_image(other)
    with open(os.path.join(CARD_IMG_DIR, fn), "wb") as f:
        f.write(data)
    conn.execute("UPDATE card_types SET image=? WHERE id=?", (fn, row["id"]))
    conn.commit()
    conn.close()
    return None


def removed_catalog():
    """Starting issuers and cards the user removed, so restarts don't add them back."""
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key='removed_catalog'").fetchone()
    conn.close()
    try:
        data = json.loads(row["value"]) if row else {}
    except ValueError:
        data = {}
    issuers = {n.lower() for n in data.get("issuers", [])}
    cards = {(i.lower(), n.lower()) for i, n in data.get("cards", [])}
    return issuers, cards


def remember_removed(issuer=None, card=None):
    try:
        data = json.loads(get_setting("removed_catalog") or "{}")
    except ValueError:
        data = {}
    if issuer:
        data.setdefault("issuers", []).append(issuer)
    if card:
        data.setdefault("cards", []).append(list(card))
    set_setting("removed_catalog", json.dumps(data))


def catalog_groups():
    """Issuers with their cards, for the Settings page."""
    by = {}
    for c in list_card_types():
        by.setdefault(c["issuer"].lower(), []).append(c)
    return [{"issuer": i, "cards": by.get(i.lower(), [])} for i in list_issuers()]


def _delete_image(fn):
    if fn:
        try:
            os.remove(os.path.join(CARD_IMG_DIR, fn))
        except OSError:
            pass


def _card_key(issuer, name, last4, owner):
    return tuple((x or "").strip().lower() for x in (issuer, name, last4, owner))


def _existing_card_keys():
    conn = db()
    rows = conn.execute("SELECT issuer, nickname, last4, owner FROM cards").fetchall()
    conn.close()
    return {_card_key(r["issuer"], r["nickname"], r["last4"], r["owner"]) for r in rows}


def check_card_row(issuer, card, last4, owner):
    """(card data, None) if the row can be added, or (None, reason)."""
    issuer, card = (issuer or "").strip(), (card or "").strip()
    last4, owner = (last4 or "").strip(), (owner or "").strip()
    if not card:
        card = "Unnamed card"  # a blank name still imports; rename the card later on its edit page
    if len(card) > 80 or len(issuer) > 60 or len(owner) > 60:
        return None, "A name is too long"
    if last4 and not re.fullmatch(r"\d{4}", last4):
        # Never echo the value back: it may be a full card number.
        return None, "Last 4 must be exactly 4 digits. Full card numbers are not accepted"
    return {
        "issuer": issuer, "card": card, "last4": last4, "owner": owner,
    }, None


def parse_card_csv(text):
    """Turn CSV text into preview entries. Returns (entries, error)."""
    header = None
    entries = []
    existing = _existing_card_keys()
    seen = set()
    for lineno, cells in enumerate(csv.reader(io.StringIO(text)), start=1):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        if header is None:
            lowered = [c.lower() for c in cells]
            if {"card", "issuer", "last4", "owner"} & set(lowered):
                if "card" not in lowered:
                    return [], "The first line needs a Card column."
                header = {name: i for i, name in enumerate(lowered)}
                continue
            header = {"issuer": 0, "card": 1, "last4": 2, "owner": 3}

        def cell(name):
            i = header.get(name)
            return cells[i] if i is not None and i < len(cells) else ""

        entry = {"line": lineno, "issuer": cell("issuer"), "card": cell("card"),
                 "last4": cell("last4"), "owner": cell("owner"), "message": ""}
        if len(entries) >= IMPORT_MAX_ROWS:
            entry.update(status="problem", message=f"Over the {IMPORT_MAX_ROWS}-card limit")
        else:
            data, err = check_card_row(entry["issuer"], entry["card"], entry["last4"], entry["owner"])
            if entry["last4"] and not re.fullmatch(r"\d{4}", entry["last4"]):
                entry["last4"] = "not saved"  # never show what was typed here: it may be a card number
            if err:
                entry.update(status="problem", message=err)
            else:
                entry["card"] = data["card"]  # show the name that will be saved, e.g. Unnamed card
                key = _card_key(data["issuer"], data["card"], data["last4"], data["owner"])
                if key in existing:
                    entry.update(status="duplicate", message="Already in your cards")
                elif key in seen:
                    entry.update(status="duplicate", message="Repeated in this file")
                else:
                    seen.add(key)
                    entry.update(status="add", message="")
        entries.append(entry)
    if header is None:
        return [], "The file has no card lines."
    return entries, None


# ------------------------------------------------------------------- logic
def card_due_date(card):
    """Date the card is next due: its planned date when one is set, otherwise last use plus its cycle."""
    if not card["last_used"]:
        return date.fromisoformat(card["due_on"]) if card["due_on"] else date.today()
    cycle_end = date.fromisoformat(card["last_used"]) + timedelta(days=card["interval_days"])
    if card["due_on"]:
        planned = date.fromisoformat(card["due_on"])
        # a plan outside the current cycle is stale (for example, last use was changed outside the app)
        if date.fromisoformat(card["last_used"]) <= planned <= cycle_end:
            return planned
    return cycle_end


def next_due(card):
    return card_due_date(card)


def stagger_percent():
    try:
        return min(50, max(0, int(get_setting("stagger_percent") or 0)))
    except ValueError:
        return 0


def due_window(last_used, interval, today):
    """First and last day a card may come up.

    The cycle ends on last use plus the interval. The card may come up early by the stagger percent
    of that cycle. A card never used yet can come up any day in its first cycle.
    """
    if last_used:
        hi = date.fromisoformat(last_used) + timedelta(days=interval)
        lo = hi - timedelta(days=interval * stagger_percent() // 100)
    else:
        if not stagger_percent():
            return today, today
        hi = today + timedelta(days=interval)
        lo = today
    if hi < today:
        return today, today                    # already overdue: due today
    return max(lo, today), hi


def pick_day(lo, hi, load):
    """The day in [lo, hi] with the fewest cards already due. Ties go to the later day."""
    best, best_n = hi, load.get(hi, 0)
    d = hi - timedelta(days=1)
    while d >= lo:
        if load.get(d, 0) < best_n:
            best, best_n = d, load.get(d, 0)
        d -= timedelta(days=1)
    return best


def plan_saved_card(conn, card_id):
    """Pick the planned due date for one saved card, on a day with few other cards due."""
    row = conn.execute("SELECT id, last_used, interval_days, active FROM cards WHERE id=?", (card_id,)).fetchone()
    if row is None:
        return None
    if not row["active"]:
        conn.execute("UPDATE cards SET due_on=NULL WHERE id=?", (card_id,))
        return None
    lo, hi = due_window(row["last_used"], row["interval_days"], date.today())
    load = Counter(card_due_date(r) for r in conn.execute("SELECT * FROM cards WHERE active=1 AND id!=?", (card_id,)))
    day = pick_day(lo, hi, load)
    conn.execute("UPDATE cards SET due_on=? WHERE id=?", (day.isoformat(), card_id))
    return day


def spread_all_cards(conn):
    """Plan a due date for every active card so they don't pile onto the same days.

    Cards with the tightest windows are placed first. Returns (cards planned, days used).
    """
    today = date.today()
    rows = conn.execute("SELECT id, last_used, interval_days FROM cards WHERE active=1").fetchall()
    windows = sorted((due_window(r["last_used"], r["interval_days"], today)[1], r["id"],
                      due_window(r["last_used"], r["interval_days"], today)[0]) for r in rows)
    load = Counter()
    for hi, cid, lo in windows:
        day = pick_day(lo, hi, load)
        load[day] += 1
        conn.execute("UPDATE cards SET due_on=? WHERE id=?", (day.isoformat(), cid))
    conn.execute("UPDATE cards SET due_on=NULL WHERE active=0")
    return len(windows), len(load)


def card_view(card, images=None):
    nd = next_due(card)
    days = (nd - date.today()).days  # negative = overdue
    if not card["active"]:
        status, label, pct = "paused", "Paused", 0
    else:
        if card["last_used"]:
            start = date.fromisoformat(card["last_used"])
            span = max(1, card["interval_days"])
            pct = max(0, min(100, round((date.today() - start).days * 100 / span)))
        else:
            pct = 100 if days <= 0 else 0
        if days < 0:
            status = "overdue"
            label = "Never used" if not card["last_used"] else f"{-days} day{'s' if days != -1 else ''} overdue"
        elif days == 0:
            status, label = "due", "Due today"
        elif days <= 7:
            status, label = "soon", f"Due in {days} day{'s' if days != 1 else ''}"
        else:
            status, label = "ok", f"Due in {days} days"
    theme = card["theme"] if card["theme"] in THEME_KEYS else "graphite"
    return {
        "id": card["id"],
        "nickname": card["nickname"],
        "issuer": card["issuer"],
        "owner": card["owner"],
        "last4": card["last4"],
        "theme": theme,
        "interval_days": card["interval_days"],
        "last_used": card["last_used"],
        "notes": card["notes"],
        "active": bool(card["active"]),
        "next_due": nd.isoformat(),
        "days": days,
        "status": status,
        "label": label,
        "pct": pct,
        "image": card_image_url(images, card["issuer"], card["nickname"]),
        "art_only": picture_is_generic((images or {}).get(((card["issuer"] or "").lower(), (card["nickname"] or "").lower()))),
    }


def all_card_views():
    conn = db()
    rows = conn.execute("SELECT * FROM cards").fetchall()
    conn.close()
    images = image_map()
    views = [card_view(r, images) for r in rows]
    # Active cards first, most urgent first; paused cards at the bottom.
    views.sort(key=lambda v: (not v["active"], v["days"], v["nickname"].lower()))
    return views


def dashboard_stats(views):
    active = [v for v in views if v["active"]]
    return {
        "active": len(active),
        "due": sum(1 for v in active if v["days"] <= 0),
        "overdue": sum(1 for v in active if v["days"] < 0),
        "paused": len(views) - len(active),
        "next": active[0] if active else None,
    }


def grouped_views(filter_name, views):
    if filter_name == "due":
        views = [v for v in views if v["active"] and v["days"] <= 0]
    elif filter_name == "paused":
        views = [v for v in views if not v["active"]]
    people = {p["name"].lower(): p for p in people_rows()}
    buckets = {}
    for v in views:
        buckets.setdefault(v["owner"], []).append(v)
    groups = []
    for owner in sorted(buckets, key=lambda n: (n == "", n.lower())):
        cards = buckets[owner]
        p = people.get(owner.lower()) if owner else None
        groups.append({
            "name": owner or "Unassigned",
            "cards": cards,
            "is_person": p is not None,
            "enabled": bool(p["enabled"]) if p is not None else True,
            "due": sum(1 for c in cards if c["active"] and c["days"] <= 0),
        })
    return groups


def mark_used(card_id, used_on, source="dashboard"):
    conn = db()
    conn.execute("UPDATE cards SET last_used=?, last_notified=NULL WHERE id=?", (used_on, card_id))
    conn.execute(
        "INSERT INTO usage_log(card_id, used_on, source, logged_at) VALUES(?,?,?,?)",
        (card_id, used_on, source, datetime.now().isoformat(timespec="minutes")),
    )
    plan_saved_card(conn, card_id)
    conn.commit()
    conn.close()


def card_line(v):
    who = f" ({v['owner']})" if v["owner"] else ""
    issuer = f"{v['issuer']} " if v["issuer"] else ""
    tail = f" ••{v['last4']}" if v["last4"] else ""
    return f"{issuer}{v['nickname']}{tail}{who}: {v['label']}"


# ------------------------------------------------------------ notifications
def used_url(card_id, quiet=False):
    base = app_address()
    if not base:
        return None
    return f"{base}/used/{card_id}/{get_setting('token')}" + ("?quiet=1" if quiet else "")


def notify_ntfy(url, title, message, card_id=None):
    """Publish to an ntfy topic. Returns (ok, message)."""
    ok, msg, _ = publish_ntfy(url, title, message, card_id)
    return ok, msg


def publish_ntfy(url, title, message, card_id=None):
    """Like notify_ntfy, and also returns the id ntfy gave the message (None if unknown)."""
    url = resolve_ntfy(url)
    p = urlparse(url)
    topic = p.path.strip("/")
    if p.scheme not in ("http", "https") or not p.netloc or not topic:
        return False, "ntfy URL should look like https://ntfy.sh/your-topic", None
    payload = {"topic": topic, "title": title, "message": message, "tags": ["credit_card"]}
    base = app_address()
    if base:
        payload["click"] = base + "/"
        if card_id is not None:
            payload["actions"] = [{
                "action": "http",
                "label": "Mark used",
                "url": used_url(card_id, quiet=True),
                "method": "POST",
                "clear": True,
            }]
    headers = {"Content-Type": "application/json"}
    if url.startswith(NTFY_INTERNAL + "/"):
        tok = ntfy_acct.publisher_token()  # the built-in server only accepts the app's own login
    else:
        tok = (get_setting("ntfy_token") or "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = Request(f"{p.scheme}://{p.netloc}", data=json.dumps(payload).encode(),
                  headers=headers, method="POST")
    try:
        with urlopen(req, timeout=15) as r:
            if 200 <= r.status < 300:
                try:
                    msg_id = json.loads(r.read().decode() or "{}").get("id")
                except (ValueError, AttributeError):
                    msg_id = None
                return True, "ntfy sent.", msg_id
            return False, f"ntfy returned HTTP {r.status}.", None
    except Exception as e:  # network errors and 4xx/5xx
        return False, f"ntfy failed: {e}", None


def confirm_stored(url, msg_id):
    """True if the built-in phone server still holds this message, False if it doesn't,
    None if the check itself failed. Only the built-in server can be read with the app's login."""
    topic = urlparse(resolve_ntfy(url)).path.strip("/")
    tok = ntfy_acct.publisher_token()
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        with urlopen(Request(f"{NTFY_INTERNAL}/{topic}/json?poll=1&since=10m", headers=headers),
                     timeout=10) as r:
            lines = r.read().decode().splitlines()
    except Exception:
        return None
    for line in lines:
        try:
            if json.loads(line).get("id") == msg_id:
                return True
        except ValueError:
            continue
    return False


def builtin_ref(topic):
    return BUILTIN_PREFIX + topic


def _version(text):
    m_ = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m_.groups()) if m_ else None


def local_ntfy_version():
    """Version of the ntfy program inside this image, or None."""
    try:
        out = subprocess.run(["ntfy", "--version"], capture_output=True, text=True, timeout=5).stdout
        return _version(out)
    except Exception:
        return None


def latest_ntfy_version():
    """Newest ntfy release on GitHub. Checked at most every 12 hours."""
    raw = get_setting("ntfy_latest_cache") or ""
    try:
        cache = json.loads(raw) if raw else {}
    except ValueError:
        cache = {}
    cached_version = _version(cache.get("version", ""))
    if cache and time.time() - cache.get("checked", 0) < 12 * 3600:
        return cached_version
    try:
        req = Request("https://api.github.com/repos/binwiederhier/ntfy/releases/latest",
                      headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "Credit-KeepAlive"})
        with urlopen(req, timeout=8) as r:
            tag = json.loads(r.read().decode()).get("tag_name", "")
        version = _version(tag)
        if version:
            cache = {"version": ".".join(map(str, version)), "checked": time.time()}
            set_setting("ntfy_latest_cache", json.dumps(cache))
            return version
    except Exception:
        pass
    # Couldn't reach GitHub: remember the attempt so pages don't wait on it every time.
    set_setting("ntfy_latest_cache", json.dumps({"version": cache.get("version", ""), "checked": time.time()}))
    return cached_version


def ntfy_update_notice():
    """Dict for the dashboard notice, or None when the image is up to date or unknown."""
    local, latest = local_ntfy_version(), latest_ntfy_version()
    if not local or not latest or latest <= local:
        return None
    return {"local": ".".join(map(str, local)), "latest": ".".join(map(str, latest))}


def is_builtin(url):
    return bool(url) and (url.startswith(BUILTIN_PREFIX) or url.startswith(LEGACY_PREFIX))


def topic_from(url):
    if not url:
        return ""
    if url.startswith(BUILTIN_PREFIX):
        return url[len(BUILTIN_PREFIX):]
    if url.startswith(LEGACY_PREFIX):
        return url[len(LEGACY_PREFIX):].strip("/")
    return urlparse(url).path.strip("/")


def to_display(url):
    """What forms show: just the topic name for built-in topics."""
    return topic_from(url) if is_builtin(url) else url


def to_stored(url):
    """What forms submit. A bare name means a topic on the built-in phone server.
    Full addresses such as https://ntfy.sh/topic are kept as typed."""
    url = (url or "").strip()
    if url.startswith(NTFY_INTERNAL + "/"):
        return builtin_ref(url[len(NTFY_INTERNAL) + 1:].strip("/"))
    if url and "://" not in url and "/" not in url and " " not in url:
        return builtin_ref(url)
    return url


def resolve_ntfy(url):
    """Turn a saved reference into the address to post to right now."""
    if url.startswith(BUILTIN_PREFIX):
        return f"{NTFY_INTERNAL}/{url[len(BUILTIN_PREFIX):]}"
    if url.startswith(LEGACY_PREFIX):
        return f"{NTFY_INTERNAL}/{url[len(LEGACY_PREFIX):]}"
    return url


def new_topic():
    """Hard-to-guess topic name. The topic name is the only secret on the built-in ntfy."""
    return "cr-" + secrets.token_urlsafe(18)


def phone_server_ready():
    """True if the built-in phone server answers its health check."""
    try:
        with urlopen(f"{NTFY_INTERNAL}/v1/health", timeout=3) as r:
            return bool(json.loads(r.read().decode()).get("healthy"))
    except Exception:
        return False


def notify_apprise(urls_text, title, body):
    """Send markdown through Apprise (email, Telegram, etc.). Returns (ok, message)."""
    ap = apprise.Apprise()
    for u in [l.strip() for l in urls_text.splitlines() if l.strip()]:
        if not ap.add(u):
            return False, f"Could not parse a URL starting {u.split('://')[0]}://"
    ok = ap.notify(title=title, body=body, body_format=apprise.NotifyFormat.MARKDOWN)
    return bool(ok), "Email/other sent." if ok else "Email/other sending failed."


def build_markdown(cards):
    lines = []
    for v in cards:
        line = f"• **{card_line(v)}**"
        link = used_url(v["id"])
        if link:
            line += f" — [Mark used]({link})"
        lines.append(line)
    base = app_address()
    if base:
        lines.append(f"[Open dashboard]({base}/)")
    title = "Time to use a card" if len(cards) == 1 else f"{len(cards)} cards are due"
    return title, "\n\n".join(lines)


def deliver(ntfy, appr, cards, label):
    """Send due cards to one route. Returns (ok, message) or None if the route has no channels."""
    results = []
    if ntfy.strip():
        for v in cards:  # one ntfy notification per card, each with its own button
            results.append(notify_ntfy(ntfy.strip(), f"Use {v['nickname']}", card_line(v), card_id=v["id"]))
    if appr.strip():
        title, body = build_markdown(cards)
        results.append(notify_apprise(appr, title, body))
    if not results:
        return None
    return all(ok for ok, _ in results), f"{label}: " + " ".join(m for _, m in results)


def send_test(ntfy, appr, label):
    title, body = f"Test for {label}", "If you can read this, notifications work."
    results = []
    stored = None
    if ntfy.strip():
        ok, msg, msg_id = publish_ntfy(ntfy.strip(), title, body)
        results.append((ok, msg))
        if ok and msg_id and resolve_ntfy(ntfy.strip()).startswith(NTFY_INTERNAL + "/"):
            stored = confirm_stored(ntfy.strip(), msg_id)
    if appr.strip():
        results.append(notify_apprise(appr, title, body))
    if not results:
        return None
    ok_all = all(ok for ok, _ in results)
    text = f"{label}: " + " ".join(m for _, m in results)
    if stored is True:
        text += " The phone server stored the test message."
    elif stored is False:
        text += " The phone server did not keep the test message, so check the phone server."
    elif stored is None and ntfy.strip() and resolve_ntfy(ntfy.strip()).startswith(NTFY_INTERNAL + "/"):
        text += " Could not check the phone server's stored messages."
    if ok_all and ntfy.strip():
        text += (" Check the phone within a minute. If nothing arrives, check that it's logged in with the "
                 "phone login and subscribed to this topic.")
    return ok_all, text


def run_check(force=False):
    """Route every due card to its owner's alerts, or to the default alerts.

    Owners with reminders turned off get nothing. Returns a summary dict.
    """
    today = date.today()
    renag = int(get_setting("renag_days") or 3)
    conn = db()
    last = {r["id"]: r["last_notified"] for r in conn.execute("SELECT id, last_notified FROM cards")}
    conn.close()
    people = {p["name"].lower(): p for p in people_rows()}

    due = []
    for v in all_card_views():
        if not v["active"] or v["days"] > 0:
            continue
        ln = last.get(v["id"])
        if not force and ln and (today - date.fromisoformat(ln)).days < renag:
            continue
        due.append(v)

    summary = {"due": len(due), "sent": 0, "failed": 0, "skipped": 0}
    if not due:
        return summary

    household = (get_setting("ntfy_url").strip(), get_setting("notify_urls"), "Default alerts")
    own_topics = topic_mode() == "person"
    routes = {}  # key -> (route, [cards])
    for v in due:
        p = people.get(v["owner"].lower()) if v["owner"] else None
        if p is not None and not p["enabled"]:
            summary["skipped"] += 1
            continue
        if p is not None and own_topics and (p["ntfy_url"].strip() or p["apprise_urls"].strip()):
            key = f"person:{p['id']}"
            # Without their own topic, phone alerts fall back to the household topic.
            route = (p["ntfy_url"].strip() or household[0], p["apprise_urls"], p["name"])
        elif p is not None and p["apprise_urls"].strip():
            # Household topic for the phones, plus this person's email or other alerts.
            key = f"person:{p['id']}"
            route = (household[0], p["apprise_urls"], p["name"])
        else:
            key = "household"
            route = household
        if not route[0] and not route[1].strip():
            summary["skipped"] += 1
            continue
        routes.setdefault(key, (route, []))[1].append(v)

    sent_ids = []
    for (ntfy, appr, label), cards in routes.values():
        res = deliver(ntfy, appr, cards, label)
        if res is None:
            summary["skipped"] += len(cards)
            continue
        ok, msg = res
        if ok:
            summary["sent"] += len(cards)
            sent_ids += [c["id"] for c in cards]
        else:
            summary["failed"] += len(cards)
            print(f"[notify] {msg}", flush=True)

    if sent_ids:
        conn = db()
        conn.executemany(
            "UPDATE cards SET last_notified=? WHERE id=?",
            [(today.isoformat(), i) for i in sent_ids],
        )
        conn.commit()
        conn.close()
    return summary


def scheduled_tick():
    """Runs every 15 minutes; does the daily check once after the notify hour."""
    now = datetime.now()
    hour = int(get_setting("notify_hour") or 9)
    if now.hour < hour:
        return
    today = now.date().isoformat()
    if get_setting("last_check") == today:
        return
    set_setting("last_check", today)
    run_check()


# ----------------------------------------------------------------- appearance
# The colour themes used across rcodehub's apps (same palettes as AutoExtract), plus this app's original look.
# Each theme has a light and a dark palette: bg, bg2 (bars and pills), card, text, muted, line, accent, accent2.
def _pal(bg, card, text, muted, line, accent, bg2=None):
    return dict(bg=bg, bg2=bg2 or line, card=card, text=text, muted=muted, line=line, accent=accent, accent2=accent)

APPEARANCE_THEMES = [
    ("default", "Credit-KeepAlive",
     _pal("#eef1f6", "#ffffff", "#162033", "#64708a", "#dde3ee", "#3b6cf6", bg2="#e2e7f0"),
     _pal("#0d1017", "#171c27", "#e9edf5", "#8e99b1", "#262e3f", "#6d8dff", bg2="#141924")),
    ("unraid", "Unraid orange",
     _pal("#f4f5f7", "#ffffff", "#1b1f24", "#667085", "#dde1e6", "#e8590c"),
     _pal("#111317", "#1a1d23", "#e6e8eb", "#8b94a3", "#2c313a", "#f76707")),
    ("midnight", "Midnight",
     _pal("#eef2fb", "#ffffff", "#14213d", "#5c6b8a", "#d5ddf0", "#3b82f6"),
     _pal("#0b1020", "#141b34", "#e6ebff", "#8a96bd", "#25305a", "#5aa2ff", bg2="#131a33")),
    ("nord", "Nord",
     _pal("#eceff4", "#ffffff", "#2e3440", "#5e6b81", "#d8dee9", "#5e81ac"),
     _pal("#2e3440", "#3b4252", "#eceff4", "#a3adc2", "#4c566a", "#88c0d0")),
    ("dracula", "Dracula",
     _pal("#f8f8f2", "#ffffff", "#282a36", "#6272a4", "#e1e1d8", "#7c4dff"),
     _pal("#282a36", "#343746", "#f8f8f2", "#a6aecc", "#44475a", "#bd93f9")),
    ("solarized", "Solarized",
     _pal("#fdf6e3", "#fffbf0", "#586e75", "#93a1a1", "#eee8d5", "#b58900"),
     _pal("#002b36", "#073642", "#93a1a1", "#657b83", "#0f4452", "#2aa198")),
    ("forest", "Forest",
     _pal("#eef3ec", "#ffffff", "#1d2b1f", "#607264", "#d3dfd0", "#2f9e44"),
     _pal("#0f1a12", "#17241a", "#e3efe4", "#8aa38d", "#264030", "#51cf66", bg2="#16261a")),
    ("rose", "Rosé",
     _pal("#fdf1f4", "#ffffff", "#3a1f2b", "#8a6572", "#f3d5de", "#e64980"),
     _pal("#1b1116", "#26181f", "#f6e7ec", "#b58b99", "#3d2530", "#f06595")),
    ("contrast", "High contrast",
     _pal("#ffffff", "#ffffff", "#000000", "#333333", "#000000", "#0044cc"),
     _pal("#000000", "#000000", "#ffffff", "#dddddd", "#ffffff", "#ffd400")),
    ("terminal", "Terminal",
     _pal("#f2f5ee", "#f8faf4", "#1a2a14", "#4b6a3c", "#9fb88f", "#2f7d12"),
     _pal("#050805", "#0a120a", "#7dff6a", "#3fa032", "#1f4d1a", "#39ff14")),
]
THEME_BY_ID = {t[0]: t for t in APPEARANCE_THEMES}

SHADOWS = {  # light, dark
    "none": ("none", "none"),
    "soft": ("0 10px 30px -12px rgba(22,32,51,.25)", "0 10px 30px -12px rgba(0,0,0,.6)"),
    "strong": ("0 18px 40px -10px rgba(22,32,51,.45)", "0 18px 40px -10px rgba(0,0,0,.85)"),
}
FONTS = {  # "" keeps the standard system font
    "": 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
    "rounded": 'ui-rounded,"SF Pro Rounded",Nunito,"Segoe UI",system-ui,sans-serif',
    "serif": 'Georgia,"Times New Roman",serif',
    "mono": "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
}
WIDTHS = {"standard": "1120px", "wide": "1400px", "full": "100%"}
APPEARANCE_CHOICES = {
    "look_mode": ("auto", "light", "dark"),
    "look_theme": tuple(THEME_BY_ID),
    "look_font": tuple(FONTS),
    "look_shadow": tuple(SHADOWS),
    "look_width": tuple(WIDTHS),
}


def current_look():
    """The saved look, with anything unknown or out of range put back to its default."""
    look = {}
    for key, default in APPEARANCE_DEFAULTS.items():
        value = get_setting(key)
        if key in APPEARANCE_CHOICES and value not in APPEARANCE_CHOICES[key]:
            value = default
        look[key] = value if value is not None else default
    try:
        look["look_radius"] = str(min(24, max(0, int(look["look_radius"]))))
    except (TypeError, ValueError):
        look["look_radius"] = APPEARANCE_DEFAULTS["look_radius"]
    return look


def look_css():
    """CSS that restyles the page for the saved theme. Goes after the main stylesheet, so it wins."""
    look = current_look()
    theme = THEME_BY_ID.get(look["look_theme"], APPEARANCE_THEMES[0])
    light, dark = theme[2], theme[3]
    shadow_light, shadow_dark = SHADOWS[look["look_shadow"]]

    def vars_(p, shadow):
        return ("--bg:%(bg)s;--bg2:%(bg2)s;--card:%(card)s;--text:%(text)s;--muted:%(muted)s;"
                "--line:%(line)s;--accent:%(accent)s;--accent2:%(accent2)s;--shadow:%(shadow)s;") % dict(p, shadow=shadow)

    light_vars = vars_(light, shadow_light)
    dark_vars = vars_(dark, shadow_dark)
    common = "--radius:%spx;--font:%s;--maxw:%s;" % (look["look_radius"], FONTS[look["look_font"]], WIDTHS[look["look_width"]])
    # Light is the base. Dark follows the device unless a mode is forced. The explicit mode rules come last.
    return (":root{" + light_vars + common + "}"
            "@media (prefers-color-scheme: dark){:root:not([data-mode=\"light\"]){" + dark_vars + "}}"
            ':root[data-mode="light"]{' + light_vars + "}"
            ':root[data-mode="dark"]{' + dark_vars + "}")


def look_attr():
    """Puts data-mode on the <html> tag when light or dark is forced."""
    mode = current_look()["look_mode"]
    return "" if mode == "auto" else ' data-mode="%s"' % mode


def appearance_themes():
    return [(t[0], t[1]) for t in APPEARANCE_THEMES]


# ----------------------------------------------------------------- templates
CSS = r"""
*{box-sizing:border-box}
[hidden]{display:none!important}
:root{
  --bg:#eef1f6; --bg2:#e2e7f0; --card:#ffffff; --text:#162033; --muted:#64708a; --line:#dde3ee;
  --accent:#3b6cf6; --accent2:#7c5cf6; --ok:#17a673; --soon:#e09a10; --due:#ea6a2c;
  --overdue:#e03b4f; --paused:#8a93a6; --shadow:0 10px 30px -12px rgba(22,32,51,.25); --radius:18px;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#0d1017; --bg2:#141924; --card:#171c27; --text:#e9edf5; --muted:#8e99b1; --line:#262e3f;
    --accent:#6d8dff; --accent2:#a78bfa; --shadow:0 10px 30px -12px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"]{
  --bg:#0d1017; --bg2:#141924; --card:#171c27; --text:#e9edf5; --muted:#8e99b1; --line:#262e3f;
  --accent:#6d8dff; --accent2:#a78bfa; --shadow:0 10px 30px -12px rgba(0,0,0,.6);
}
body{margin:0;min-height:100vh;color:var(--text);background:var(--bg);
  font:15px/1.5 var(--font,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif);
  background-image:radial-gradient(900px 500px at 0% -10%,rgba(59,108,246,.16),transparent 60%),
  radial-gradient(800px 500px at 110% 0%,rgba(124,92,246,.14),transparent 60%)}
a{color:var(--accent);text-decoration:none}
.wrap{max-width:var(--maxw,1120px);margin:0 auto;padding:0 16px}
main.wrap{padding-top:24px;padding-bottom:60px}
.top{position:sticky;top:0;z-index:10;backdrop-filter:blur(12px);background:var(--bg);border-bottom:1px solid var(--line)}
.topbar{display:flex;align-items:center;gap:12px;min-height:60px;flex-wrap:wrap;padding-top:8px;padding-bottom:8px}
.brand{font-weight:800;color:var(--text);display:flex;gap:8px;align-items:center;margin-right:auto;font-size:1.02rem}
.logo{font-size:20px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{padding:6px 11px;border-radius:10px;color:var(--muted);font-weight:600}
nav a.on,nav a:hover{background:var(--card);color:var(--text)}
h1{font-size:clamp(1.5rem,3vw,2rem);margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:1.1rem;margin:0}
.meta{color:var(--muted);font-size:.9rem}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:18px}
.stack{display:flex;flex-direction:column;gap:16px}
.toast{background:var(--text);color:var(--bg);padding:12px 16px;border-radius:12px;margin-bottom:16px;font-weight:600;box-shadow:var(--shadow)}
.hero{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;flex-wrap:wrap;margin-bottom:18px}
.next-up{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-radius:var(--radius);padding:14px 18px;min-width:230px;box-shadow:var(--shadow)}
.next-up .meta{color:rgba(255,255,255,.8)}
.nu-name{font-size:1.2rem;font-weight:700;margin:2px 0 8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;display:flex;flex-direction:column}
.stat .num{font-size:1.7rem;font-weight:800;letter-spacing:-.02em}
.stat .lbl{color:var(--muted);font-size:.85rem}
.stat.warn .num{color:var(--due)}
.stat.bad .num{color:var(--overdue)}
.chips{display:flex;gap:8px;margin-bottom:22px;flex-wrap:wrap}
.chip{padding:6px 14px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-weight:600;font-size:.9rem}
.chip.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.owner{margin-bottom:32px}
.owner-head{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.owner-head .meta{margin-top:2px}
.owner-head .pills{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap}
.avatar{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:800;color:#fff;
  background:linear-gradient(135deg,var(--accent),var(--accent2));flex:none}
.grid-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:18px}
.cardbox{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px}
.cardbox.is-paused{opacity:.6}
.cardinfo{display:flex;flex-direction:column;gap:8px;padding:2px 4px 4px}
.row{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}
.bar{height:8px;border-radius:999px;background:var(--bg2);overflow:hidden}
.bar span{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--ok),var(--soon) 70%,var(--overdue))}
.status{display:inline-block;padding:3px 10px;border-radius:999px;color:#fff;font-size:.78rem;font-weight:700}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.78rem;font-weight:700;background:var(--bg2);color:var(--muted)}
.pill.ok{background:rgba(23,166,115,.15);color:var(--ok)}
.pill.off{background:rgba(138,147,166,.2);color:var(--paused)}
.s-ok{background:var(--ok)} .s-soon{background:var(--soon)} .s-due{background:var(--due)}
.s-overdue{background:var(--overdue)} .s-paused{background:var(--paused)}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.actions input[type=date]{width:auto}
.stack-col{display:flex;flex-direction:column;gap:12px}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:11px;padding:9px 14px;cursor:pointer;
  background:var(--text);color:var(--bg);display:inline-flex;align-items:center;gap:6px;font-size:.92rem}
.btn.primary,button.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.btn.small,button.small{padding:7px 12px;font-size:.88rem}
.ghost{background:transparent;color:var(--text);border:1px solid var(--line);border-radius:11px;padding:8px 12px;
  font-weight:600;font-size:.9rem;cursor:pointer;font-family:inherit;display:inline-block}
button.ghost{font:inherit}
.danger{background:transparent;color:var(--overdue);border:1px solid var(--line);border-radius:11px;padding:8px 12px;font:inherit;font-weight:600;cursor:pointer}
details.other summary,details.chan summary{cursor:pointer;color:var(--accent);font-size:.88rem;font-weight:600}
input,textarea,select{font:inherit;width:100%;padding:10px 12px;border-radius:11px;border:1px solid var(--line);background:var(--bg);color:var(--text)}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
label{display:block;font-size:.85rem;color:var(--muted);font-weight:600}
label input,label textarea,label select{margin-top:4px;color:var(--text);font-weight:400}
.form{display:flex;flex-direction:column;gap:14px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:640px){.two{grid-template-columns:1fr}}
.form-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,340px);gap:24px;align-items:start}
@media (max-width:860px){.form-grid{grid-template-columns:1fr}}
fieldset.themes{border:0;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}
legend{font-size:.85rem;color:var(--muted);font-weight:600;padding:0;margin-bottom:6px}
.swatch{position:relative;display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:12px;padding:8px;cursor:pointer;color:var(--text);font-weight:600;font-size:.88rem;margin:0}
.swatch input{position:absolute;opacity:0;pointer-events:none;margin:0;width:auto}
.swatch:has(input:checked){border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.sw{width:28px;height:19px;border-radius:5px;flex:none}
.check{display:flex;gap:8px;align-items:center;font-weight:600;color:var(--text)}
.check input{width:auto;margin:0}
.person{display:flex;flex-direction:column;gap:12px}
.person.off{opacity:.75}
.who{display:flex;gap:12px;align-items:center}
.name{font-weight:700}
.empty{text-align:center;padding:40px 18px}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:9px 6px;border-top:1px solid var(--line)}
th{color:var(--muted);font-weight:600;border-top:0}
.auth{max-width:380px;margin:8vh auto 0;text-align:center}
.auth .form{text-align:left;margin-top:14px}
.auth-logo{font-size:34px}
/* Credit card visual */
.ccard{position:relative;aspect-ratio:1.586/1;border-radius:18px;padding:18px 20px;color:#fff;overflow:hidden;
  display:flex;flex-direction:column;justify-content:space-between;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 14px 26px -12px rgba(0,0,0,.5)}
.ccard::before{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(120% 90% at 100% 0%,rgba(255,255,255,.28),transparent 55%)}
.ccard::after{content:"";position:absolute;width:280px;height:280px;border-radius:50%;right:-130px;bottom:-160px;background:rgba(255,255,255,.08)}
.ccard-top,.ccard-num,.ccard-bot{position:relative;z-index:1}
.ccard-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.issuer{font-weight:800;letter-spacing:.02em;font-size:1rem;text-shadow:0 1px 2px rgba(0,0,0,.25);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chipart{width:36px;height:27px;border-radius:6px;flex:none;background:linear-gradient(135deg,#f5e1a4,#b8862b);box-shadow:inset 0 0 0 1px rgba(0,0,0,.2)}
.ccard-num{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.14em;font-size:clamp(.95rem,2.4vw,1.15rem);text-shadow:0 1px 2px rgba(0,0,0,.3);white-space:nowrap}
.ccard-bot{display:flex;justify-content:space-between;align-items:flex-end;gap:10px}
.ccard-name{font-weight:700;font-size:1rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ccard-owner{font-size:.8rem;opacity:.85}
.ccard-due{font-size:.76rem;font-weight:700;background:rgba(0,0,0,.25);padding:4px 10px;border-radius:999px;white-space:nowrap}
.ccard.has-art{background-color:#1b2030;background-size:cover;background-position:center}
.ccard.has-art::before{background:linear-gradient(180deg,rgba(0,0,0,.05),rgba(0,0,0,.6))}
.ccard.has-art::after{display:none}
.ccard.art-only .ccard-top,.ccard.art-only .ccard-num,.ccard.art-only .ccard-name,.ccard.art-only .ccard-owner,.ccard.art-only .ccard-due{display:none}
.theme-graphite{background:linear-gradient(135deg,#4a5366,#141821)}
.theme-ocean{background:linear-gradient(135deg,#2c8cf0,#0c3576)}
.theme-ember{background:linear-gradient(135deg,#ff8a4c,#9a2e12)}
.theme-forest{background:linear-gradient(135deg,#36b67c,#0d4a33)}
.theme-violet{background:linear-gradient(135deg,#9b75ff,#3a1c86)}
.theme-gold{background:linear-gradient(135deg,#f0c75e,#8e6512);color:#21190a}
.theme-gold .chipart{background:linear-gradient(135deg,#fff3c4,#9c7422)}
.theme-rose{background:linear-gradient(135deg,#ff6fae,#8c1d55)}
.theme-slate{background:linear-gradient(135deg,#8a96aa,#2c3443)}
.notice{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--soon);border-radius:14px;padding:12px 16px;margin-bottom:18px;box-shadow:var(--shadow);font-size:.92rem}
.notice b{color:var(--text)}
/* Settings pages: side list plus cards, theme menu in the header */
.page-head{margin-bottom:18px}
.page-head .meta{margin:4px 0 0;max-width:62ch}
.page-head a{font-weight:600}
.split{display:grid;grid-template-columns:190px minmax(0,1fr);gap:24px;align-items:start}
.split>.stack{min-width:0}
.side{position:sticky;top:84px;display:flex;flex-direction:column;gap:2px}
.side a{padding:9px 12px;border-radius:11px;color:var(--muted);font-weight:600;font-size:.92rem}
.side a:hover{background:var(--card);color:var(--text)}
@media (max-width:820px){
  .split{grid-template-columns:1fr}
  .side{position:static;flex-direction:row;flex-wrap:wrap;gap:6px}
  .side a{background:var(--card);border:1px solid var(--line);padding:7px 11px}
}
.sec-head{display:flex;flex-direction:column;gap:4px;margin-bottom:16px}
.sec-head h2{font-size:1.15rem}
.sec-head p{margin:0}
.subpanel{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;flex-direction:column;gap:12px}
.subpanel h3{margin:0;font-size:1rem}
.subpanel p{margin:0}
.notice p{margin:0}
label .meta{display:block;font-weight:400;margin-top:3px;line-height:1.4}
.group{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--bg)}
.group summary{cursor:pointer}
.steps{display:flex;flex-direction:column}
.step{display:grid;grid-template-columns:34px minmax(0,1fr);gap:14px;padding:20px 0;border-top:1px solid var(--line)}
.steps .step:first-child{border-top:0;padding-top:4px}
.step-num{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;font-weight:800;color:#fff;
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 6px 14px -8px var(--accent)}
.step-body{display:flex;flex-direction:column;gap:14px;min-width:0}
.step-body h3{margin:0;font-size:1rem}
.step-body .form{gap:12px}
.tips{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.tip{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;flex-direction:column;gap:4px;font-size:.9rem}
.tip p{margin:0}
.kv{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:4px 14px}
.kv div{display:grid;grid-template-columns:110px minmax(0,1fr);gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.kv div:first-child{border-top:0}
.kv code{word-break:break-all}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.tile{position:relative;display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:14px;padding:14px;
  cursor:pointer;color:var(--text);font-weight:400;font-size:.92rem;margin:0;background:var(--bg)}
.tile input{width:auto;margin:3px 0 0;flex:none}
.tile span{display:flex;flex-direction:column;gap:2px}
.tile:has(input:checked){border-color:var(--accent);box-shadow:0 0 0 2px var(--accent);background:var(--card)}
.plain{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:6px;font-size:.92rem}
.more{border-top:1px solid var(--line);padding-top:14px;margin-top:6px}
.more summary{cursor:pointer;color:var(--accent);font-weight:600}
.more[open] summary{margin-bottom:14px}
.theme-menu{position:relative}
.theme-menu summary{list-style:none;cursor:pointer}
.theme-menu summary::-webkit-details-marker{display:none}
.theme-panel{position:absolute;right:0;top:calc(100% + 10px);width:min(360px,calc(100vw - 32px));z-index:30;
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:16px}
.theme-panel h2{margin-bottom:2px}
.theme-panel .form{gap:10px}
.theme-panel label{font-size:.8rem}
@media (max-width:640px){
  .theme-menu{position:static}
  .theme-panel{left:12px;right:12px;width:auto}
}
.busy{display:flex;flex-direction:column;gap:8px}
.busy-bar{height:8px;border-radius:999px;background:var(--bg2);overflow:hidden;position:relative}
.busy-bar span{position:absolute;top:0;bottom:0;width:35%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2));animation:busy-slide 1.3s ease-in-out infinite}
@keyframes busy-slide{0%{left:-35%}100%{left:100%}}
@media (prefers-reduced-motion:reduce){.busy-bar span{animation-duration:3s}}
.pick-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.pick-item{border:1px solid var(--line);border-radius:14px;padding:10px;background:var(--bg);display:flex;flex-direction:column;gap:8px;min-width:0}
.pick-item img{width:100%;aspect-ratio:1.586/1;object-fit:contain;background:var(--card);border-radius:10px}
.pick-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-item select{font-size:.85rem}
.confirm-box{max-width:340px;margin:12vh auto 0;text-align:center}
"""

BASE = r"""<!doctype html>
<html lang="en"{{ look_attr|safe }}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{% block title %}Credit-KeepAlive{% endblock %}</title>
<style>{{ css|safe }}</style><style>{{ look_css|safe }}</style>
</head>
<body>
<header class="top"><div class="wrap topbar">
  <a class="brand" href="{{ url_for('index') }}"><span class="logo">💳</span>Credit-KeepAlive</a>
  {% if session.get('auth') %}
  <nav>
    <a href="{{ url_for('index') }}" class="{{ 'on' if page == 'dashboard' }}">Cards</a>
    <a href="{{ url_for('people_page') }}" class="{{ 'on' if page == 'people' }}">People</a>
    <a href="{{ url_for('history_page') }}" class="{{ 'on' if page == 'history' }}">History</a>
    <a href="{{ url_for('card_settings_page') }}" class="{{ 'on' if page == 'card_settings' }}">Card settings</a>
    <a href="{{ url_for('settings_page') }}" class="{{ 'on' if page == 'settings' }}">Settings</a>
  </nav>
  <a class="ghost small" href="{{ url_for('card_import') }}">Import</a>
  <a class="btn primary small" href="{{ url_for('card_new') }}">+ Add card</a>
  <details class="theme-menu">
    <summary class="ghost">Theme</summary>
    <div class="theme-panel">
      <form method="post" action="{{ url_for('settings_appearance') }}" class="form">
        <input type="hidden" name="next" value="{{ request.full_path }}">
        <h2>Look</h2>
        <p class="meta">Saved on the server, so every device shows the same look.</p>
        <label>Light or dark
          <select name="look_mode">
            <option value="auto" {{ 'selected' if look.look_mode == 'auto' }}>Follow my device</option>
            <option value="light" {{ 'selected' if look.look_mode == 'light' }}>Light</option>
            <option value="dark" {{ 'selected' if look.look_mode == 'dark' }}>Dark</option>
          </select>
        </label>
        <label>Colour theme
          <select name="look_theme">{% for tid, tname in look_themes %}<option value="{{ tid }}" {{ 'selected' if look.look_theme == tid }}>{{ tname }}</option>{% endfor %}</select>
        </label>
        <div class="two">
          <label>Corner rounding (0 to 24)
            <input name="look_radius" type="number" min="0" max="24" value="{{ look.look_radius }}">
          </label>
          <label>Font
            <select name="look_font">
              <option value="" {{ 'selected' if look.look_font == '' }}>Standard</option>
              <option value="rounded" {{ 'selected' if look.look_font == 'rounded' }}>Rounded</option>
              <option value="serif" {{ 'selected' if look.look_font == 'serif' }}>Serif</option>
              <option value="mono" {{ 'selected' if look.look_font == 'mono' }}>Monospace</option>
            </select>
          </label>
        </div>
        <div class="two">
          <label>Shadows
            <select name="look_shadow">
              <option value="none" {{ 'selected' if look.look_shadow == 'none' }}>None</option>
              <option value="soft" {{ 'selected' if look.look_shadow == 'soft' }}>Soft</option>
              <option value="strong" {{ 'selected' if look.look_shadow == 'strong' }}>Strong</option>
            </select>
          </label>
          <label>Page width
            <select name="look_width">
              <option value="standard" {{ 'selected' if look.look_width == 'standard' }}>Standard</option>
              <option value="wide" {{ 'selected' if look.look_width == 'wide' }}>Wide</option>
              <option value="full" {{ 'selected' if look.look_width == 'full' }}>Full width</option>
            </select>
          </label>
        </div>
        <div class="actions">
          <button class="primary small" type="submit">Save look</button>
          <button class="ghost" type="submit" name="reset" value="1">Reset</button>
        </div>
      </form>
    </div>
  </details>
  <form method="post" action="{{ url_for('logout') }}"><button class="ghost small" type="submit">Log out</button></form>
  {% endif %}
</div></header>
<main class="wrap">
{% with messages = get_flashed_messages() %}{% for m in messages %}<div class="toast">{{ m }}</div>{% endfor %}{% endwith %}
{% block content %}{% endblock %}
</main>
<footer class="wrap meta" style="padding:18px 0 28px;text-align:center">Credit-KeepAlive version {{ app_version }}</footer>
</body>
</html>
"""

MACROS = r"""
{% macro cardface(c) -%}
<div class="ccard theme-{{ c.theme }}{{ ' has-art' if c.image }}{{ ' art-only' if c.image and c.art_only }}"{% if c.image %} style="background-image:url('{{ c.image }}')"{% endif %}>
  <div class="ccard-top"><span class="issuer">{{ c.issuer or 'Credit card' }}</span><span class="chipart"></span></div>
  <div class="ccard-num">•••• •••• •••• {{ c.last4 or '••••' }}</div>
  <div class="ccard-bot">
    <div style="min-width:0"><div class="ccard-name">{{ c.nickname or 'Card name' }}</div><div class="ccard-owner">{{ c.owner or 'Unassigned' }}</div></div>
    <div class="ccard-due">{{ c.label }}</div>
  </div>
</div>
{%- endmacro %}
"""

DASHBOARD = r"""{% extends 'base.html' %}
{% from 'macros.html' import cardface %}
{% block title %}Cards · Credit-KeepAlive{% endblock %}
{% block content %}
{% if ntfy_update %}
<div class="notice"><b>Developer notice:</b> ntfy {{ ntfy_update.latest }} is out, and this build includes {{ ntfy_update.local }}.
  Rebuild Credit-KeepAlive to pick it up.</div>
{% endif %}
<div class="hero">
  <div>
    <h1>Your cards</h1>
    <p class="meta">{{ stats.active }} active · {{ stats.due }} due now{% if stats.paused %} · {{ stats.paused }} paused{% endif %}</p>
  </div>
  {% if stats.next %}
  <div class="next-up">
    <div class="meta">Use next</div>
    <div class="nu-name">{{ stats.next.issuer }} {{ stats.next.nickname }}{% if stats.next.last4 %} ••{{ stats.next.last4 }}{% endif %}</div>
    <span class="status s-{{ stats.next.status }}">{{ stats.next.label }}</span>
  </div>
  {% endif %}
</div>
<div class="stats">
  <div class="stat"><span class="num">{{ stats.active }}</span><span class="lbl">Active cards</span></div>
  <div class="stat warn"><span class="num">{{ stats.due }}</span><span class="lbl">Due now</span></div>
  <div class="stat bad"><span class="num">{{ stats.overdue }}</span><span class="lbl">Overdue</span></div>
  <div class="stat"><span class="num">{{ people_count }}</span><span class="lbl">People</span></div>
</div>
<div class="chips">
  <a class="chip {{ 'on' if filter == 'all' }}" href="{{ url_for('index', filter='all') }}">All</a>
  <a class="chip {{ 'on' if filter == 'due' }}" href="{{ url_for('index', filter='due') }}">Due</a>
  <a class="chip {{ 'on' if filter == 'paused' }}" href="{{ url_for('index', filter='paused') }}">Paused</a>
</div>

{% for g in groups %}
<section class="owner">
  <header class="owner-head">
    <div class="avatar">{{ g.name[:1]|upper }}</div>
    <div>
      <h2>{{ g.name }}</h2>
      <div class="meta">{{ g.cards|length }} card{{ '' if g.cards|length == 1 else 's' }}{% if g.due %} · <b style="color:var(--due)">{{ g.due }} due</b>{% endif %}</div>
    </div>
    <div class="pills">
      {% if g.is_person %}
        {% if g.enabled %}<span class="pill ok">Reminders on</span>{% else %}<span class="pill off">Reminders off</span>{% endif %}
      {% endif %}
    </div>
  </header>
  <div class="grid-cards">
  {% for c in g.cards %}
    <article class="cardbox {{ 'is-paused' if not c.active }}">
      {{ cardface(c) }}
      <div class="cardinfo">
        <div class="row">
          <span class="status s-{{ c.status }}">{{ c.label }}</span>
          <span class="meta">Next {{ c.next_due }}</span>
        </div>
        <div class="bar" title="{{ c.pct }}% of this cycle used"><span style="width: {{ c.pct }}%"></span></div>
        <div class="meta">Every {{ c.interval_days }} days · last used {{ c.last_used or 'never' }}</div>
        <div class="actions">
          {% if c.active %}
          <form method="post" action="{{ url_for('used', card_id=c.id) }}"><button class="primary small" type="submit">Mark used today</button></form>
          <details class="other"><summary>Other date</summary>
            <form method="post" action="{{ url_for('used', card_id=c.id) }}" class="actions" style="margin-top:8px">
              <input type="date" name="used_on" value="{{ today }}" max="{{ today }}">
              <button class="ghost small" type="submit">Save</button>
            </form>
          </details>
          {% endif %}
          <a class="ghost" href="{{ url_for('card_edit', card_id=c.id) }}">Edit</a>
          <a class="ghost" href="{{ url_for('history_page', card=c.id) }}">History</a>
        </div>
      </div>
    </article>
  {% endfor %}
  </div>
</section>
{% else %}
<div class="panel empty">
  <h2>No cards here yet</h2>
  <p class="meta">Add a card and pick a color. You can group cards by owner and choose who gets reminders.</p>
  <a class="btn primary" href="{{ url_for('card_new') }}">Add your first card</a>
</div>
{% endfor %}
{% endblock %}
"""

CARD_FORM = r"""{% extends 'base.html' %}
{% from 'macros.html' import cardface %}
{% block title %}{{ 'Edit card' if editing else 'Add card' }} · Credit-KeepAlive{% endblock %}
{% block content %}
<h1>{{ 'Edit card' if editing else 'Add a card' }}</h1>
<p class="meta" style="margin-bottom:18px">Only store the last 4 digits. Never enter full card numbers.</p>
<div class="form-grid">
  <form id="cardform" method="post" enctype="multipart/form-data" class="panel form">
    <div class="two">
      <label>Issuer
        <select name="issuer" id="issuer">
          <option value="">Choose an issuer</option>
          {% for name in issuers %}<option value="{{ name }}" {{ 'selected' if name|lower == (card.issuer or '')|lower }}>{{ name }}</option>{% endfor %}
          <option value="__new__">+ Add a new issuer</option>
        </select>
      </label>
      <label>Card
        <select name="card_type" id="card_type">
          <option value="">Choose a card</option>
          {% for t in card_types %}<option value="{{ t.name }}" data-issuer="{{ t.issuer }}" data-image="{{ t.image or '' }}" data-generic="{{ '1' if t.generic else '' }}" {{ 'selected' if t.selected }}>{{ t.name }}</option>{% endfor %}
          <option value="__new__">+ Add a new card</option>
        </select>
      </label>
    </div>
    <div class="two">
      <label id="new-issuer-box" hidden>New issuer name
        <input name="new_issuer" maxlength="60" placeholder="e.g. Local Credit Union" autocomplete="off">
      </label>
      <label id="new-card-box" hidden>New card name
        <input name="new_card_type" maxlength="80" placeholder="e.g. Sapphire Preferred" autocomplete="off">
      </label>
    </div>
    <div class="two">
      <label>Owner
        <input name="owner" list="owners" value="{{ card.owner }}" placeholder="Mom" autocomplete="off">
      </label>
      <label>Last 4 digits
        <input name="last4" maxlength="4" pattern="\d{0,4}" inputmode="numeric" value="{{ card.last4 }}" placeholder="1234">
      </label>
    </div>
    <datalist id="owners">{% for p in people %}<option value="{{ p.name }}">{% endfor %}</datalist>
    <fieldset class="themes" style="border:0;padding:0;margin:0">
      <legend>Card color (used when there is no card image)</legend>
      {% for key, name in themes %}
      <label class="swatch">
        <input type="radio" name="theme" value="{{ key }}" {{ 'checked' if card.theme == key }}>
        <span class="sw theme-{{ key }}"></span><span>{{ name }}</span>
      </label>
      {% endfor %}
    </fieldset>
    <label>Card image <span class="meta">(optional. PNG, JPG, GIF or WEBP, up to 4 MB. Every card of this type uses it.)</span>
      <input type="file" name="card_image" id="card_image" accept="image/png,image/jpeg,image/gif,image/webp">
    </label>
    <div class="two">
      <label>Use every (days)
        <input name="interval_days" type="number" min="1" value="{{ card.interval_days }}" required list="interval-presets">
        <datalist id="interval-presets">
          <option value="14" label="Every 2 weeks"></option>
          <option value="21" label="Every 3 weeks"></option>
          <option value="30" label="Every month"></option>
          <option value="60" label="Every 2 months"></option>
          <option value="90" label="Every 3 months"></option>
        </datalist>
      </label>
      <label>Notes
        <input name="notes" value="{{ card.notes }}" placeholder="optional">
      </label>
    </div>
    <label class="check"><input type="checkbox" name="active" value="1" {{ 'checked' if card.active }}> Include in rotation</label>
    <div class="actions">
      <button class="primary" type="submit">{{ 'Save changes' if editing else 'Add card' }}</button>
      <a class="ghost" href="{{ url_for('index') }}">Cancel</a>
    </div>
  </form>
  <div>
    <div class="meta" style="margin-bottom:8px">Preview</div>
    <div id="preview">{{ cardface(preview) }}</div>
  </div>
</div>
{% if editing %}
<form method="post" action="{{ url_for('card_delete', card_id=card_id) }}" style="margin-top:18px"
      onsubmit="return confirm('Delete this card and its history?');">
  <button class="danger" type="submit">Delete card</button>
</form>
{% endif %}
<script>
(function () {
  var f = document.getElementById('cardform');
  var p = document.querySelector('#preview .ccard');
  var issuer = document.getElementById('issuer');
  var cardSel = document.getElementById('card_type');
  var newIssuer = document.getElementById('new-issuer-box');
  var newCard = document.getElementById('new-card-box');
  var file = document.getElementById('card_image');
  var picked = null;  // preview of a newly chosen image
  function val(n) { return f.elements[n] ? f.elements[n].value.trim() : ''; }
  function selectedOption() { return cardSel.options[cardSel.selectedIndex]; }

  // Show only the cards for the chosen issuer. Choosing "add new" hides the list.
  function filterCards() {
    var iss = issuer.value;
    var adding = iss === '__new__';
    Array.prototype.forEach.call(cardSel.options, function (o) {
      if (!o.value || o.value === '__new__') return;
      var show = !adding && (iss === '' || (o.getAttribute('data-issuer') || '').toLowerCase() === iss.toLowerCase());
      o.hidden = !show;
      if (!show && o.selected) cardSel.value = '';
    });
    newIssuer.hidden = !adding;
    newCard.hidden = cardSel.value !== '__new__';
  }

  function currentIssuer() { return issuer.value === '__new__' ? val('new_issuer') : issuer.value; }
  function currentName() { return cardSel.value === '__new__' ? val('new_card_type') : cardSel.value; }
  function art() {
    if (picked) return picked;
    var o = selectedOption();
    return (cardSel.value && o && o.getAttribute('data-image')) || '';
  }

  function update() {
    p.querySelector('.issuer').textContent = currentIssuer() || 'Credit card';
    p.querySelector('.ccard-name').textContent = currentName() || 'Card name';
    p.querySelector('.ccard-owner').textContent = val('owner') || 'Unassigned';
    var l = val('last4');
    p.querySelector('.ccard-num').textContent = '•••• •••• •••• ' + (l || '••••');
    var t = f.querySelector('input[name=theme]:checked');
    var a = art();
    var generic = !picked && selectedOption() && selectedOption().getAttribute('data-generic') === '1';
    p.className = 'ccard theme-' + (t ? t.value : 'graphite') + (a ? ' has-art' : '') + (a && generic ? ' art-only' : '');
    p.style.backgroundImage = a ? "url('" + a + "')" : '';
  }

  file.addEventListener('change', function () {
    var pick = file.files && file.files[0];
    if (!pick) { picked = null; update(); return; }
    var r = new FileReader();
    r.onload = function () { picked = r.result; update(); };
    r.readAsDataURL(pick);
  });
  issuer.addEventListener('change', function () { filterCards(); update(); });
  cardSel.addEventListener('change', function () { filterCards(); update(); });
  f.addEventListener('input', update);
  f.addEventListener('change', update);
  filterCards();
  update();
})();
</script>
{% endblock %}
"""

CARD_IMPORT = r"""{% extends 'base.html' %}
{% block title %}Import cards · Credit-KeepAlive{% endblock %}
{% block content %}
<h1>Import cards</h1>
{% if stage == 'upload' %}
<p class="meta" style="margin-bottom:18px">Add many cards at once from a CSV file. You'll check the list before anything is saved.</p>
<form method="post" enctype="multipart/form-data" class="panel form" style="max-width:640px">
  <label>CSV file
    <input type="file" name="csv_file" accept=".csv,text/csv" required>
  </label>
  <div class="actions">
    <button class="primary" type="submit">Check file</button>
    <a class="ghost" href="{{ url_for('card_import_sample') }}">Download a sample</a>
  </div>
</form>
<section class="panel" style="margin-top:18px;max-width:640px">
  <h2>File format</h2>
  <p class="meta">One card per line, with these columns: <code>Issuer,Card,Last4,Owner</code>.
    The first line can be that header row. Only <b>Card</b> is required.</p>
  <pre style="white-space:pre-wrap;font-size:.88rem">Issuer,Card,Last4,Owner
Chase,Sapphire Preferred,1234,Mom
American Express,Blue Cash Everyday,5678,Dad</pre>
  <ul class="meta">
    <li><b>Last4</b> is the last four digits only. A longer number is rejected and never saved.</li>
    <li>Issuers and cards that aren't on the list yet are added to it.</li>
    <li>Owners are added to People. Each new card gets your default days between uses and the graphite color.</li>
    <li>Cards already saved with the same issuer, card, last 4 and owner are skipped.</li>
    <li>Up to 500 cards per file, and the file can be at most 256 KB.</li>
  </ul>
</section>
{% else %}
<p class="meta" style="margin-bottom:12px">Check each line. Only the rows marked <b>Will add</b> are saved.</p>
<div class="panel" style="overflow-x:auto">
<table>
  <tr><th>Line</th><th>Issuer</th><th>Card</th><th>Last 4</th><th>Owner</th><th>Result</th></tr>
  {% for e in entries %}
  <tr>
    <td>{{ e.line }}</td>
    <td>{{ e.issuer or '—' }}</td>
    <td>{{ e.card or '—' }}</td>
    <td>{{ e.last4 or '—' }}</td>
    <td>{{ e.owner or '—' }}</td>
    <td>{% if e.status == 'add' %}<span class="pill ok">Will add</span>{% elif e.status == 'duplicate' %}<span class="pill">Skipped</span>{% else %}<span class="pill off">{{ e.message }}</span>{% endif %}
      {% if e.status == 'duplicate' %}<span class="meta">{{ e.message }}</span>{% endif %}</td>
  </tr>
  {% else %}
  <tr><td colspan="6" class="meta">The file has no card lines.</td></tr>
  {% endfor %}
</table>
</div>
<form method="post" action="{{ url_for('card_import_confirm') }}" style="margin-top:16px">
  <input type="hidden" name="rows" value="{{ rows_json }}">
  <div class="actions">
    <button class="primary" type="submit" {{ 'disabled' if not to_add }}>Add {{ to_add }} card{{ '' if to_add == 1 else 's' }}</button>
    <a class="ghost" href="{{ url_for('card_import') }}">Choose a different file</a>
  </div>
</form>
{% endif %}
{% endblock %}
"""

CARD_PICTURES = r"""{% extends 'base.html' %}
{% block title %}Check pictures · Credit-KeepAlive{% endblock %}
{% block content %}
<h1>Check card pictures</h1>
<p class="meta" style="margin-bottom:12px">Each file is matched to a card by its name. Check the picks, change any that are wrong,
  and choose Skip for files you don't want. Nothing is saved until you click <b>Save pictures</b>.</p>
{% for p in problems %}<div class="toast">{{ p }}</div>{% endfor %}
<form method="post" action="{{ url_for('card_pictures_confirm') }}">
  <input type="hidden" name="token" value="{{ token }}">
  <div class="panel" style="overflow-x:auto">
  <table>
    <tr><th>Picture</th><th>File</th><th>Card</th><th>How it was matched</th></tr>
    {% for e in entries %}
    <tr>
      <td><img src="{{ url_for('staged_picture', token=token, idx=e.idx) }}" alt="" style="width:110px;border-radius:10px;display:block"></td>
      <td class="meta">{{ e.name }}</td>
      <td>
        <select name="pick_{{ e.idx }}" data-pick="{{ e.idx }}" style="min-width:230px">
          <option value="">Skip</option>
          {% for o in options %}<option value="{{ o.val }}" {{ 'selected' if o.val == e.pick }}>{{ o.label }}</option>{% endfor %}
        </select>
        <div id="new-{{ e.idx }}" hidden style="margin-top:8px;display:grid;gap:6px">
          <input name="new_issuer_{{ e.idx }}" maxlength="60" placeholder="Issuer, e.g. Chase" value="{{ e.new_issuer }}">
          <input name="new_card_{{ e.idx }}" maxlength="80" placeholder="Card name, e.g. Sapphire Reserve" value="{{ e.new_card }}">
        </div>
      </td>
      <td class="meta">{{ e.note }}</td>
    </tr>
    {% endfor %}
  </table></div>
  <div class="actions" style="margin-top:14px">
    <button class="primary" type="submit">Save pictures</button>
    <a class="ghost" href="{{ url_for('settings_page') }}">Cancel</a>
  </div>
</form>
<script>
(function () {
  document.querySelectorAll('select[data-pick]').forEach(function (sel) {
    var box = document.getElementById('new-' + sel.getAttribute('data-pick'));
    function sync() { box.hidden = sel.value !== '__new__'; }
    sel.addEventListener('change', sync);
    sync();
  });
})();
</script>
{% endblock %}
"""

PEOPLE = r"""{% extends 'base.html' %}
{% block title %}People · Credit-KeepAlive{% endblock %}
{% block content %}
<div class="hero">
  <div>
    <h1>People</h1>
    <p class="meta">Each person can get reminders on their phone or by email. Turn someone's reminders off without deleting them.
    Cards whose owner hasn't set up alerts use the default alerts in Settings.</p>
  </div>
</div>
<div class="stack">
{% for p in people %}
<section class="panel person {{ '' if p.enabled else 'off' }}">
  <div class="row">
    <div class="who">
      <div class="avatar">{{ p.name[:1]|upper }}</div>
      <div>
        <div class="name">{{ p.name }}</div>
        <div class="meta">{{ p.card_count }} card{{ '' if p.card_count == 1 else 's' }} · {{ 'Reminders on' if p.enabled else 'Reminders off' }}
          · {% if topic_mode == 'household' %}{{ 'household topic' }}{% if p.apprise_urls %} and own alerts{% endif %}{% elif p.ntfy_url or p.apprise_urls %}own alerts set up{% else %}uses default alerts{% endif %}</div>
      </div>
    </div>
    <div class="actions">
      <form method="post" action="{{ url_for('person_toggle', person_id=p.id) }}">
        <button class="ghost" type="submit">{{ 'Turn reminders off' if p.enabled else 'Turn reminders on' }}</button>
      </form>
      <a class="ghost" href="{{ url_for('card_new', owner=p.name) }}">+ Add card</a>
    </div>
  </div>
  <details class="chan">
    <summary>Alerts</summary>
    <div class="stack-col" style="margin-top:12px">
      {% if p.builtin %}
      {% if topic_mode == 'household' %}<div class="meta">Phone alerts go to the household topic. To give this person their own topic, choose "Own topic per person" in Settings.</div>{% endif %}
      {% if p.builtin %}<div class="meta">Own phone topic <code>{{ p.topic }}</code>{% if topic_mode == 'person' %}. Subscribe to this topic in the ntfy app on their phone.{% endif %}</div>{% endif %}
      {% endif %}
      <form method="post" action="{{ url_for('person_topic', person_id=p.id) }}">
        <button class="ghost small" type="submit">{{ 'New phone topic' if p.builtin else 'Create a phone topic' }}</button>
      </form>
    </div>
    <form method="post" action="{{ url_for('person_update', person_id=p.id) }}" class="form" style="margin-top:12px">
      <label>Phone topic
        <input name="ntfy_url" value="{{ p.ntfy_display }}" placeholder="Create one above, or paste an ntfy.sh topic">
      </label>
      <label>Email and other alerts, one per line
        <textarea name="apprise_urls" rows="2" placeholder="mailto://user:app-password@gmail.com?to=them@example.com">{{ p.apprise_urls }}</textarea>
      </label>
      <div class="actions"><button class="primary small" type="submit">Save alerts</button></div>
    </form>
    <div class="actions" style="margin-top:12px">
      <form method="post" action="{{ url_for('person_test', person_id=p.id) }}"><button class="ghost small" type="submit">Send test</button></form>
      <form method="post" action="{{ url_for('person_delete', person_id=p.id) }}"
            onsubmit='return confirm({{ ("Remove " ~ p.name ~ "? Their cards stay.")|tojson }});'>
        <button class="danger" type="submit">Remove person</button>
      </form>
    </div>
  </details>
</section>
{% else %}
<div class="panel empty"><p class="meta">No people yet. Add one below, or give a card an owner.</p></div>
{% endfor %}
<form method="post" action="{{ url_for('person_add') }}" class="panel actions">
  <input name="name" placeholder="Name, e.g. Mom" required style="max-width:280px">
  <button class="primary" type="submit">Add person</button>
</form>
</div>
{% endblock %}
"""

HISTORY = r"""{% extends 'base.html' %}
{% block title %}History · Credit-KeepAlive{% endblock %}
{% block content %}
<h1>History</h1>
<p class="meta" style="margin-bottom:18px">Every logged use. Undo removes an entry and recalculates the card's last-used date.</p>
<div class="stack">
  <section class="panel">
    <h2 style="margin-bottom:10px">By card</h2>
    <table>
      <tr><th>Card</th><th>Owner</th><th>Uses logged</th><th>Last used</th></tr>
      {% for s in summary %}
      <tr>
        <td><a href="{{ url_for('history_page', card=s.id) }}">{{ s.issuer }} {{ s.nickname }}</a></td>
        <td>{{ s.owner or 'Unassigned' }}</td>
        <td>{{ s.n }}</td><td>{{ s.last or 'never' }}</td>
      </tr>
      {% endfor %}
    </table>
  </section>
  <section class="panel">
    <div class="row" style="margin-bottom:10px">
      <h2>{{ 'Log for ' ~ filter_name if filter_name else 'Recent uses' }}</h2>
      {% if filter_name %}<a class="meta" href="{{ url_for('history_page') }}">Show all cards</a>{% endif %}
    </div>
    {% if not entries %}<p class="meta">Nothing logged yet.</p>{% else %}
    <table>
      <tr><th>Date used</th><th>Card</th><th>How</th><th>Logged</th><th></th></tr>
      {% for e in entries %}
      <tr>
        <td>{{ e.used_on }}</td>
        <td>{{ e.nickname }}</td>
        <td>{{ 'Notification link' if e.source == 'link' else 'Dashboard' }}</td>
        <td class="meta">{{ (e.logged_at or '').replace('T', ' ') }}</td>
        <td>
          <form method="post" action="{{ url_for('delete_entry', entry_id=e.id) }}"
                onsubmit="return confirm('Remove this entry and recalculate the last-used date?');">
            <button class="danger" type="submit">Undo</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% endif %}
    <p class="meta" style="margin-top:10px">Showing the latest 200 entries.</p>
  </section>
</div>
{% endblock %}
"""

SETTINGS = r"""{% extends 'base.html' %}
{% block title %}Settings · Credit-KeepAlive{% endblock %}
{% block content %}
<div class="page-head">
  <h1>Settings</h1>
  <p class="meta">Reminders, phone alerts and your login. Card-related settings are on <a href="{{ url_for('card_settings_page') }}">Card settings</a>.</p>
</div>

<div class="split">
  <nav class="side" aria-label="Settings sections">
    <a href="#reminders">Reminders</a>
    <a href="#phone">Phone alerts</a>
    <a href="#email">Email and other alerts</a>
    <a href="#login">Login and password</a>
    <a href="#tests">Tests</a>
  </nav>

  <div class="stack">

    <section id="reminders" class="panel">
      <div class="sec-head">
        <h2>Reminders</h2>
        <p class="meta">When reminders go out, and how long to wait before repeating a card that is still due.</p>
      </div>
      <form method="post" action="{{ url_for('settings_save') }}" class="form">
        <div class="two">
          <label>Send reminders at (hour, 0 to 23)
            <input name="notify_hour" type="number" min="0" max="23" value="{{ s.notify_hour }}">
          </label>
          <label>Remind again after (days)
            <input name="renag_days" type="number" min="1" value="{{ s.renag_days }}">
          </label>
        </div>
        <div class="actions"><button class="primary" type="submit">Save reminders</button></div>
      </form>
    </section>

    <section id="phone" class="panel">
      <div class="sec-head">
        <h2>Phone alerts</h2>
        <p class="meta">Reminders with a <b>Mark used</b> button arrive on your phones through the free <b>ntfy</b> app.
          Phone server:
          {% if ntfy.up %}<span class="pill ok">ready</span>{% else %}<span class="pill" style="color:var(--overdue)">not responding</span>. If this stays red, restart the container.{% endif %}</p>
      </div>

      <div class="steps">
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-body">
            <h3>Address the phones connect to</h3>
            <form method="post" action="{{ url_for('settings_addresses') }}" class="form">
              <label>Phone server address <span class="meta">Leave blank to use {{ phone_guess }}{% if guess_is_name %}. You opened this app by name, so enter the address yourself{% endif %}.</span>
                <input name="phone_address" value="{{ phone_saved }}" placeholder="{{ phone_guess }}">
              </label>
              <label>Web address of this app <span class="meta">Used by the Mark used buttons. Filled in the first time you log in.</span>
                <input name="app_address" value="{{ app_saved or app_now }}" placeholder="http://192.168.1.50:8686">
              </label>
              <div class="actions"><button class="ghost" type="submit">Save addresses</button></div>
            </form>
            <div class="tips">
              <div class="tip">
                <b>On your home network</b>
                <p class="meta">Use <code>http://</code>, your server's IP address and port <code>2586</code>, such as <code>http://192.168.1.50:2586</code>. Phones away from home need a VPN such as Tailscale.</p>
              </div>
              <div class="tip">
                <b>Cloudflare Tunnel or Nginx Proxy Manager</b>
                <p class="meta">Use <code>https://</code> and your hostname <b>with no port</b>, such as <code>https://ntfy.example.com</code>. The proxy handles the port, so don't add <code>:2586</code>. Point the hostname at port 2586 and turn on WebSocket support.</p>
              </div>
            </div>
          </div>
        </div>

        <div class="step">
          <div class="step-num">2</div>
          <div class="step-body">
            <h3>Phone login</h3>
            <p class="meta">Every phone uses this one login. It can read reminders but can't send them.</p>
            <div class="kv">
              <div><span class="meta">Username</span><code>phone</code></div>
              <div><span class="meta">Password</span><code>{{ ntfy_logins.phone or 'not created yet, restart the container' }}</code></div>
            </div>
            <form method="post" action="{{ url_for('settings_ntfy_login', user='phone') }}" class="form"
                  onsubmit="return confirm('Change the phone password? Every phone will need the new password.');">
              <label>Your own password <span class="meta">8 to 64 characters, or leave blank to generate one</span>
                <input name="password" type="text" minlength="8" maxlength="64" autocomplete="off" spellcheck="false" placeholder="e.g. smith-family-alerts">
              </label>
              <div class="actions"><button class="ghost" type="submit">Save phone password</button></div>
            </form>
          </div>
        </div>

        <div class="step">
          <div class="step-num">3</div>
          <div class="step-body">
            <h3>Where alerts go</h3>
            <form method="post" action="{{ url_for('settings_topic_mode') }}" class="form">
              <div class="tiles">
                <label class="tile">
                  <input type="radio" name="topic_mode" value="household" {{ 'checked' if topic_mode == 'household' }}>
                  <span><b>One household topic</b><span class="meta">Every phone gets every reminder. New phones need only one topic.</span></span>
                </label>
                <label class="tile">
                  <input type="radio" name="topic_mode" value="person" {{ 'checked' if topic_mode == 'person' }}>
                  <span><b>Own topic per person</b><span class="meta">Each person's reminders go to their own topic, set on the People page.</span></span>
                </label>
              </div>
              <div class="actions"><button class="ghost" type="submit">Save choice</button></div>
            </form>
            <form method="post" action="{{ url_for('settings_save') }}" class="form">
              <label>Household topic <span class="meta">Also used for people with no topic of their own.</span>
                <input name="ntfy_url" value="{{ s.ntfy_url }}" placeholder="Create one below, or paste an ntfy.sh topic">
              </label>
              <div class="actions"><button class="ghost" type="submit">Save topic</button></div>
            </form>
            <form method="post" action="{{ url_for('settings_topic') }}">
              <button class="ghost" type="submit">{{ 'Create a new household topic' if ntfy.builtin else 'Create a household topic' }}</button>
            </form>
          </div>
        </div>

        <div class="step">
          <div class="step-num">4</div>
          <div class="step-body">
            <h3>Set up each phone</h3>
            <ol class="plain">
              <li>Install the free <b>ntfy</b> app from the App Store or Google Play.</li>
              <li>In the app, add a user for the server: the <b>phone server address</b> from step 1, username <code>phone</code>, and the password from step 2. Menu names can differ slightly between app versions.</li>
              <li>Subscribe: tap <b>+</b>, choose that server, and enter the topic <code>{{ s.ntfy_url or 'the household topic' }}</code>{% if topic_mode == 'person' %}, or the person's own topic from the People page{% endif %}.</li>
              <li>Send a test message (see Tests below). It should appear on the phone.</li>
            </ol>
          </div>
        </div>
      </div>

      <details class="more">
        <summary>Advanced: admin login and access token</summary>
        <div class="stack-col">
          <p class="meta">The admin login opens the ntfy web page at <code>{{ phone or 'your phone server address' }}</code>. Only you need it.</p>
          <div class="kv">
            <div><span class="meta">Username</span><code>admin</code></div>
            <div><span class="meta">Password</span><code>{{ ntfy_logins.admin or 'not created yet, restart the container' }}</code></div>
          </div>
          <form method="post" action="{{ url_for('settings_ntfy_login', user='admin') }}" class="form"
                onsubmit="return confirm('Change the admin password? You will need it to log in to the ntfy web page.');">
            <label>Your own admin password <span class="meta">8 to 64 characters, or leave blank to generate one</span>
              <input name="password" type="text" minlength="8" maxlength="64" autocomplete="off" spellcheck="false" placeholder="e.g. my-ntfy-admin">
            </label>
            <div class="actions"><button class="ghost" type="submit">Save admin password</button></div>
          </form>
          <form method="post" action="{{ url_for('settings_save') }}" class="form">
            <label>Access token <span class="meta">Only if your ntfy server requires one{% if token_set %}. One is saved; leave blank to keep it{% endif %}.</span>
              <input name="ntfy_token" type="password" autocomplete="off">
            </label>
            <div class="actions"><button class="ghost" type="submit">Save access token</button></div>
          </form>
        </div>
      </details>
    </section>

    <section id="email" class="panel">
      <div class="sec-head">
        <h2>Email and other alerts</h2>
        <p class="meta">Sent for cards with no owner, and for people who have an email set up on the People page. Put one address on each line, such as <code>mailto://yourname:app-password@gmail.com?to=you@example.com</code>.</p>
      </div>
      <form method="post" action="{{ url_for('settings_save') }}" class="form">
        <label>Alert addresses
          <textarea name="notify_urls" rows="3" placeholder="mailto://user:app-password@gmail.com?to=you@example.com">{{ s.notify_urls }}</textarea>
        </label>
        <div class="actions"><button class="primary" type="submit">Save alerts</button></div>
      </form>
    </section>

    <section id="login" class="panel">
      <div class="sec-head">
        <h2>Login and password</h2>
        <p class="meta">The password for this app. Changing it signs out every other browser.</p>
      </div>
      <form method="post" action="{{ url_for('change_password') }}" class="form">
        <div class="two">
          <label>Current password<input name="current" type="password" autocomplete="current-password" required></label>
          <label>New password (8+ characters)<input name="new" type="password" minlength="8" autocomplete="new-password" required></label>
        </div>
        <div class="actions"><button type="submit">Change password</button></div>
      </form>
    </section>

    <section id="tests" class="panel">
      <div class="sec-head">
        <h2>Tests</h2>
        <p class="meta">A test message goes to the household topic and email alerts. Sending due reminders now ignores the repeat delay.</p>
      </div>
      <div class="actions">
        <form method="post" action="{{ url_for('settings_test') }}"><button class="ghost" type="submit">Send test message</button></form>
        <form method="post" action="{{ url_for('settings_check') }}"><button class="ghost" type="submit">Send due reminders now</button></form>
      </div>
    </section>

  </div>
</div>
{% endblock %}
"""

CARD_SETTINGS = r"""{% extends 'base.html' %}
{% block title %}Card settings · Credit-KeepAlive{% endblock %}
{% block content %}
<div class="page-head">
  <h1>Card settings</h1>
  <p class="meta">How often cards come due, their pictures, and the issuers and cards you can pick from. Phone and email alerts are on <a href="{{ url_for('settings_page') }}">Settings</a>.</p>
</div>

<div class="split">
  <nav class="side" aria-label="Card settings sections">
    <a href="#due">Due dates</a>
    <a href="#pictures">Card pictures</a>
    <a href="#list">Issuers and cards</a>
  </nav>

  <div class="stack">

    <section id="due" class="panel">
      <div class="sec-head">
        <h2>Due dates</h2>
        <p class="meta">How long a card can wait between uses, and how due dates are spread out.</p>
      </div>
      <form method="post" action="{{ url_for('card_settings_save') }}" class="form">
        <div class="two">
          <label>Default days between uses <span class="meta">Used for new cards unless a card sets its own.</span>
            <input name="default_interval" type="number" min="1" value="{{ s.default_interval }}">
          </label>
          <label>Spread due dates by up to (percent of the cycle) <span class="meta">Cards on the same schedule don't all fall on one day. 0 turns this off.</span>
            <input name="stagger_percent" type="number" min="0" max="50" value="{{ s.stagger_percent }}">
          </label>
        </div>
        <div class="actions"><button class="primary" type="submit">Save due dates</button></div>
      </form>
      <form method="post" action="{{ url_for('settings_spread') }}" class="subpanel" style="margin-top:14px">
        <h3>Spread due dates now</h3>
        <p class="meta">Use this after changing several cards at once. It only moves due dates within the spread percent above, so nothing is due later than its cycle.</p>
        <div class="actions"><button class="ghost" type="submit">Spread due dates now</button></div>
      </form>
    </section>

    <section id="pictures" class="panel">
      <div class="sec-head">
        <h2>Card pictures</h2>
        <p class="meta">Each card shows its picture. A card without one gets a generic picture: a coloured card with its issuer and name.</p>
      </div>
      {% if pictures_missing %}
      <div class="notice">
        <b>{{ pictures_missing|length }} card picture{{ '' if pictures_missing|length == 1 else 's' }} missing.</b>
        The app can't find {{ 'it' if pictures_missing|length == 1 else 'them' }} in <code>{{ picture_folder }}</code>.
        In Unraid this is the <code>card-images</code> folder inside your app data folder. If you moved to a new app data folder, copy the whole folder across, or import your picture backup ZIP below.
        Missing: {{ pictures_missing[:12]|join(', ') }}{{ ' and more' if pictures_missing|length > 12 }}.
      </div>
      {% elif pictures_saved %}
      <p class="meta">{{ pictures_saved }} card picture{{ '' if pictures_saved == 1 else 's' }} saved, all found. Files are named after the card, for example <code>chase-sapphire-preferred.png</code>, so you can replace one by copying a file with that name into <code>{{ picture_folder }}</code>.</p>
      {% endif %}

      <div class="two">
        <form method="post" action="{{ url_for('card_pictures') }}" enctype="multipart/form-data" class="subpanel">
          <h3>Upload pictures</h3>
          <label>Picture files <span class="meta">Use the files as the issuer named them. Choose several at once, then check each match before saving.</span>
            <input type="file" name="pictures" multiple accept="image/png,image/jpeg,image/gif,image/webp,.zip">
          </label>
          <label>Or a whole folder <span class="meta">Every picture inside is used. A ZIP of pictures works too.</span>
            <input type="file" name="pictures" webkitdirectory>
          </label>
          <div class="actions"><button class="ghost" type="submit">Upload pictures</button></div>
        </form>

        <form method="post" action="{{ url_for('card_pictures_scrape') }}" class="subpanel" data-busy
              data-steps="Opening the page…|Looking through the page for card pictures…|Downloading the matching pictures. This can take up to 30 seconds.">
          <h3>Find pictures on an issuer's page</h3>
          <label>Page address <span class="meta">Only pictures that name a card in your list are used, and you still check each one. Pages that load pictures with scripts won't work.</span>
            <input type="url" name="url" maxlength="500" placeholder="https://creditcards.chase.com/travel-credit-cards" required>
          </label>
          <div class="actions"><button class="ghost" type="submit">Find pictures</button></div>
          <div class="busy" hidden>
            <div class="busy-bar"><span></span></div>
            <p class="meta" data-busy-text aria-live="polite">Opening the page…</p>
          </div>
        </form>
      </div>

      <div class="two">
        <div class="subpanel">
          <h3>Back up and restore pictures</h3>
          <p class="meta">Export saves every card picture in one ZIP file. On a new installation, import that ZIP and each picture goes straight to its card, with no review.</p>
          <div class="actions"><a class="ghost" href="{{ url_for('card_pictures_export') }}">Export pictures (ZIP)</a></div>
          <form method="post" action="{{ url_for('card_pictures_import') }}" enctype="multipart/form-data" class="form">
            <label>Import a pictures ZIP <input type="file" name="pack" accept=".zip"></label>
            <label class="check"><input type="checkbox" name="replace" value="1"> Replace pictures that are already saved</label>
            <div class="actions"><button class="ghost" type="submit">Import pictures</button></div>
          </form>
        </div>

        <form method="post" action="{{ url_for('settings_default_pictures') }}" class="subpanel">
          <h3>Generic pictures</h3>
          <p class="meta">Redraws the generic picture for every card that uses one, including ones made by older versions. Your own pictures are not changed.</p>
          <div class="actions">
            <button class="ghost" type="submit">Redraw generic pictures</button>
            {% if real_art_url %}<a class="ghost" href="{{ real_art_url }}" target="_blank" rel="noopener">Download real card art (zip)</a>{% endif %}
          </div>
        </form>
      </div>
    </section>

    <section id="list" class="panel">
      <div class="sec-head">
        <h2>Issuers and cards</h2>
        <p class="meta">The issuers and cards offered in the Add card dropdowns. Removing one only takes it off the list. Cards you already saved are not changed.</p>
      </div>
      <div class="actions" style="margin-bottom:14px">
        <a class="ghost" href="{{ url_for('card_import') }}">Import cards from a CSV</a>
      </div>
      <div class="stack-col">
        {% for g in catalog %}
        <details class="group">
          <summary><b>{{ g.issuer }}</b> <span class="meta">({{ g.cards|length }} card{{ '' if g.cards|length == 1 else 's' }})</span></summary>
          <div class="stack-col" style="margin-top:12px">
            {% for c in g.cards %}
            <div class="row">
              <span>{{ c.name }}</span>
              <form method="post" action="{{ url_for('remove_card') }}">
                <input type="hidden" name="issuer" value="{{ g.issuer }}">
                <input type="hidden" name="name" value="{{ c.name }}">
                <button class="danger" type="submit">Remove card</button>
              </form>
            </div>
            {% else %}
            <p class="meta">No cards in this group yet.</p>
            {% endfor %}
            <form method="post" action="{{ url_for('remove_issuer') }}"
                  onsubmit='return confirm({{ ("Remove " ~ g.issuer ~ " and all of its cards from the list? Saved cards are not changed.")|tojson }});'>
              <input type="hidden" name="issuer" value="{{ g.issuer }}">
              <button class="danger" type="submit">Remove issuer</button>
            </form>
          </div>
        </details>
        {% else %}
        <p class="meta">The list is empty. Add a card from the Add card page to start it.</p>
        {% endfor %}
      </div>
    </section>

  </div>
</div>
<script>
document.querySelectorAll('form[data-busy]').forEach(function (f) {
  f.addEventListener('submit', function () {
    var box = f.querySelector('.busy'), txt = f.querySelector('[data-busy-text]');
    var btn = f.querySelector('button[type=submit]');
    var steps = (f.getAttribute('data-steps') || '').split('|');
    if (box) box.hidden = false;
    if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
    var t0 = Date.now();
    setInterval(function () {
      var s = Math.floor((Date.now() - t0) / 1000);
      var i = Math.min(steps.length - 1, Math.floor(s / 8));
      if (txt) txt.textContent = (steps[i] || 'Working…') + ' (' + s + ' s)';
    }, 500);
  });
});
</script>
{% endblock %}
"""

CARD_PICK = r"""{% extends 'base.html' %}
{% block title %}Choose pictures · Credit-KeepAlive{% endblock %}
{% block content %}
<div class="page-head">
  <h1>Choose pictures</h1>
  <p class="meta">The search couldn't match any pictures to your cards, so here is every picture on
    <a href="{{ page_url }}" target="_blank" rel="noopener noreferrer">the page you searched</a>.
    Tick the ones you want and choose the card for each one.</p>
</div>
{% for p in problems %}<div class="notice"><p>{{ p }}</p></div>{% endfor %}
{% if items %}
<form method="post" action="{{ url_for('card_pictures_pick_save') }}" class="stack" data-busy
      data-steps="Downloading the chosen pictures…|Still downloading. Large pictures take longer.">
  <input type="hidden" name="token" value="{{ token }}">
  <div class="pick-grid">
    {% for it in items %}
    <div class="pick-item">
      <img src="{{ it.url }}" alt="{{ it.desc or it.name }}" loading="lazy" referrerpolicy="no-referrer">
      <div class="meta pick-name" title="{{ it.desc or it.name }}">{{ it.desc or it.name }}</div>
      <label class="check"><input type="checkbox" name="keep" value="{{ loop.index0 }}"> Use this picture</label>
      <select name="card_{{ loop.index0 }}" aria-label="Card for this picture">
        <option value="">Choose a card…</option>
        {% for o in options %}<option value="{{ o.val }}">{{ o.label }}</option>{% endfor %}
      </select>
    </div>
    {% endfor %}
  </div>
  <div class="busy" hidden>
    <div class="busy-bar"><span></span></div>
    <p class="meta" data-busy-text aria-live="polite">Downloading the chosen pictures…</p>
  </div>
  <div class="actions">
    <button class="primary" type="submit">Review and save selected</button>
    <a class="ghost" href="{{ url_for('card_settings_page') }}">Cancel</a>
  </div>
</form>
{% else %}
<p class="meta">This page has no pictures to choose from. Try a page that shows the cards, or upload the pictures instead.</p>
{% endif %}
<script>
document.querySelectorAll('form[data-busy]').forEach(function (f) {
  f.addEventListener('submit', function () {
    var box = f.querySelector('.busy'), txt = f.querySelector('[data-busy-text]');
    var btn = f.querySelector('button[type=submit]');
    var steps = (f.getAttribute('data-steps') || '').split('|');
    if (box) box.hidden = false;
    if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
    var t0 = Date.now();
    setInterval(function () {
      var s = Math.floor((Date.now() - t0) / 1000);
      var i = Math.min(steps.length - 1, Math.floor(s / 8));
      if (txt) txt.textContent = (steps[i] || 'Working…') + ' (' + s + ' s)';
    }, 500);
  });
});
</script>
{% endblock %}
"""

AUTH = r"""{% extends 'base.html' %}
{% block title %}{{ heading }} · Credit-KeepAlive{% endblock %}
{% block content %}
<div class="auth panel">
  <div class="auth-logo">💳</div>
  <h1>{{ heading }}</h1>
  {% if intro %}<p class="meta">{{ intro }}</p>{% endif %}
  <form method="post" class="form">
    <label>Password
      <input name="password" type="password" required autofocus
             autocomplete="{{ 'new-password' if confirm else 'current-password' }}" {% if confirm %}minlength="8"{% endif %}>
    </label>
    {% if confirm %}
    <label>Confirm password
      <input name="confirm" type="password" required minlength="8" autocomplete="new-password">
    </label>
    {% endif %}
    <button class="btn primary" type="submit">{{ button }}</button>
  </form>
</div>
{% endblock %}
"""

CONFIRM = r"""<!doctype html>
<html lang="en"{{ look_attr|safe }}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Mark used · Credit-KeepAlive</title>
<style>{{ css|safe }}</style><style>{{ look_css|safe }}</style><style>
body{display:grid;place-items:center}
</style></head>
<body>
<div class="panel confirm-box">
  {% if done %}
    <div style="font-size:38px;color:var(--ok)">✓</div>
    <p><b>{{ name }}</b> marked as used today.</p>
  {% else %}
    <p>Mark <b>{{ name }}</b> as used today?</p>
    <form method="post"><button class="primary" type="submit" style="margin-top:6px">Yes, mark used</button></form>
  {% endif %}
  <p style="margin-top:14px"><a href="{{ url_for('index') }}">Open dashboard</a></p>
</div>
</body></html>
"""

TEMPLATES = {
    "base.html": BASE,
    "macros.html": MACROS,
    "dashboard.html": DASHBOARD,
    "card_form.html": CARD_FORM,
    "card_import.html": CARD_IMPORT,
    "card_pictures.html": CARD_PICTURES,
    "people.html": PEOPLE,
    "history.html": HISTORY,
    "settings.html": SETTINGS,
    "card_settings.html": CARD_SETTINGS,
    "card_pictures_pick.html": CARD_PICK,
    "auth.html": AUTH,
    "confirm.html": CONFIRM,
}


# --------------------------------------------------------------------- web
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")

app = Flask(__name__)
app.jinja_loader = DictLoader(TEMPLATES)
init_db()
try:
    ensure_default_pictures()
except OSError:
    pass  # the data folder isn't writable yet; pictures can be restored from Settings later
app.secret_key = get_setting("secret_key")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BASE_URL", "").lower().startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
)

LAST4_RE = re.compile(r"^\d{0,4}$")
MIN_PASSWORD = 8

# Reachable without login. used_link is guarded by its secret token instead.
PUBLIC_ENDPOINTS = {"login", "setup", "used_link", "healthz", "static"}

def client_ip():
    """The address to count failures against.

    Forwarded headers are believed only when TRUST_PROXY=1 is set (a reverse proxy or Cloudflare Tunnel
    is in front of the app) AND the connection itself comes from a private address. Otherwise anyone can
    send a fake X-Forwarded-For header and get a fresh count on every guess, so the real connection
    address is used.
    """
    peer = request.remote_addr or "?"
    if not TRUST_PROXY:
        return peer
    try:
        private = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
    except ValueError:
        private = False
    if private:
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            value = (request.headers.get(header) or "").split(",")[0].strip()
            try:
                ipaddress.ip_address(value)
                return value
            except ValueError:
                continue
    return peer


class Brake:
    """Counts failures per address. After `limit` failures inside `window` seconds, that address is blocked."""

    def __init__(self, limit, window):
        self.limit, self.window, self.data = limit, window, {}

    def blocked(self, key):
        count, first = self.data.get(key, (0, 0))
        if time.time() - first >= self.window:
            self.data.pop(key, None)
            return False
        return count >= self.limit

    def fail(self, key):
        count, first = self.data.get(key, (0, time.time()))
        self.data[key] = (count + 1, first)

    def clear(self, key):
        self.data.pop(key, None)


_login_brake = Brake(limit=5, window=300)       # wrong passwords: 5 minutes
_link_brake = Brake(limit=10, window=600)       # wrong Mark used tokens: 10 in 10 minutes


def _locked(ip):
    return _login_brake.blocked(ip)


def _record_failure(ip):
    _login_brake.fail(ip)


def _safe_next(target):
    """Only same-site paths. Browsers treat a backslash or a control character as a slash, so refuse those too."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target \
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in target):
        p = urlparse(target)
        if not p.scheme and not p.netloc:
            return target
    return url_for("index")


def csrf_token():
    """The token every form must send back. Created once per browser session."""
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


# Posts that come from outside the browser session. The Mark used link is guarded by its own secret token.
CSRF_EXEMPT = {"used_link"}
_FORM_RE = re.compile(r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)', re.I)


@app.before_request
def check_csrf():
    if request.method != "POST" or request.endpoint in CSRF_EXEMPT or request.endpoint is None:
        return None
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf") or ""
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(400, description="That form expired or came from another site. Go back, reload the page and try again.")
    return None


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"  # pages show card and alert details, and the phone logins
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; "
                            "form-action 'self'")
    return resp


@app.before_request
def gate():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not get_setting("password_hash"):
        return redirect(url_for("setup"))
    if not session.get("auth") or session.get("epoch") != get_setting("session_epoch"):
        session.pop("auth", None)
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if not get_setting("app_address") and not os.environ.get("BASE_URL"):
        set_setting("app_address", seen_address())
    return None


APP_VERSION = os.environ.get("APP_VERSION", "dev").strip() or "dev"


@app.context_processor
def _app_version():
    """Shown in the footer. The build sets it (see the Dockerfile); a local run shows 'dev'."""
    return {"app_version": APP_VERSION}


@app.context_processor
def _look_menu():
    """The theme menu in the header needs the saved look on every page."""
    if not session.get("auth"):
        return {}
    return {"look_themes": appearance_themes(), "look": {k: get_setting(k) for k in APPEARANCE_DEFAULTS}}


def _render(template, **ctx):
    html = render_template(template, css=CSS, look_css=look_css(), look_attr=look_attr(), **ctx)
    hidden = f'<input type="hidden" name="csrf_token" value="{csrf_token()}">'
    return _FORM_RE.sub(lambda m_: m_.group(1) + hidden, html)


# -- auth
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if get_setting("password_hash"):
        return redirect(url_for("login"))
    if request.method == "POST":
        pw, conf = request.form.get("password", ""), request.form.get("confirm", "")
        if len(pw) < MIN_PASSWORD:
            flash(f"Use at least {MIN_PASSWORD} characters.")
        elif pw != conf:
            flash("Passwords did not match.")
        else:
            set_setting("password_hash", generate_password_hash(pw))
            session.permanent = True
            session["auth"] = True
            session["epoch"] = get_setting("session_epoch")
            return redirect(url_for("index"))
    return _render("auth.html", page="setup", heading="Set a password",
                   intro="First-time setup. This password protects the dashboard. "
                         "Mark-used links in notifications keep working without it.",
                   confirm=True, button="Save password")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not get_setting("password_hash"):
        return redirect(url_for("setup"))
    ip = client_ip()
    if request.method == "POST":
        if _locked(ip):
            flash("Too many attempts. Wait a few minutes and try again.")
        elif check_password_hash(get_setting("password_hash"), request.form.get("password", "")):
            _login_brake.clear(ip)
            session.permanent = True
            session["auth"] = True
            session["epoch"] = get_setting("session_epoch")
            return redirect(_safe_next(request.args.get("next")))
        else:
            _record_failure(ip)
            flash("Wrong password.")
    return _render("auth.html", page="login", heading="Credit-KeepAlive", intro="",
                   confirm=False, button="Log in")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.post("/password")
def change_password():
    if not check_password_hash(get_setting("password_hash"), request.form.get("current", "")):
        flash("Current password was wrong.")
    elif len(request.form.get("new", "")) < MIN_PASSWORD:
        flash(f"New password needs at least {MIN_PASSWORD} characters.")
    else:
        set_setting("password_hash", generate_password_hash(request.form["new"]))
        epoch = secrets.token_hex(12)
        set_setting("session_epoch", epoch)  # every other signed-in browser must log in again
        session["epoch"] = epoch
        flash("Password changed. Other browsers have been signed out.")
    return redirect(url_for("settings_page"))


@app.route("/healthz")
def healthz():
    return "ok"


# -- cards
@app.route("/")
def index():
    filter_name = request.args.get("filter", "all")
    if filter_name not in ("all", "due", "paused"):
        filter_name = "all"
    views = all_card_views()
    return _render(
        "dashboard.html", page="dashboard", filter=filter_name,
        stats=dashboard_stats(views), groups=grouped_views(filter_name, views),
        people_count=len(people_rows()), today=date.today().isoformat(),
        ntfy_update=ntfy_update_notice(),
    )


NEW_CHOICE = "__new__"


def _pick(f, choice_field, new_field):
    """The dropdown value, or the typed name when '+ Add new' was chosen."""
    choice = f.get(choice_field, "").strip()
    if choice == NEW_CHOICE:
        return f.get(new_field, "").strip()
    return "" if choice in ("", NEW_CHOICE) else choice


def _form_values():
    f = request.form
    try:
        interval = max(1, int(f.get("interval_days", "45")))
    except ValueError:
        interval = 45
    theme = f.get("theme", "graphite")
    return {
        "issuer": _pick(f, "issuer", "new_issuer"),
        "nickname": _pick(f, "card_type", "new_card_type"),
        "owner": f.get("owner", "").strip(),
        "last4": f.get("last4", "").strip(),
        "notes": f.get("notes", "").strip(),
        "interval_days": interval,
        "theme": theme if theme in THEME_KEYS else "graphite",
        "active": 1 if f.get("active") else 0,
    }


def _valid(v):
    return bool(v["nickname"]) and bool(LAST4_RE.match(v["last4"]))


def _blank(owner=""):
    return {
        "issuer": "", "nickname": "", "owner": owner, "last4": "", "notes": "",
        "interval_days": int(get_setting("default_interval") or 45),
        "theme": "graphite", "active": 1,
    }


def _render_card_form(v, editing, card_id):
    ensure_card_type(v["issuer"], v["nickname"])
    images = image_map()
    preview = dict(v)
    preview["label"] = "Ready"
    preview["image"] = card_image_url(images, v["issuer"], v["nickname"])
    types = []
    for t in list_card_types():
        types.append({
            "issuer": t["issuer"], "name": t["name"],
            "image": card_image_url(images, t["issuer"], t["name"]),
            "generic": picture_is_generic(images.get((t["issuer"].lower(), t["name"].lower()))),
            "selected": t["issuer"].lower() == (v["issuer"] or "").lower()
                        and t["name"].lower() == (v["nickname"] or "").lower(),
        })
    return _render(
        "card_form.html", page="cards", card=v, editing=editing, card_id=card_id,
        preview=preview, themes=THEMES, issuers=list_issuers(), card_types=types,
        people=people_rows(),
    )


@app.route("/card-images/<path:filename>")
def card_image(filename):
    return send_from_directory(CARD_IMG_DIR, filename)


def _catalog_and_image(v):
    """Remember the issuer and card for the dropdowns, and save an uploaded card image."""
    ensure_card_type(v["issuer"], v["nickname"])
    img = request.files.get("card_image")
    if img and img.filename and v["issuer"] and v["nickname"]:
        err = save_card_image(v["issuer"], v["nickname"], img)
        if err:
            flash(err)


@app.route("/cards/new", methods=["GET", "POST"])
def card_new():
    if request.method == "POST":
        v = _form_values()
        if not _valid(v):
            flash("Enter a card name, and if you add digits, exactly the last 4.")
            return _render_card_form(v, editing=False, card_id=None)
        ensure_person(v["owner"])
        _catalog_and_image(v)
        conn = db()
        conn.execute(
            "INSERT INTO cards(nickname, issuer, owner, last4, theme, interval_days, notes, active) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (v["nickname"], v["issuer"], v["owner"], v["last4"], v["theme"],
             v["interval_days"], v["notes"], v["active"]),
        )
        plan_saved_card(conn, conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        conn.close()
        flash(f"Added {v['nickname']}.")
        return redirect(url_for("index"))
    return _render_card_form(_blank(request.args.get("owner", "").strip()), editing=False, card_id=None)


@app.route("/cards/<int:card_id>/edit", methods=["GET", "POST"])
def card_edit(card_id):
    conn = db()
    row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    conn.close()
    if row is None:
        abort(404)
    if request.method == "POST":
        v = _form_values()
        if not _valid(v):
            flash("Could not save: check the card name and last 4 digits.")
            return _render_card_form(v, editing=True, card_id=card_id)
        ensure_person(v["owner"])
        _catalog_and_image(v)
        conn = db()
        prev = conn.execute("SELECT interval_days, active FROM cards WHERE id=?", (card_id,)).fetchone()
        conn.execute(
            "UPDATE cards SET nickname=?, issuer=?, owner=?, last4=?, theme=?, interval_days=?, "
            "notes=?, active=? WHERE id=?",
            (v["nickname"], v["issuer"], v["owner"], v["last4"], v["theme"],
             v["interval_days"], v["notes"], v["active"], card_id),
        )
        if prev is None or prev["interval_days"] != v["interval_days"] or (not prev["active"] and v["active"]):
            plan_saved_card(conn, card_id)
        conn.commit()
        conn.close()
        flash("Saved.")
        return redirect(url_for("index"))
    v = {
        "issuer": row["issuer"], "nickname": row["nickname"], "owner": row["owner"],
        "last4": row["last4"], "notes": row["notes"], "interval_days": row["interval_days"],
        "theme": row["theme"] if row["theme"] in THEME_KEYS else "graphite", "active": row["active"],
    }
    return _render_card_form(v, editing=True, card_id=card_id)


@app.post("/cards/<int:card_id>/delete")
def card_delete(card_id):
    conn = db()
    conn.execute("DELETE FROM usage_log WHERE card_id=?", (card_id,))
    conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
    conn.commit()
    conn.close()
    flash("Card deleted.")
    return redirect(url_for("index"))


@app.post("/cards/<int:card_id>/used")
def used(card_id):
    raw = request.form.get("used_on") or date.today().isoformat()
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        d = date.today()
    if d > date.today():
        d = date.today()
    mark_used(card_id, d.isoformat(), source="dashboard")
    flash("Marked as used.")
    return redirect(url_for("index"))


@app.route("/used/<int:card_id>/<token>", methods=["GET", "POST"])
def used_link(card_id, token):
    """Public and token-protected, so notification buttons work without login.
    POST with ?quiet=1 (the ntfy button) returns plain 'ok'."""
    ip = client_ip()
    if _link_brake.blocked(ip):
        abort(429)
    if not secrets.compare_digest(token, get_setting("token")):
        _link_brake.fail(ip)
        abort(404)
    conn = db()
    row = conn.execute("SELECT nickname FROM cards WHERE id=?", (card_id,)).fetchone()
    conn.close()
    if row is None:
        abort(404)
    if request.method == "POST":
        mark_used(card_id, date.today().isoformat(), source="link")
        if request.args.get("quiet"):
            return "ok", 200
        return _render("confirm.html", name=row["nickname"], done=True)
    return _render("confirm.html", name=row["nickname"], done=False)


@app.post("/card-list/issuer/remove")
def remove_issuer():
    issuer = request.form.get("issuer", "").strip()
    conn = db()
    named = _named_pictures()
    rows = conn.execute("SELECT name, image FROM card_types WHERE issuer=?", (issuer,)).fetchall()
    images = [card_picture(issuer, r["name"], r["image"] or "", named) for r in rows]
    images += [r["image"] for r in rows if r["image"]]
    conn.execute("DELETE FROM card_types WHERE issuer=?", (issuer,))
    conn.execute("DELETE FROM issuers WHERE name=?", (issuer,))
    conn.commit()
    conn.close()
    for fn in set(filter(None, images)):
        _delete_image(fn)
    remember_removed(issuer=issuer)
    flash(f"Removed {issuer} and its cards from the list. Saved cards are not changed.")
    return redirect(url_for("card_settings_page"))


@app.post("/card-list/card/remove")
def remove_card():
    issuer = request.form.get("issuer", "").strip()
    name = request.form.get("name", "").strip()
    conn = db()
    row = conn.execute("SELECT image FROM card_types WHERE issuer=? AND name=?", (issuer, name)).fetchone()
    conn.execute("DELETE FROM card_types WHERE issuer=? AND name=?", (issuer, name))
    conn.commit()
    conn.close()
    if row:
        for fn in {card_picture(issuer, name, row["image"] or ""), row["image"]} - {None, ""}:
            _delete_image(fn)
    remember_removed(card=(issuer, name))
    flash(f"Removed {name} from the list. Saved cards are not changed.")
    return redirect(url_for("card_settings_page"))


@app.route("/cards/import", methods=["GET", "POST"])
def card_import():
    if request.method == "GET":
        return _render("card_import.html", page="import", stage="upload",
                       entries=[], rows_json="", to_add=0)
    f = request.files.get("csv_file")
    if not f or not f.filename:
        flash("Choose a CSV file first.")
        return redirect(url_for("card_import"))
    raw = f.read(IMPORT_MAX_BYTES + 1)
    if len(raw) > IMPORT_MAX_BYTES:
        flash("That file is larger than 256 KB.")
        return redirect(url_for("card_import"))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Save the file as a CSV in UTF-8 format, then try again.")
        return redirect(url_for("card_import"))
    entries, err = parse_card_csv(text)
    if err:
        flash(err)
        return redirect(url_for("card_import"))
    to_add = [{"issuer": e["issuer"], "card": e["card"], "last4": e["last4"], "owner": e["owner"]}
              for e in entries if e["status"] == "add"]
    return _render("card_import.html", page="import", stage="preview", entries=entries,
                   rows_json=json.dumps(to_add), to_add=len(to_add))


@app.post("/cards/import/confirm")
def card_import_confirm():
    try:
        rows = json.loads(request.form.get("rows", "[]"))
    except ValueError:
        rows = []
    if not isinstance(rows, list) or not rows or len(rows) > IMPORT_MAX_ROWS:
        flash("Nothing to add.")
        return redirect(url_for("card_import"))
    existing = _existing_card_keys()
    seen = set()
    to_insert = []
    skipped = problems = 0
    for r in rows:
        if not isinstance(r, dict):
            problems += 1
            continue
        data, err = check_card_row(r.get("issuer"), r.get("card"), r.get("last4"), r.get("owner"))
        if err:
            problems += 1
            continue
        key = _card_key(data["issuer"], data["card"], data["last4"], data["owner"])
        if key in existing or key in seen:
            skipped += 1
            continue
        seen.add(key)
        to_insert.append(data)
    interval = int(get_setting("default_interval") or 45)
    for data in to_insert:  # catalog and people first, each in its own short transaction
        ensure_card_type(data["issuer"], data["card"])
        ensure_person(data["owner"])
    conn = db()
    for data in to_insert:
        conn.execute(
            "INSERT INTO cards(nickname, issuer, owner, last4, theme, interval_days, notes, active) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (data["card"], data["issuer"], data["owner"], data["last4"], "graphite", interval, "", 1),
        )
        plan_saved_card(conn, conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()
    added = len(to_insert)
    msg = f"Added {added} card{'' if added == 1 else 's'}."
    if skipped:
        msg += f" Skipped {skipped} already saved."
    if problems:
        msg += f" {problems} line{'' if problems == 1 else 's'} had a problem and {'was' if problems == 1 else 'were'} not added."
    flash(msg)
    return redirect(url_for("index"))


@app.get("/cards/import/sample.csv")
def card_import_sample():
    body = ("Issuer,Card,Last4,Owner\n"
            "Chase,Sapphire Preferred,1234,Mom\n"
            "American Express,Blue Cash Everyday,5678,Dad\n")
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=credit-cards-sample.csv"})


PICTURE_STAGE_DIR = os.path.join(CARD_IMG_DIR, "staging")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def _stage_path(token):
    if not _TOKEN_RE.fullmatch(token or ""):
        abort(404)
    return os.path.join(PICTURE_STAGE_DIR, token)


def _clear_old_staging():
    """Drop review folders nobody finished within a day."""
    if not os.path.isdir(PICTURE_STAGE_DIR):
        return
    cutoff = time.time() - 24 * 3600
    for name in os.listdir(PICTURE_STAGE_DIR):
        path = os.path.join(PICTURE_STAGE_DIR, name)
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _load_staged(folder):
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["entries"]


def _assign_once(entries):
    """Two files can't both fill the same card. The second one is set to Skip for a review."""
    seen = set()
    for e in entries:
        key = e["pick"]
        if key == "__new__":
            key = "new:" + f"{e['new_issuer']}\t{e['new_card']}".lower()
        if e["pick"] and key in seen:
            e["pick"] = ""
            e["note"] = "Same card as another file. Choose a different card or skip"
        elif e["pick"]:
            seen.add(key)


def _picture_uploads():
    """Collect (file name, bytes) for each picture in the upload. ZIP files are opened and their pictures used.

    Returns (items, problems). Files that aren't pictures by name (Thumbs.db, notes) are skipped quietly.
    """
    items, problems = [], []
    unpacked = 0

    def add(name, raw):
        if not name.lower().endswith(PICTURE_EXTS):
            return
        if len(raw) > MAX_IMAGE_BYTES:
            problems.append(f"{name}: larger than 4 MB")
            return
        items.append((name, raw))

    for f in request.files.getlist("pictures"):
        if not f or not f.filename:
            continue
        name = os.path.basename(f.filename.replace("\\", "/"))[:120]
        if name.lower().endswith(".zip"):
            raw = f.read(MAX_ZIP_BYTES + 1)
            if len(raw) > MAX_ZIP_BYTES:
                problems.append(f"{name}: larger than 40 MB")
                continue
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                problems.append(f"{name}: not a valid ZIP file")
                continue
            with zf:
                for info in zf.infolist():
                    inner = info.filename.replace("\\", "/")
                    base = os.path.basename(inner)
                    if info.is_dir() or not base or base.startswith(".") or inner.startswith("__MACOSX/"):
                        continue
                    if not base.lower().endswith(PICTURE_EXTS):
                        continue
                    if info.file_size > MAX_IMAGE_BYTES:
                        problems.append(f"{base}: larger than 4 MB")
                        continue
                    unpacked += info.file_size
                    if unpacked > MAX_ZIP_UNPACKED or len(items) >= MAX_PICTURES:
                        problems.append(f"{name}: stopped after {len(items)} pictures to keep the upload small")
                        break
                    with zf.open(info) as fh:
                        add(base, fh.read(MAX_IMAGE_BYTES + 1))
        else:
            add(name, f.read(MAX_IMAGE_BYTES + 1))
    if len(items) > MAX_PICTURES:
        problems.append(f"Only the first {MAX_PICTURES} pictures were used")
        items = items[:MAX_PICTURES]
    return items, problems


@app.post("/card-pictures")
def card_pictures():
    """Step 1 for uploads: read the files and send them to the review."""
    uploads, problems = _picture_uploads()
    return _stage_and_review([{"name": n, "raw": r} for n, r in uploads], problems)


def _stage_and_review(uploads, problems):
    """Store the pictures and show the review.

    Each upload is a dict: name, raw (bytes), hint (text used for matching, optional),
    new_issuer and new_card (a card that isn't in the list yet, pre-filled for the user to check).
    """
    _clear_old_staging()
    token = secrets.token_urlsafe(18)
    folder = _stage_path(token)
    os.makedirs(folder)
    entries = []
    for u in uploads:
        raw = u["raw"]
        ext = _image_ext(raw[:16])
        if ext is None:
            problems.append(f"{u['name']}: not a PNG, JPG, GIF or WEBP image")
            continue
        idx = len(entries)
        stored = f"{idx}.{ext}"
        with open(os.path.join(folder, stored), "wb") as out:
            out.write(raw)
        entries.append({"idx": idx, "name": u["name"], "stored": stored, "hint": u.get("hint", ""),
                        "new_issuer": u.get("new_issuer", ""), "new_card": u.get("new_card", ""),
                        "pick": "", "note": "", "forced": u.get("forced_pick", "")})
    if not entries:
        shutil.rmtree(folder, ignore_errors=True)
        flash("No usable pictures were uploaded." + (" " + "; ".join(problems) if problems else ""))
        return redirect(url_for("card_settings_page"))
    catalog = list_card_types()
    for e in entries:
        stem = os.path.splitext(e["name"])[0]
        key = slug(stem)
        exact = [c for c in catalog if picture_name(c["issuer"], c["name"]) == key]
        if len(exact) == 1:
            card, note, sure = exact[0], "Exact name", True
        else:
            card, note, sure = match_picture(e["hint"] or stem, catalog)
        if card and sure:
            e["pick"] = _pick_value(card)
            e["note"] = note
        elif e["new_card"] and any(c["issuer"].lower() == e["new_issuer"].lower()
                                   and c["name"].lower() == e["new_card"].lower() for c in catalog):
            card = next(c for c in catalog if c["issuer"].lower() == e["new_issuer"].lower()
                        and c["name"].lower() == e["new_card"].lower())
            e["pick"] = _pick_value(card)
            e["note"] = "Matched by name"
        elif e["new_card"]:
            e["pick"] = "__new__"
            e["note"] = ("Not in the list yet. Check the issuer and name, then save to add it. " + note)
        else:
            e["note"] = note
        if e.get("forced"):
            e["pick"] = e["forced"]
            e["note"] = "Chosen by you"
    _assign_once(entries)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"entries": entries}, fh)
    options = [{"val": _pick_value(c), "label": f"{c['issuer']} · {c['name']}"} for c in catalog]
    options.append({"val": "__new__", "label": "+ Add a new card…"})
    return _render("card_pictures.html", page="settings", token=token, entries=entries,
                   options=options, problems=problems)


# -- finding pictures on a card website
SCRAPE_UA = "Mozilla/5.0 (compatible; CreditKeepAlive/1.0; self-hosted card picture helper)"
SCRAPE_TIMEOUT = 8                  # seconds per request
SCRAPE_BUDGET = 20                  # seconds for the whole search; the web server stops a request after 30
SCRAPE_MAX_PAGE = 3 * 1024 * 1024
SCRAPE_MAX_IMAGES = 40


def _public_url(url):
    """None if the address is a public web address. Otherwise a message saying why not."""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https") or not p.hostname:
        return "Enter a full web address that starts with https://"
    try:
        found = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except (OSError, UnicodeError):
        return "That address could not be found."
    for info in found:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return "That address could not be checked."
        if not ip.is_global:
            return "That address is on a private network, which can't be used here."
    return None


class _NoPrivateRedirect(HTTPRedirectHandler):
    """Refuses redirects that lead to a private network address."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = _public_url(newurl)
        if problem:
            raise URLError(problem)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SCRAPE_OPENER = build_opener(_NoPrivateRedirect)


def _fetch(url, limit, accept="*/*"):
    """Returns (content type, bytes, final address). Raises if the request fails or the answer is larger than limit."""
    req = Request(url, headers={"User-Agent": SCRAPE_UA, "Accept": accept})
    with _SCRAPE_OPENER.open(req, timeout=SCRAPE_TIMEOUT) as r:
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return r.headers.get("Content-Type", ""), data, r.geturl()


def _robots_problem(url):
    """None if the site's robots.txt allows this page. Otherwise a message."""
    p = urlparse(url)
    try:
        _, data, _ = _fetch(f"{p.scheme}://{p.netloc}/robots.txt", 200_000, "text/plain")
    except HTTPError as err:
        if err.code in (404, 410):
            return None                        # no robots.txt means no rules
        return "The site's robots.txt could not be read, so the page was not fetched."
    except Exception:
        return "The site's robots.txt could not be read, so the page was not fetched."
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(data.decode("utf-8", "replace").splitlines())
    if not rp.can_fetch(SCRAPE_UA, url):
        return "The site's robots.txt does not allow fetching this page."
    return None


class _ImageFinder(HTMLParser):
    """Collects the pictures on a page, with the text that describes each one."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.images = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        a = {k: (v or "") for k, v in attrs}
        try:
            if int(a.get("width") or 999) < 60 or int(a.get("height") or 999) < 60:
                return                         # icons
        except ValueError:
            pass
        src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
        if a.get("srcset"):
            parts = [x.strip().split(" ")[0] for x in a["srcset"].split(",") if x.strip()]
            if parts:
                src = parts[-1]                # the last entry is usually the largest
        if not src or src.startswith("data:"):
            return
        url = urljoin(self.base, src)
        if url in self.seen:
            return
        self.seen.add(url)
        desc = " ".join(a.get(k, "") for k in ("alt", "title", "aria-label") if a.get(k, "").strip())
        self.images.append((url, desc))


_CARD_WORD = re.compile(r"card", re.I)


def _looks_like_card(text):
    """True when a picture's name or description mentions a card."""
    return bool(_CARD_WORD.search(text or ""))


def _issuer_for_host(host, catalog):
    """The issuer whose name is in the page's address, e.g. creditcards.chase.com -> Chase. Else ''."""
    plain = re.sub(r"[^a-z0-9]", "", (host or "").lower())
    best = ""
    for issuer in {c["issuer"] for c in catalog}:
        key = re.sub(r"[^a-z0-9]", "", issuer.lower())
        if key and key in plain and len(key) > len(best):
            best = issuer
    return best


def _card_name_from(desc, stem, issuer):
    """A card name guessed from the picture's description, or from its file name if there is none."""
    text = (desc or "").strip()
    if text:
        text = re.sub(r"^(clickable card art links to|card art for|link to)\s+", "", text, flags=re.I)
        text = re.split(r"[.|•]|\s+-\s+", text)[0]
        text = re.sub(r"\(?(registered trademark|service mark)\)?|\(tm\)|[®™℠]", " ", text, flags=re.I)
        text = re.sub(r"\b(credit card|credit|card|product page|page|apply now|apply|offer)\b", " ", text, flags=re.I)
    else:
        text = re.sub(r"[_\-]+", " ", stem)
        text = re.sub(r"(?i)\b(card|art|new|alt|final|copy|cardart|2x|x2|v\d+|\d{4})\b", " ", text)
    if issuer and text.lower().startswith(issuer.lower()):
        text = text[len(issuer):]
    return re.sub(r"\s+", " ", text).strip(" ,-:")[:80]


def _scrape_pictures(page_url, candidates_out=None):
    """Find card pictures on a web page. Returns (uploads, problems).

    Every picture that looks like a card is taken. Pictures that name a card in the list are chosen for it.
    Other card pictures are offered as new cards, with the name and issuer filled in for the user to check.
    """
    problem = _public_url(page_url) or _robots_problem(page_url)
    if problem:
        return [], [problem]
    try:
        ctype, data, final = _fetch(page_url, SCRAPE_MAX_PAGE, "text/html")
    except HTTPError as err:
        return [], [f"The page could not be opened (error {err.code})."]
    except ValueError:
        return [], ["That page is larger than 3 MB, so it was not read."]
    except Exception:
        return [], ["The page could not be opened."]
    if "html" not in ctype.lower():
        return [], ["That address is not a web page."]
    finder = _ImageFinder(final)
    finder.feed(data.decode("utf-8", "replace"))
    if candidates_out is not None:
        candidates_out.extend(finder.images)
    if not finder.images:
        return [], ["No pictures were found on that page. Many card sites load pictures with scripts, "
                    "so a folder or ZIP upload may work better."]
    catalog = list_card_types()
    host = urlparse(final).hostname or ""
    page_issuer = _issuer_for_host(host, catalog)
    uploads, problems, skipped, downloads = [], [], 0, 0
    started = time.monotonic()
    for url, desc in finder.images:
        fname = unquote(os.path.basename(urlparse(url).path))[:120]
        if not fname.lower().endswith(PICTURE_EXTS):
            continue
        stem = os.path.splitext(fname)[0]
        chosen, sure = None, False
        for hint in (stem, desc):
            if not hint:
                continue
            card, _, ok = match_picture(hint, catalog)
            if card and ok:
                chosen, sure = hint, True
                break
            if card and chosen is None:
                chosen = hint
        card_like = _looks_like_card(desc) or _looks_like_card(stem)
        if not sure and not card_like:
            skipped += 1
            continue
        if time.monotonic() - started > SCRAPE_BUDGET:
            problems.append("Stopped early because the site was slow. Run the search again, or upload a ZIP.")
            break
        if downloads >= SCRAPE_MAX_IMAGES or len(uploads) >= MAX_PICTURES:
            problems.append(f"Stopped after {len(uploads)} pictures.")
            break
        if _public_url(url):
            problems.append(f"{fname}: is not a public web address, so it was not downloaded")
            continue
        downloads += 1
        try:
            _, raw, _ = _fetch(url, MAX_IMAGE_BYTES, "image/*")
        except Exception:
            problems.append(f"{fname}: could not be downloaded")
            continue
        if sure:
            uploads.append({"name": fname, "raw": raw, "hint": chosen, "new_issuer": "", "new_card": ""})
        else:
            base = desc if _looks_like_card(desc) else stem
            uploads.append({"name": fname, "raw": raw, "hint": chosen or "",
                            "new_issuer": page_issuer, "new_card": _card_name_from(base, stem, page_issuer)})
    if skipped:
        problems.append(f"{skipped} other pictures on the page were not card pictures, so they were left out.")
    return uploads, problems


@app.post("/card-pictures/scrape")
def card_pictures_scrape():
    """Find card pictures on a card issuer's web page and send them to the same review."""
    page = request.form.get("url", "").strip()[:500]
    if not page:
        flash("Enter the address of a card page.")
        return redirect(url_for("card_settings_page"))
    found = []
    uploads, problems = _scrape_pictures(page, candidates_out=found)
    if uploads:
        return _stage_and_review(uploads, problems)
    web = [(u, d) for u, d in found if urlparse(u).scheme in ("http", "https")]
    if web:
        return _picker(page, web, problems)
    flash("No card pictures could be taken from that page. " + " ".join(problems))
    return redirect(url_for("card_settings_page"))


def _picker(page, candidates, problems):
    """Every picture on the page, for the user to tick and assign by hand."""
    _clear_old_staging()
    token = secrets.token_urlsafe(18)
    folder = _stage_path(token)
    os.makedirs(folder)
    items = []
    for url, desc in candidates[:SCRAPE_MAX_IMAGES * 2]:
        items.append({"url": url, "desc": desc,
                      "name": unquote(os.path.basename(urlparse(url).path))[:120] or "picture"})
    with open(os.path.join(folder, "candidates.json"), "w", encoding="utf-8") as fh:
        json.dump({"page": page, "items": items}, fh)
    options = [{"val": _pick_value(c), "label": f"{c['issuer']} · {c['name']}"} for c in list_card_types()]
    return _render("card_pictures_pick.html", page="card_settings", token=token, items=items,
                   options=options, page_url=page, problems=problems)


@app.post("/card-pictures/pick/save")
def card_pictures_pick_save():
    """Download the pictures the user ticked, each for the card they chose, and send them to the review."""
    token = request.form.get("token", "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,60}", token):
        abort(400)
    path = os.path.join(_stage_path(token), "candidates.json")
    if not os.path.exists(path):
        flash("That list of pictures has expired. Find the pictures again.")
        return redirect(url_for("card_settings_page"))
    with open(path, encoding="utf-8") as fh:
        items = json.load(fh)["items"]
    valid = {_pick_value(c) for c in list_card_types()}
    uploads, problems = [], []
    for raw_idx in request.form.getlist("keep"):
        try:
            i = int(raw_idx)
            item = items[i]
        except (ValueError, IndexError):
            continue
        choice = request.form.get(f"card_{i}", "")
        if choice not in valid:
            problems.append(f"{item['name']}: no card was chosen, so it was skipped")
            continue
        if len(uploads) >= MAX_PICTURES:
            problems.append(f"Stopped after {len(uploads)} pictures.")
            break
        problem = _public_url(item["url"])
        if problem:
            problems.append(f"{item['name']}: not a public web address, so it was not downloaded")
            continue
        try:
            _, raw, _ = _fetch(item["url"], MAX_IMAGE_BYTES, "image/*")
        except Exception:
            problems.append(f"{item['name']}: could not be downloaded")
            continue
        uploads.append({"name": item["name"], "raw": raw, "hint": "", "new_issuer": "", "new_card": "",
                        "forced_pick": choice})
    if not uploads:
        flash("No pictures were selected. " + " ".join(problems))
        return redirect(url_for("card_settings_page"))
    return _stage_and_review(uploads, problems)


@app.get("/card-pictures/staged/<token>/<int:idx>")
def staged_picture(token, idx):
    folder = _stage_path(token)
    if not os.path.exists(os.path.join(folder, "manifest.json")):
        abort(404)
    entries = _load_staged(folder)
    if not 0 <= idx < len(entries):
        abort(404)
    return send_from_directory(folder, entries[idx]["stored"])


@app.post("/card-pictures/confirm")
def card_pictures_confirm():
    """Step 2: save the pictures the user kept in the review."""
    folder = _stage_path(request.form.get("token", ""))
    if not os.path.exists(os.path.join(folder, "manifest.json")):
        flash("That review has expired. Upload the pictures again.")
        return redirect(url_for("card_settings_page"))
    entries = _load_staged(folder)
    known = {(c["issuer"], c["name"]) for c in list_card_types()}
    saved, problems, used = [], [], set()
    skipped = 0
    for e in entries:
        choice = request.form.get(f"pick_{e['idx']}", "")
        if not choice:
            skipped += 1
            continue
        if choice == "__new__":
            issuer = request.form.get(f"new_issuer_{e['idx']}", "").strip()
            name = request.form.get(f"new_card_{e['idx']}", "").strip()
            if not issuer or not name or len(issuer) > 60 or len(name) > 80:
                problems.append(f"{e['name']}: enter the issuer and the card name")
                continue
            ensure_card_type(issuer, name)
            target = (issuer, name)
        else:
            issuer, _, name = choice.partition("\t")
            if (issuer, name) not in known:
                problems.append(f"{e['name']}: that card is no longer in the list")
                continue
            target = (issuer, name)
        if target in used:
            problems.append(f"{e['name']}: that card already has a picture from another file")
            continue
        used.add(target)
        with open(os.path.join(folder, e["stored"]), "rb") as fh:
            err = save_card_image(target[0], target[1], fh)
        if err:
            problems.append(f"{e['name']}: {err.rstrip('.')}")
        else:
            saved.append(f"{target[0]} {target[1]}")
    shutil.rmtree(folder, ignore_errors=True)
    msg = f"Saved {len(saved)} picture{'' if len(saved) == 1 else 's'}."
    if skipped:
        msg += f" Skipped {skipped}."
    if problems:
        msg += " Not saved: " + "; ".join(problems) + "."
    flash(msg)
    return redirect(url_for("card_settings_page"))


# -- card picture backup: export every picture in one ZIP, and import it on another installation
PICTURE_PACK_FORMAT = "credit-keepalive-card-pictures"
PICTURE_PACK_MAX_CARDS = 500


@app.get("/card-pictures/export")
def card_pictures_export():
    conn = db()
    rows = conn.execute("SELECT issuer, name, image FROM card_types "
                        "ORDER BY issuer COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    conn.close()
    named = _named_pictures()
    buf = io.BytesIO()
    cards, used = [], set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            found = card_picture(r["issuer"], r["name"], r["image"] or "", named)
            if not found:
                continue
            src = os.path.join(CARD_IMG_DIR, found)
            ext = os.path.splitext(found)[1].lower()
            base = picture_name(r["issuer"], r["name"]) or "card"
            fname, n = base + ext, 2
            while fname in used:
                fname = f"{base}-{n}{ext}"
                n += 1
            used.add(fname)
            z.write(src, fname)
            cards.append({"issuer": r["issuer"], "name": r["name"], "file": fname})
        z.writestr("manifest.json", json.dumps({"format": PICTURE_PACK_FORMAT, "version": 1, "cards": cards}, indent=2))
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="Credit-KeepAlive-card-pictures.zip"'})


@app.post("/card-pictures/import")
def card_pictures_import():
    """Restore pictures from an export. Each picture goes to the card it was exported from, adding the card if it isn't in the list."""
    f = request.files.get("pack")
    if not f or not f.filename:
        flash("Choose a pictures ZIP file first.")
        return redirect(url_for("card_settings_page"))
    replace = request.form.get("replace") == "1"
    raw = f.read(MAX_ZIP_BYTES + 1)
    if len(raw) > MAX_ZIP_BYTES:
        flash("That file is larger than 40 MB, so it was not imported.")
        return redirect(url_for("card_settings_page"))
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        flash("That file is not a valid ZIP file.")
        return redirect(url_for("card_settings_page"))
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError):
            flash("That ZIP has no card list, so it is not a card picture export.")
            return redirect(url_for("card_settings_page"))
        if not isinstance(manifest, dict) or manifest.get("format") != PICTURE_PACK_FORMAT:
            flash("That ZIP is not a card picture export from this app.")
            return redirect(url_for("card_settings_page"))
        cards = manifest.get("cards")
        if not isinstance(cards, list):
            flash("That ZIP is not a card picture export from this app.")
            return redirect(url_for("card_settings_page"))
        if len(cards) > PICTURE_PACK_MAX_CARDS:
            flash(f"That export has more than {PICTURE_PACK_MAX_CARDS} cards, so it was not imported.")
            return redirect(url_for("card_settings_page"))
        if sum(i.file_size for i in zf.infolist()) > MAX_ZIP_UNPACKED:
            flash("That ZIP is too large once unpacked, so it was not imported.")
            return redirect(url_for("card_settings_page"))
        conn = db()
        conn.close()
        pictured = image_map()
        with_picture = {(c["issuer"], c["name"]) for c in list_card_types()
                        if (c["issuer"].lower(), c["name"].lower()) in pictured}
        known = {(c["issuer"], c["name"]) for c in list_card_types()}
        saved, skipped, added_cards, problems = 0, 0, 0, []
        for item in cards:
            if not isinstance(item, dict):
                problems.append("a card entry was not readable")
                continue
            issuer = str(item.get("issuer", "")).strip()[:60]
            name = str(item.get("name", "")).strip()[:80]
            fn = str(item.get("file", ""))
            label = f"{issuer} {name}".strip() or "a card"
            if not issuer or not name:
                problems.append(f"{label}: the issuer or card name is missing")
                continue
            if "/" in fn or "\\" in fn or fn.startswith(".") or fn not in zf.namelist():
                problems.append(f"{label}: its picture is missing from the ZIP")
                continue
            if zf.getinfo(fn).file_size > MAX_IMAGE_BYTES:
                problems.append(f"{label}: its picture is larger than 4 MB")
                continue
            data = zf.read(fn)
            if _image_ext(data[:16]) is None:
                problems.append(f"{label}: its picture is not a PNG, JPG, GIF or WEBP image")
                continue
            if (issuer, name) in with_picture and not replace:
                skipped += 1
                continue
            if (issuer, name) not in known:
                added_cards += 1
            ensure_card_type(issuer, name)
            err = save_card_image(issuer, name, io.BytesIO(data))
            if err:
                problems.append(f"{label}: {err.rstrip('.')}")
            else:
                saved += 1
                with_picture.add((issuer, name))
                known.add((issuer, name))
    msg = f"Imported {saved} picture{'' if saved == 1 else 's'}."
    if added_cards:
        msg += f" Added {added_cards} card{'' if added_cards == 1 else 's'} to the list."
    if skipped:
        msg += f" Skipped {skipped} that already have a picture (tick Replace to overwrite)."
    if problems:
        msg += " Not imported: " + "; ".join(problems) + "."
    flash(msg)
    return redirect(url_for("card_settings_page"))


# -- history
@app.route("/history")
def history_page():
    card_filter = request.args.get("card", type=int)
    conn = db()
    summary = conn.execute(
        "SELECT c.id, c.nickname, c.issuer, c.owner, COUNT(u.id) AS n, MAX(u.used_on) AS last "
        "FROM cards c LEFT JOIN usage_log u ON u.card_id = c.id "
        "GROUP BY c.id ORDER BY c.owner COLLATE NOCASE, c.nickname COLLATE NOCASE"
    ).fetchall()
    sql = ("SELECT u.id, u.used_on, u.source, u.logged_at, c.nickname FROM usage_log u "
           "JOIN cards c ON c.id = u.card_id")
    args, filter_name = [], None
    if card_filter:
        sql += " WHERE u.card_id = ?"
        args.append(card_filter)
        r = conn.execute("SELECT nickname FROM cards WHERE id=?", (card_filter,)).fetchone()
        filter_name = r["nickname"] if r else None
    sql += " ORDER BY u.used_on DESC, u.id DESC LIMIT 200"
    entries = conn.execute(sql, args).fetchall()
    conn.close()
    return _render("history.html", page="history", summary=summary,
                   entries=entries, filter_name=filter_name)


@app.post("/history/<int:entry_id>/delete")
def delete_entry(entry_id):
    conn = db()
    row = conn.execute("SELECT card_id FROM usage_log WHERE id=?", (entry_id,)).fetchone()
    if row:
        cid = row["card_id"]
        conn.execute("DELETE FROM usage_log WHERE id=?", (entry_id,))
        latest = conn.execute("SELECT MAX(used_on) AS d FROM usage_log WHERE card_id=?", (cid,)).fetchone()["d"]
        conn.execute("UPDATE cards SET last_used=?, last_notified=NULL WHERE id=?", (latest, cid))
        plan_saved_card(conn, cid)
        conn.commit()
        flash("Entry removed and the last-used date recalculated.")
    conn.close()
    return redirect(request.referrer or url_for("history_page"))


# -- people
@app.route("/people")
def people_page():
    conn = db()
    counts = {r["o"].lower(): r["n"] for r in conn.execute(
        "SELECT owner AS o, COUNT(*) AS n FROM cards WHERE owner != '' GROUP BY owner COLLATE NOCASE")}
    conn.close()
    rows = []
    for p in people_rows():
        d = dict(p)
        d["card_count"] = counts.get(p["name"].lower(), 0)
        d["ntfy_display"] = to_display(p["ntfy_url"])
        d["builtin"] = is_builtin(p["ntfy_url"])
        d["topic"] = topic_from(p["ntfy_url"]) if d["builtin"] else ""
        d["phone"] = phone_server()
        rows.append(d)
    return _render("people.html", page="people", people=rows, topic_mode=topic_mode())


def _person(person_id):
    conn = db()
    row = conn.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
    conn.close()
    if row is None:
        abort(404)
    return row


@app.post("/people/add")
def person_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a name.")
    else:
        ensure_person(name)
        flash(f"Added {name}.")
    return redirect(url_for("people_page"))


@app.post("/people/<int:person_id>/channels")
def person_update(person_id):
    _person(person_id)
    conn = db()
    conn.execute("UPDATE people SET ntfy_url=?, apprise_urls=? WHERE id=?", (
        to_stored(request.form.get("ntfy_url", "")),
        "\n".join(l.strip() for l in request.form.get("apprise_urls", "").splitlines() if l.strip()),
        person_id,
    ))
    conn.commit()
    conn.close()
    flash("Alerts saved.")
    return redirect(url_for("people_page"))


@app.post("/people/<int:person_id>/toggle")
def person_toggle(person_id):
    p = _person(person_id)
    new = 0 if p["enabled"] else 1
    conn = db()
    conn.execute("UPDATE people SET enabled=? WHERE id=?", (new, person_id))
    conn.commit()
    conn.close()
    flash(f"Reminders {'on' if new else 'off'} for {p['name']}.")
    return redirect(url_for("people_page"))


@app.post("/people/<int:person_id>/delete")
def person_delete(person_id):
    p = _person(person_id)
    conn = db()
    conn.execute("DELETE FROM people WHERE id=?", (person_id,))
    conn.commit()
    conn.close()
    flash(f"Removed {p['name']}. Their cards stay and use the default alerts.")
    return redirect(url_for("people_page"))


@app.post("/people/<int:person_id>/test")
def person_test(person_id):
    p = _person(person_id)
    res = send_test(p["ntfy_url"], p["apprise_urls"], p["name"])
    if res is None:
        flash(f"No alerts set up for {p['name']} yet.")
    else:
        flash(("Test sent. " if res[0] else "Test failed. ") + res[1])
    return redirect(url_for("people_page"))


# -- settings and actions
@app.route("/cards/settings")
def card_settings_page():
    s = {k: get_setting(k) for k in ("default_interval", "stagger_percent")}
    pictures_saved, pictures_missing = picture_health()
    return _render(
        "card_settings.html", page="card_settings", s=s, catalog=catalog_groups(),
        pictures_saved=pictures_saved, pictures_missing=pictures_missing,
        picture_folder=CARD_IMG_DIR, real_art_url=REAL_ART_URL,
    )


@app.post("/cards/settings/save")
def card_settings_save():
    for key, lo, hi in (("default_interval", 1, 3650), ("stagger_percent", 0, 50)):
        if key in request.form:
            try:
                set_setting(key, min(hi, max(lo, int(request.form.get(key, "")))))
            except ValueError:
                pass
    flash("Card settings saved.")
    return redirect(url_for("card_settings_page"))


@app.route("/settings")
def settings_page():
    s = {k: get_setting(k) for k in DEFAULTS if k != "ntfy_token"}
    topic = get_setting("ntfy_url")
    builtin = is_builtin(topic)
    s["ntfy_url"] = to_display(topic)
    pictures_saved, pictures_missing = picture_health()
    return _render(
        "settings.html", page="settings", s=s, catalog=catalog_groups(),
        pictures_saved=pictures_saved, pictures_missing=pictures_missing, picture_folder=CARD_IMG_DIR,
        real_art_url=REAL_ART_URL,
        token_set=bool(get_setting("ntfy_token")),
        phone=phone_server(), phone_saved=get_setting("phone_address"), phone_guess=phone_guess(),
        guess_is_name=host_is_name(), app_saved=get_setting("app_address"), app_now=app_address(),
        topic_mode=topic_mode(),
        ntfy={"up": phone_server_ready(), "builtin": builtin,
              "topic": topic_from(topic) if builtin else ""},
        ntfy_logins=ntfy_logins(), look_themes=appearance_themes(),
    )


def ntfy_logins():
    """Logins for the built-in phone server, shown on the Settings page behind the app password."""
    return {user: ntfy_acct.password_for(user) for user in ntfy_acct.LOGIN_USERS}


@app.post("/settings")
def settings_save():
    if "ntfy_url" in request.form:
        set_setting("ntfy_url", to_stored(request.form.get("ntfy_url", "")))
    tok = request.form.get("ntfy_token", "").strip()
    if tok:
        set_setting("ntfy_token", tok)
    if "notify_urls" in request.form:
        set_setting("notify_urls", "\n".join(
            l.strip() for l in request.form.get("notify_urls", "").splitlines() if l.strip()))
    for key, lo, hi in (("notify_hour", 0, 23), ("renag_days", 1, 365)):
        if key in request.form:
            try:
                set_setting(key, min(hi, max(lo, int(request.form.get(key, "")))))
            except ValueError:
                pass
    flash("Settings saved.")
    return redirect(url_for("settings_page"))


def _look_back():
    """Back to the page the theme was changed on. Only same-site paths are used."""
    back = request.form.get("next", "")
    return _safe_next(back) if back else url_for("settings_page")


@app.post("/settings/appearance")
def settings_appearance():
    if request.form.get("reset"):
        for key, value in APPEARANCE_DEFAULTS.items():
            set_setting(key, value)
        flash("The default look is back.")
        return redirect(_look_back())
    for key, choices in APPEARANCE_CHOICES.items():
        value = request.form.get(key, "")
        if value in choices:
            set_setting(key, value)
    try:
        set_setting("look_radius", min(24, max(0, int(request.form.get("look_radius", "")))))
    except ValueError:
        pass
    flash("Appearance saved.")
    return redirect(_look_back())


@app.post("/settings/pictures/defaults")
def settings_default_pictures():
    redrawn = redraw_generic_pictures()
    made = ensure_default_pictures()
    flash(f"Redrew {redrawn} generic card picture{'' if redrawn == 1 else 's'}, and drew "
          f"{made} for cards that had none. Your own pictures were not changed.")
    return redirect(url_for("card_settings_page"))


@app.post("/settings/spread")
def settings_spread():
    conn = db()
    count, days = spread_all_cards(conn)
    conn.commit()
    conn.close()
    flash(f"Spread {count} active card{'' if count == 1 else 's'} over their cycles. "
          f"They now come up on {days} different day{'' if days == 1 else 's'}.")
    return redirect(url_for("card_settings_page"))


@app.post("/settings/test")
def settings_test():
    res = send_test(get_setting("ntfy_url"), get_setting("notify_urls"), "Default alerts")
    if res is None:
        flash("No default alerts are set up yet.")
    else:
        flash(("Test sent. " if res[0] else "Test failed. ") + res[1])
    return redirect(url_for("settings_page"))


@app.post("/settings/check")
def settings_check():
    s = run_check(force=True)
    if s["due"] == 0:
        flash("No cards are due right now.")
    else:
        flash(f"Checked {s['due']} due: {s['sent']} sent, {s['failed']} failed, "
              f"{s['skipped']} skipped (reminders off or no alerts set up).")
    return redirect(url_for("settings_page"))


@app.post("/settings/ntfy/login/<user>")
def settings_ntfy_login(user):
    if user not in ntfy_acct.LOGIN_USERS:
        abort(404)
    typed = request.form.get("password", "")
    try:
        if typed.strip():
            if typed != typed.strip() or not (8 <= len(typed) <= 64) or any(ord(ch) < 32 for ch in typed):
                flash("Use 8 to 64 characters with no spaces at the start or end. The password was not changed.")
                return redirect(url_for("settings_page"))
            ntfy_acct.set_password(user, typed)
        else:
            ntfy_acct.regenerate(user)
    except ntfy_acct.AccountError as e:
        flash(f"Could not change the {user} password. {e}")
    else:
        if user == ntfy_acct.PHONE:
            flash("Phone password saved. Log in again in the ntfy app on each phone with the new password.")
        else:
            flash("Admin password saved. Use it to log in to the ntfy web page.")
    return redirect(url_for("settings_page"))


@app.post("/settings/addresses")
def settings_addresses():
    values = {}
    for field, label in (("phone_address", "Phone server address"), ("app_address", "Web address of this app")):
        value = request.form.get(field, "").strip().rstrip("/")
        parts = urlparse(value)
        if value and (parts.scheme not in ("http", "https") or not parts.netloc):
            flash(f"{label} should look like http://192.168.1.50:2586 or https://ntfy.example.com. Nothing was saved.")
            return redirect(url_for("settings_page"))
        values[field] = value
    for field, value in values.items():
        set_setting(field, value)
    flash("Addresses saved.")
    return redirect(url_for("settings_page"))


@app.post("/settings/topic-mode")
def settings_topic_mode():
    mode = request.form.get("topic_mode", "")
    if mode not in ("household", "person"):
        abort(400)
    set_setting("topic_mode", mode)
    flash("Every phone will get every reminder through the household topic." if mode == "household"
          else "Each person's reminders will go to their own topic.")
    return redirect(url_for("settings_page"))


@app.post("/settings/ntfy/topic")
def settings_topic():
    set_setting("ntfy_url", builtin_ref(new_topic()))
    flash("Created a household topic. Subscribe to it in the ntfy app on each phone.")
    return redirect(url_for("settings_page"))


@app.post("/people/<int:person_id>/topic")
def person_topic(person_id):
    _person(person_id)
    conn = db()
    conn.execute("UPDATE people SET ntfy_url=? WHERE id=?", (builtin_ref(new_topic()), person_id))
    conn.commit()
    conn.close()
    flash("Created a phone topic. Subscribe to it in the ntfy app on their phone.")
    return redirect(url_for("people_page"))


# --------------------------------------------------------------- scheduler
_scheduler = BackgroundScheduler()
_scheduler.add_job(scheduled_tick, "interval", minutes=15, id="tick", next_run_time=datetime.now())
_scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
