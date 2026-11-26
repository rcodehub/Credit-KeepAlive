"""Writes the Community Applications copy of the Unraid template.

Sets <Date> (today, Eastern time) and <Changes> (build number plus the newest four
changelog days) so the Apps page shows what changed. Usage:
    python3 .github/scripts/stamp_template.py <source.xml> <output.xml>
Environment: BUILD_NUMBER (the build's run number, optional).
"""
import datetime
import html
import os
import re
import sys
from zoneinfo import ZoneInfo

src, dst = sys.argv[1], sys.argv[2]
build = os.environ.get("BUILD_NUMBER", "dev")
today = datetime.datetime.now(ZoneInfo("America/New_York")).date().isoformat()

changelog = open("CHANGELOG.md", encoding="utf-8").read()
days = re.split(r"(?m)^### ", changelog)[1:5]  # newest four day sections
changes = f"Build {build}, published {today}.\n" + "\n".join("### " + d.strip() for d in days)

xml = open(src, encoding="utf-8").read()


def put(tag, value, text):
    """Replace <tag>...</tag> or <tag/>, or insert it before <Category>."""
    block = f"  <{tag}>{html.escape(value, quote=False)}</{tag}>\n"
    pattern = re.compile(rf"  <{tag}\b[^>]*?(/>|>.*?</{tag}>)\n", re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    return text.replace("  <Category>", block + "  <Category>", 1)


xml = put("Date", today, xml)
xml = put("Changes", changes, xml)
open(dst, "w", encoding="utf-8").write(xml)
print(f"stamped {dst}: build {build}, date {today}")
