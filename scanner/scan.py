"""סריקה יומית: קורא מעקבים מ-Supabase, שואל את Travelpayouts, שולח התראה בוואטספ."""
from __future__ import annotations

import os
import smtplib
import sys
import time
from email.message import EmailMessage
from datetime import date

import requests

import deals

TP_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
WA_PHONE = os.environ.get("WHATSAPP_PHONE", "")
WA_KEY = os.environ.get("CALLMEBOT_APIKEY", "")
CURRENCY = os.environ.get("CURRENCY", "usd").lower()
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "") or GMAIL_USER
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}


def sb_get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(),
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_post(path, rows):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(),
                      json=rows, timeout=30)
    r.raise_for_status()


def sb_patch(path, params, data):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=sb_headers(),
                       params=params, json=data, timeout=30)
    r.raise_for_status()


def fetch_offers(destination: str, month: str) -> list[dict]:
    params = {
        "origin": deals.ORIGIN, "destination": destination,
        "departure_at": month, "one_way": "false",
        "currency": CURRENCY, "sorting": "price", "limit": 1000,
        "token": TP_TOKEN,
    }
    r = requests.get(TP_URL, params=params, timeout=30)
    r.raise_for_status()
    body = r.json()
    if not body.get("success", True):
        raise RuntimeError(f"Travelpayouts error: {body}")
    return body.get("data", [])


def send_whatsapp(text: str):
    r = requests.get(CALLMEBOT_URL,
                     params={"phone": WA_PHONE, "text": text, "apikey": WA_KEY},
                     timeout=60)
    r.raise_for_status()


def send_email(text: str):
    first = text.splitlines()[0]
    price = text.splitlines()[1] if len(text.splitlines()) > 1 else ""
    msg = EmailMessage()
    msg["Subject"] = f"{first} {price}".strip()
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content(text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def channels() -> list:
    """ערוצי ההתראה שהוגדרו (לפי משתני הסביבה שקיימים)."""
    out = []
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        out.append(("email", send_email))
    if WA_PHONE and WA_KEY:
        out.append(("whatsapp", send_whatsapp))
    return out


def notify(text: str) -> bool:
    """שולח בכל הערוצים. מחזיר True אם לפחות אחד הצליח."""
    if DRY_RUN:
        print("[DRY RUN] הודעה שהייתה נשלחת:\n" + text)
        return True
    ok = False
    for name, fn in channels():
        try:
            fn(text)
            ok = True
        except Exception as e:
            print(f"  ! שליחה בערוץ {name} נכשלה: {e}", file=sys.stderr)
    return ok


def process_watch(watch: dict):
    name = watch.get("destination_name") or watch["destination"]
    all_offers = []
    for month in deals.month_range(watch["month_from"], watch["month_to"]):
        try:
            all_offers += fetch_offers(watch["destination"], month)
        except Exception as e:  # חודש אחד שנכשל לא מפיל את כל הסריקה
            print(f"  ! {name} {month}: {e}", file=sys.stderr)
        time.sleep(0.3)

    matching = deals.filter_offers(all_offers, watch, date.today())
    best = deals.cheapest(matching)
    if best is None:
        print(f"- {name}: אין הצעות שמתאימות לפרמטרים")
        return

    hist = sb_get("price_history", {
        "watch_id": f"eq.{watch['id']}", "select": "min_price",
        "order": "scanned_on.desc", "limit": 90})
    history_prices = [float(h["min_price"]) for h in hist]

    reasons = deals.evaluate(best, watch, history_prices)
    print(f"- {name}: הזול ביותר {best['price']} (תקרה {watch['max_price']}) "
          f"-> {'התראה ' + ','.join(reasons) if reasons else 'ללא התראה'}")

    if not DRY_RUN:
        # שורה אחת ליום לכל מעקב (upsert לפי watch_id + scanned_on)
        requests.post(
            f"{SUPABASE_URL}/rest/v1/price_history?on_conflict=watch_id,scanned_on",
            headers={**sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=[{"watch_id": watch["id"], "scanned_on": date.today().isoformat(),
                   "min_price": best["price"], "departure_at": best["departure_at"][:10],
                   "return_at": best["return_at"][:10], "transfers": best.get("transfers", 0),
                   "airline": best.get("airline")}],
            timeout=30).raise_for_status()

    if reasons:
        msg = deals.format_message(watch, best, reasons,
                                   deals.typical_price(history_prices), CURRENCY)
        sent = notify(msg)
        if sent and not DRY_RUN:
            sb_patch("watches", {"id": f"eq.{watch['id']}"}, {
                "last_alert_price": best["price"],
                "last_alert_departure": best["departure_at"][:10],
                "last_alert_return": best["return_at"][:10],
                "last_alert_at": date.today().isoformat()})


def main():
    missing = [k for k, v in {"SUPABASE_URL": SUPABASE_URL, "SUPABASE_SERVICE_KEY": SUPABASE_KEY,
                              "TRAVELPAYOUTS_TOKEN": TP_TOKEN}.items() if not v]
    if not DRY_RUN and not channels():
        missing.append("ערוץ התראה (GMAIL_USER+GMAIL_APP_PASSWORD, או WHATSAPP_PHONE+CALLMEBOT_APIKEY)")
    if missing:
        sys.exit("חסרים משתני סביבה: " + ", ".join(missing))

    watches = sb_get("watches", {"active": "eq.true", "select": "*"})
    print(f"סורק {len(watches)} מעקבים ({date.today()})")
    failures = 0
    for w in watches:
        try:
            process_watch(w)
        except Exception as e:
            failures += 1
            print(f"  ! כשל במעקב {w.get('destination')}: {e}", file=sys.stderr)
    if failures == len(watches) and watches:
        sys.exit("כל המעקבים נכשלו")


if __name__ == "__main__":
    main()
