import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SITES_FILE = "sites.json"
STATE_FILE = "state.json"

UNIT_SECONDS = {
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
    "weeks": 604800,
    "months": 2592000,  # 30 days
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def clean_text(html, selector="body"):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    target = soup.select_one(selector) if selector else soup.body
    if not target:
        return None
    text = target.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def fetch_static(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.text


def fetch_browser(url, playwright):
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    )
    page = context.new_page()
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)
    html = page.content()
    browser.close()
    return html


def send_discord(webhook_url, title, preview, url):
    if not webhook_url:
        return
    payload = {
        "embeds": [
            {
                "title": f"🔔 {title}",
                "url": url,
                "description": f"**Change detected!**\n\n> {preview[:350]}...",
                "color": 3447003,
                "footer": {"text": "Site Watcher"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }
    requests.post(webhook_url, json=payload, timeout=15)


def send_ntfy(topic, title, preview, url):
    if not topic:
        return
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=f"Updated preview: {preview[:200]}...".encode("utf-8"),
        headers={"Title": title, "Click": url, "Tags": "bell,eyes"},
        timeout=15,
    )


def dispatch_notification(site, settings, title, preview, url):
    method = site.get("notify_method", "default")
    target = (site.get("notify_target") or "").strip()
    global_discord = (settings.get("discord_webhook") or "").strip()
    global_ntfy = (settings.get("ntfy_topic") or "").strip()

    try:
        if method == "none":
            return
        elif method == "discord":
            send_discord(target or global_discord, title, preview, url)
        elif method == "ntfy":
            send_ntfy(target or global_ntfy, title, preview, url)
        elif method == "both":
            send_discord(global_discord, title, preview, url)
            send_ntfy(global_ntfy, title, preview, url)
        else:  # "default" -> uses whichever global channels are configured
            if global_discord:
                send_discord(global_discord, title, preview, url)
            if global_ntfy:
                send_ntfy(global_ntfy, title, preview, url)
    except Exception as e:
        print(f"Notification error for {title}: {e}")


def is_due(site, now_utc, force=False):
    if not site.get("enabled", True):
        return False
    if force or not site.get("last_checked"):
        return True

    try:
        last_dt = datetime.strptime(site["last_checked"], "%Y-%m-%d %H:%M UTC").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True

    val = max(1, int(site.get("interval_value", 1)))
    unit = site.get("interval_unit", "days")
    required_seconds = val * UNIT_SECONDS.get(unit, 86400)
    # Subtract 90s buffer so hourly GitHub cron jitter doesn't skip a cycle
    elapsed = (now_utc - last_dt).total_seconds()
    return elapsed >= max(30, required_seconds - 90)


def run_once(force=False):
    config = load_json(SITES_FILE, {"settings": {}, "sites": []})
    state = load_json(STATE_FILE, {})
    settings = config.get("settings", {})
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    due_sites = [s for s in config.get("sites", []) if is_due(s, now_utc, force=force)]
    if not due_sites:
        print("No sites due for check right now.")
        return

    needs_browser = any(s.get("use_browser") for s in due_sites)
    pw_manager = sync_playwright().start() if needs_browser else None

    try:
        for site in due_sites:
            site_id = site["id"]
            url = site["url"]
            selector = site.get("selector", "body")
            name = site.get("name", url)
            print(f"Checking {name} ({url})...")

            try:
                html = (
                    fetch_browser(url, pw_manager)
                    if site.get("use_browser")
                    else fetch_static(url)
                )
                extracted = clean_text(html, selector)
                if extracted is None:
                    site["status"] = f"error: selector '{selector}' not found"
                    site["last_checked"] = now_iso
                    continue

                content_hash = hashlib.sha256(extracted.encode("utf-8")).hexdigest()
                prev = state.get(site_id)

                if prev is None:
                    state[site_id] = {"hash": content_hash, "snippet": extracted[:350]}
                    site["status"] = "baseline saved"
                elif prev["hash"] != content_hash:
                    state[site_id] = {"hash": content_hash, "snippet": extracted[:350]}
                    site["last_changed"] = now_iso
                    site["status"] = "changed"
                    dispatch_notification(site, settings, name, extracted, url)
                    print(f" -> CHANGE DETECTED: {name}")
                else:
                    site["status"] = "unchanged"

                site["last_checked"] = now_iso
            except Exception as err:
                site["status"] = f"error: {str(err)[:80]}"
                site["last_checked"] = now_iso
                print(f" -> Error on {name}: {err}")
    finally:
        if pw_manager:
            pw_manager.stop()

    save_json(SITES_FILE, config)
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Check all enabled sites immediately")
    parser.add_argument("--loop", action="store_true", help="Run continuously every 60s locally")
    args = parser.parse_args()

    if args.loop:
        print("Running in local 1-minute loop mode (Ctrl+C to stop)...")
        while True:
            run_once(force=False)
            time.sleep(60)
    else:
        run_once(force=args.force)
