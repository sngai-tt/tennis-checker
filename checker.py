"""
Tennis court availability checker (9 London venues)
===================================================
What this script does, in plain English:
  1. Asks each venue's booking system which courts are free in the next 7 days.
  2. Saves ALL free slots to slots.json (the dashboard, index.html, reads this).
  3. Sends an ntfy push notification for NEW free slots that fall inside your
     preferred times (set in PREFERRED_WINDOWS below).
  4. Remembers what it already told you about in seen_slots.json, so you
     don't get the same alert twice.

Run locally to test (no notifications sent):   python checker.py --dry-run
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

# ============================================================
# 1. SETTINGS YOU MAY WANT TO EDIT
# ============================================================

LONDON = ZoneInfo("Europe/London")

DAYS_AHEAD = 7          # how many days (including today) to look at
VENUE_TIMEOUT = 3       # seconds to wait for each web request
VENUE_BUDGET = 10       # max seconds spent on any single venue in total

# Only slots inside these windows trigger a push notification.
# (The dashboard always shows every slot.)  Times are 24-hour "HH:MM".
# A slot matches if it starts at/after "start" AND ends at/before "end".
PREFERRED_WINDOWS = {
    "weekday": ("18:00", "20:00"),   # Monday-Friday
    "weekend": ("09:00", "19:00"),   # Saturday-Sunday
}

# ============================================================
# 2. VENUE LIST  (add / remove venues by editing this list)
# ============================================================
# platform_type is one of: "clubspark", "royalparks", "playtomic"
#   clubspark_slug : the bit of the ClubSpark URL after clubspark.lta.org.uk/
#   booking_url    : where the notification / "Book" button sends you
#   display_url    : the venue's info page
# To REMOVE a venue, delete its block (or put a # at the start of each line).

VENUES = [
    {
        "name": "Archbishop's Park",
        "platform_type": "clubspark",
        "clubspark_slug": "ArchbishopsParkLambethNorth",
        "booking_url": "https://clubspark.lta.org.uk/ArchbishopsParkLambethNorth/Booking/BookByDate",
        "display_url": "https://clubspark.lta.org.uk/ArchbishopsParkLambethNorth",
    },
    {
        "name": "Geraldine Mary Harmsworth",
        "platform_type": "clubspark",
        "clubspark_slug": "GeraldineMaryHarmsworth",
        "booking_url": "https://clubspark.lta.org.uk/GeraldineMaryHarmsworth/Booking/bookbycourt",
        "display_url": "https://clubspark.lta.org.uk/GeraldineMaryHarmsworth",
    },
    {
        "name": "Battersea Park",
        "platform_type": "clubspark",
        "clubspark_slug": "BatterseaParkTennisCourts",
        "booking_url": "https://clubspark.lta.org.uk/BatterseaParkTennisCourts/Booking/BookByDate",
        "display_url": "https://clubspark.lta.org.uk/BatterseaParkTennisCourts",
    },
    {
        "name": "Eel Brook Common",
        "platform_type": "clubspark",
        "clubspark_slug": "eelbrookcommon",
        "booking_url": "https://clubspark.lta.org.uk/eelbrookcommon/Booking",
        "display_url": "https://clubspark.lta.org.uk/eelbrookcommon",
    },
    {
        "name": "South Park",
        "platform_type": "clubspark",
        "clubspark_slug": "SouthParkFulham",
        "booking_url": "https://clubspark.lta.org.uk/SouthParkFulham/Booking/BookByDate",
        "display_url": "https://clubspark.lta.org.uk/SouthParkFulham",
    },
    {
        "name": "Hurlingham Park",
        "platform_type": "clubspark",
        "clubspark_slug": "HurlinghamPark",
        "booking_url": "https://clubspark.lta.org.uk/HurlinghamPark/Booking/BookByDate",
        "display_url": "https://clubspark.lta.org.uk/HurlinghamPark/PricesandInfo",
    },
    {
        # Regent's Park's booking link points at ClubSpark, so we read it that way.
        "name": "Regent's Park",
        "platform_type": "royalparks",
        "clubspark_slug": "RegentsPark",
        "booking_url": "https://clubspark.lta.org.uk/RegentsPark/Booking/",
        "display_url": "https://sportsandleisure.royalparks.org.uk/regents-park/sports-activities/adult-tennis",
    },
    {
        # No known public data feed for Hyde Park yet. It will show as "not
        # checked" on the dashboard until you add a clubspark_slug here
        # (if it turns out to be on ClubSpark) or a custom reader below.
        "name": "Hyde Park",
        "platform_type": "royalparks",
        "clubspark_slug": None,
        "booking_url": "https://sportsandleisure.royalparks.org.uk/quick-booking",
        "display_url": "https://sportsandleisure.royalparks.org.uk/hyde-park/sports-activities/adult-tennis",
    },
    {
        "name": "Bishop's Park (Rocks Lane)",
        "platform_type": "playtomic",
        "playtomic_tenant_id": None,                 # filled in automatically by name search
        "playtomic_search": "Rocks Lane",            # used for that search
        "booking_url": "https://playtomic.com/clubs/rocks-lane-bishops-park",
        "display_url": "https://playtomic.com/clubs/rocks-lane-bishops-park",
    },
]

# ============================================================
# 3. SMALL HELPERS
# ============================================================

SEEN_FILE = "seen_slots.json"
SLOTS_FILE = "slots.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal court checker)", "Accept": "application/json, */*"}


def hhmm(minutes):
    """Turn minutes-after-midnight (e.g. 1080) into '18:00'."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def make_slot(venue, court, day, start, end, uid=""):
    """Build one slot record. day='YYYY-MM-DD', start/end='HH:MM'."""
    return {
        "id": f"{venue['name']}|{uid or court}|{day}|{start}",
        "venue": venue["name"],
        "court": court,
        "date": day,
        "start": start,
        "end": end,
        "link": venue["booking_url"],
    }


