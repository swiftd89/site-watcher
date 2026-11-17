import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
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
    "months": 2592000,
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
        ),
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    return resp.text


def ensure_chromium():
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
    )


def fetch_browser(url, playwright, selector="body"):
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            print("Installing Chromium on runner...")
            ensure_chromium()
            browser = playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        else:
            raise

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
        viewport={"width": 1366, "height": 900},
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if selector and selector != "body":
        try:
            page.wait_for_selector(selector, timeout=8000)
        except Exception:
            page.wait_for_timeout(4000)
    else:
        page.wait_for_timeout(3500)
    html = page.content()
    browser.close()
    return html


def send_discord(webhook_url, title, description, url=None, color=3447003):
    if not webhook_url:
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": "Site Watcher"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        embed["url"] = url
    r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    print(f"Discord webhook response: {r.status_code}")


def send_ntfy(topic, title, message, url=None):
    if not topic:
        return
    headers = {"Title": title, "Tags": "bell,eyes"}
    if url:
        headers["Click"] = url
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=15,
    )


def dispatch_notification(site, settings, title, preview, url):
    method = site.get("notify_method", "default")
    target = (site.get("notify_target") or "").strip()
    global_discord = (os.environ.get("DISCORD_WEBHOOK") or settings.get("discord_webhook") or "").strip()
    global_ntfy = (settings.get("ntfy_topic") or "").strip()

    try:
        if method == "none":
            return
        elif method == "discord":
            send_discord(target or global_discord, f"🔔 {title}", f"**Change detected!**\n\n> {preview[:350]}...", url, color=3066993)
        elif method == "ntfy":
            send_ntfy(target or global_ntfy, title, f"Updated preview: {preview[:200]}...", url)
        elif method == "both":
            send_discord(global_discord, f"🔔 {title}", f"**Change detected!**\n\n> {preview[:350]}...", url, color=3066993)
            send_ntfy(global_ntfy, title, f"Updated preview: {preview[:200]}...", url)
        else:
            if global_discord:
                send_discord(global_discord, f"🔔 {title}", f"**Change detected!**\n\n> {preview[:350]}...", url, color=3066993)
            if global_ntfy:
                send_ntfy(global_ntfy, title, f"Updated preview: {preview[:200]}...", url)
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

    pw_manager = None
    changes_count = 0
    checked_names = []

    def get_pw():
        nonlocal pw_manager
        if pw_manager is None:
            pw_manager = sync_playwright().start()
        return pw_manager

    try:
        for site in due_sites:
            site_id = site["id"]
            url = site["url"]
            selector = site.get("selector", "body")
            name = site.get("name", url)
            print(f"Checking {name} ({url})...")

            try:
                extracted = None
                if not site.get("use_browser"):
                    html = fetch_static(url)
                    extracted = clean_text(html, selector)

                if extracted is None:
                    print(f" -> Using JS browser for {name}...")
                    html = fetch_browser(url, get_pw(), selector)
                    extracted = clean_text(html, selector)
                    if extracted is not None:
                        site["use_browser"] = True

                if extracted is None:
                    page_title = BeautifulSoup(html, "html.parser").title
                    title_txt = page_title.get_text(strip=True)[:40] if page_title else "no title"
                    site["status"] = f"error: selector '{selector}' not found (page: {title_txt})"
                    site["last_checked"] = now_iso
                    checked_names.append(f"⚠️ {name} (selector not found)")
                    continue

                content_hash = hashlib.sha256(extracted.encode("utf-8")).hexdigest()
                prev = state.get(site_id)

                if prev is None:
                    state[site_id] = {"hash": content_hash, "snippet": extracted[:350]}
                    site["status"] = "baseline saved"
                    checked_names.append(f"🆕 {name} (baseline saved)")
                elif prev["hash"] != content_hash:
                    state[site_id] = {"hash": content_hash, "snippet": extracted[:350]}
                    site["last_changed"] = now_iso
                    site["status"] = "changed"
                    changes_count += 1
                    dispatch_notification(site, settings, name, extracted, url)
                    print(f" -> CHANGE DETECTED: {name}")
                else:
                    site["status"] = "unchanged"
                    checked_names.append(f"• {name}")

                site["last_checked"] = now_iso
            except Exception as err:
                site["status"] = f"error: {str(err)[:80]}"
                site["last_checked"] = now_iso
                checked_names.append(f"❌ {name} (error)")
                print(f" -> Error on {name}: {err}")
    finally:
        if pw_manager:
            pw_manager.stop()

    if checked_names and changes_count == 0:
        global_discord = (os.environ.get("DISCORD_WEBHOOK") or settings.get("discord_webhook") or "").strip()
        global_ntfy = (settings.get("ntfy_topic") or "").strip()
        summary = "No changes detected on:\n" + "\n".join(checked_names)
        try:
            if global_discord:
                send_discord(global_discord, "✅ Check Complete — No Changes", summary, color=9807270)
            if global_ntfy:
                send_ntfy(global_ntfy, "Check Complete — No Changes", summary)
        except Exception as e:
            print(f"Summary notification error: {e}")

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