def log(msg):
    print(msg, flush=True)


# ============================================================
# 4. VENUE READERS (one per platform type)
# ============================================================

async def fetch_clubspark(session, venue, today):
    """
    ClubSpark's booking page loads its grid from a JSON address. We call that
    address directly for the whole week in a single request.
    A session with Capacity > 0 is bookable; booked ones have Capacity 0.
    """
    slug = venue.get("clubspark_slug")
    if not slug:
        raise NotImplementedError("no known public data feed for this venue yet")

    url = f"https://clubspark.lta.org.uk/{slug}/Booking/GetVenueSessions"
    params = {
        "resourceID": "",
        "startDate": today.isoformat(),
        "endDate": (today + timedelta(days=DAYS_AHEAD - 1)).isoformat(),
        "roleId": "",
    }
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    slots = []
    for res in data.get("Resources", []):
        court = res.get("Name") or f"Court {res.get('Number', '?')}"
        for day in res.get("Days", []):
            day_str = day["Date"][:10]
            for s in day.get("Sessions", []):
                if (s.get("Capacity") or 0) <= 0:
                    continue  # already booked
                step = s.get("Interval") or 60
                t = s["StartTime"]
                while t + step <= s["EndTime"]:
                    slots.append(make_slot(venue, court, day_str, hhmm(t), hhmm(t + step)))
                    t += step
    return slots


async def fetch_royalparks(session, venue, today):
    """Royal Parks venues: use ClubSpark if the venue has a slug, else say so."""
    return await fetch_clubspark(session, venue, today)


async def fetch_playtomic(session, venue, today):
    """
    Playtomic has a public availability API. We (1) find the club's id by name
    if we don't have it, then (2) ask for each day's free slots in parallel.
    Playtomic times are in UTC, so we convert to London time.
    """
    tenant_id = venue.get("playtomic_tenant_id")
    if not tenant_id:
        async with session.get(
            "https://api.playtomic.io/v1/tenants",
            params={"tenant_name": venue["playtomic_search"], "sport_id": "TENNIS"},
        ) as resp:
            resp.raise_for_status()
            tenants = await resp.json(content_type=None)
        if not tenants:
            raise RuntimeError("Playtomic club not found by name search")
        tenant_id = tenants[0]["tenant_id"]
        venue["playtomic_tenant_id"] = tenant_id

    async def one_day(day):
        params = {
            "user_id": "me",
            "tenant_id": tenant_id,
            "sport_id": "TENNIS",
            "local_start_min": f"{day.isoformat()}T00:00:00",
            "local_start_max": f"{day.isoformat()}T23:59:59",
        }
        async with session.get("https://api.playtomic.io/v1/availability", params=params) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    results = await asyncio.gather(*[one_day(d) for d in days], return_exceptions=True)

    slots = []
    ok_days = 0
    for result in results:
        if isinstance(result, Exception):
            continue
        ok_days += 1
        for res in result:
            rid = str(res.get("resource_id", ""))[:8]
            for s in res.get("slots", []):
                start_utc = datetime.fromisoformat(f"{res['start_date']}T{s['start_time']}").replace(tzinfo=timezone.utc)
                start_local = start_utc.astimezone(LONDON)
                end_local = start_local + timedelta(minutes=s.get("duration", 60))
                slots.append(make_slot(
                    venue, "Court (see app)", start_local.date().isoformat(),
                    start_local.strftime("%H:%M"), end_local.strftime("%H:%M"), uid=rid,
                ))
    if ok_days == 0:
        raise RuntimeError("all Playtomic day requests failed")
    return slots


READERS = {
    "clubspark": fetch_clubspark,
    "royalparks": fetch_royalparks,
    "playtomic": fetch_playtomic,
}


async def check_venue(session, venue, today):
    """Run one venue's reader. Never raises: failures are recorded instead."""
    try:
        reader = READERS[venue["platform_type"]]
        slots = await asyncio.wait_for(reader(session, venue, today), VENUE_BUDGET)
        log(f"  OK      {venue['name']}: {len(slots)} free slots")
        return {"venue": venue, "ok": True, "slots": slots, "error": None}
    except Exception as exc:  # noqa: BLE001 - we really do want to catch everything
        msg = f"{type(exc).__name__}: {exc}"
        log(f"  FAILED  {venue['name']}: {msg}")
        return {"venue": venue, "ok": False, "slots": [], "error": msg}


# ============================================================
# 5. NOTIFICATIONS
# ============================================================

def in_preferred_window(slot):
    """True if the slot is inside the weekday/weekend window you chose."""
    weekday = datetime.strptime(slot["date"], "%Y-%m-%d").weekday()  # Mon=0 ... Sun=6
    start_limit, end_limit = PREFERRED_WINDOWS["weekend" if weekday >= 5 else "weekday"]
    return slot["start"] >= start_limit and slot["end"] <= end_limit


def pretty_date(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")


async def send_ntfy(session, topic, venue_name, slots):
    """One push notification per venue, listing all its new slots."""
    lines = [f"{s['court']} · {pretty_date(s['date'])} · {s['start']}–{s['end']}" for s in slots]
    payload = {
        "topic": topic,
        "title": "🎾 Court Available!",
        "message": f"{venue_name}\n" + "\n".join(lines),
        "priority": 4,                 # 4 = "high" (phone buzzes)
        "tags": ["tennis"],
        "click": slots[0]["link"],     # tapping the notification opens the booking page
        "actions": [{"action": "view", "label": "Book", "url": slots[0]["link"]}],
    }
    async with session.post("https://ntfy.sh/", json=payload) as resp:
        resp.raise_for_status()


# ============================================================
# 6. MAIN
# ============================================================

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


async def main(dry_run=False):
    now = datetime.now(LONDON)
    today = now.date()
    log(f"Checking {len(VENUES)} venues at {now:%Y-%m-%d %H:%M} London time")

    timeout = aiohttp.ClientTimeout(total=VENUE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
        results = await asyncio.gather(*[check_venue(session, v, today) for v in VENUES])

        # Gather every free slot, dropping any that have already started.
        all_slots = []
        for r in results:
            for s in r["slots"]:
                start_dt = datetime.strptime(f"{s['date']} {s['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
                if start_dt > now:
                    all_slots.append(s)
        all_slots.sort(key=lambda s: (s["date"], s["start"], s["venue"], s["court"]))

        # --- New slot detection -------------------------------------------
        seen = load_json(SEEN_FILE, {})
        current_ids = {s["id"] for s in all_slots}
        failed_venues = {r["venue"]["name"] for r in results if not r["ok"]}

        new_slots = [s for s in all_slots if in_preferred_window(s) and s["id"] not in seen]

        # --- Send notifications (grouped by venue) --------------------------
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        by_venue = {}
        for s in new_slots:
            by_venue.setdefault(s["venue"], []).append(s)

        notified_ids = set()
        if new_slots and dry_run:
            log(f"DRY RUN: would notify about {len(new_slots)} new slots in {len(by_venue)} venues")
            for s in new_slots:
                log(f"   {s['venue']} | {s['court']} | {s['date']} {s['start']}-{s['end']}")
        elif new_slots and not topic:
            log("NTFY_TOPIC is not set - skipping notifications")
        else:
            for venue_name, slots in by_venue.items():
                try:
                    await send_ntfy(session, topic, venue_name, slots)
                    notified_ids.update(s["id"] for s in slots)
                    log(f"  Notified: {venue_name} ({len(slots)} slots)")
                except Exception as exc:  # noqa: BLE001
                    log(f"  Notification FAILED for {venue_name}: {exc}")

    # --- Update the "already told you" memory -----------------------------
    # Keep an entry only while the slot is still free (so a cancelled-then-
    # re-released slot alerts you again), or while its venue is failing
    # (so a blip doesn't cause repeat alerts), and never past its date.
    if not dry_run:
        kept = {}
        for sid, info in seen.items():
            if info["date"] < today.isoformat():
                continue
            if sid in current_ids or info["venue"] in failed_venues:
                kept[sid] = info
        for s in all_slots:
            if s["id"] in notified_ids:
                kept[s["id"]] = {"date": s["date"], "venue": s["venue"]}
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=1)

    # --- Write the dashboard data -------------------------------------------
    venue_status = [
        {
            "name": r["venue"]["name"],
            "ok": r["ok"],
            "error": r["error"],
            "display_url": r["venue"]["display_url"],
            "booking_url": r["venue"]["booking_url"],
        }
        for r in results
    ]
    previous = load_json(SLOTS_FILE, {})
    content = {"venues": venue_status, "slots": all_slots}
    # Only change the "updated" time when something actually changed, so the
    # workflow doesn't make a new commit every 5 minutes for nothing.
    if {k: previous.get(k) for k in content} != content:
        out = {"updated": now.isoformat(timespec="seconds"), **content}
    else:
        out = {"updated": previous.get("updated", now.isoformat(timespec="seconds")), **content}
    with open(SLOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)

    ok = sum(1 for r in results if r["ok"])
    log(f"Done: {ok}/{len(VENUES)} venues OK, {len(all_slots)} free slots, {len(new_slots)} new in preferred times")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
