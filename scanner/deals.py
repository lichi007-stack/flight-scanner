"""לוגיקת זיהוי עסקאות - פונקציות טהורות ללא קריאות רשת, כדי שיהיה קל לבדוק אותן."""
from __future__ import annotations

import statistics
from datetime import date, datetime
from urllib.parse import quote

ORIGIN = "TLV"
# התראה גם כשהמחיר נמוך ב-X% מהחציון ההיסטורי (גם אם הוא מעל התקרה שהוגדרה)
BELOW_TYPICAL_RATIO = 0.75
MIN_HISTORY_POINTS = 10
# לא שולחים התראה חוזרת על אותה עסקה אלא אם המחיר ירד לפחות בשיעור הזה
REALERT_DROP_RATIO = 0.95


def month_range(start: str, end: str) -> list[str]:
    """('2026-11', '2027-01') -> ['2026-11', '2026-12', '2027-01']"""
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def filter_offers(offers: list[dict], watch: dict, today: date | None = None) -> list[dict]:
    """משאיר רק הצעות שמתאימות לטווח ימי החופשה, לחודשים ולסינון טיסה ישירה."""
    today = today or date.today()
    months = set(month_range(watch["month_from"], watch["month_to"]))
    kept = []
    for o in offers:
        if not o.get("departure_at") or not o.get("return_at") or o.get("price") is None:
            continue
        dep, ret = _parse_date(o["departure_at"]), _parse_date(o["return_at"])
        if dep < today:
            continue
        if dep.strftime("%Y-%m") not in months:
            continue
        days = (ret - dep).days
        if not (watch["min_days"] <= days <= watch["max_days"]):
            continue
        if watch.get("direct_only") and o.get("transfers", 0) != 0:
            continue
        kept.append({**o, "trip_days": days})
    return kept


def cheapest(offers: list[dict]) -> dict | None:
    return min(offers, key=lambda o: o["price"]) if offers else None


def evaluate(best: dict | None, watch: dict, history_prices: list[float]) -> list[str]:
    """מחזיר רשימת סיבות להתראה (ריקה = אין התראה)."""
    if best is None:
        return []
    price = best["price"]
    reasons = []

    if price <= watch["max_price"]:
        reasons.append("under_max")

    if len(history_prices) >= MIN_HISTORY_POINTS:
        typical = statistics.median(history_prices)
        if price <= typical * BELOW_TYPICAL_RATIO:
            reasons.append("below_typical")

    if not reasons:
        return []

    # מניעת כפילויות: אותה טיסה/מחיר שכבר דווחו, או ירידה קטנה מדי
    last = watch.get("last_alert_price")
    same_flight = (
        watch.get("last_alert_departure") == best["departure_at"][:10]
        and watch.get("last_alert_return") == best["return_at"][:10]
    )
    if last is not None:
        if same_flight and price >= last:
            return []
        if not same_flight and price >= last * REALERT_DROP_RATIO:
            return []
    return reasons


def typical_price(history_prices: list[float]) -> float | None:
    if len(history_prices) < MIN_HISTORY_POINTS:
        return None
    return statistics.median(history_prices)


def google_flights_link(dest: str, dep: str, ret: str) -> str:
    q = f"Flights from {ORIGIN} to {dest} on {dep} through {ret}"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def format_message(watch: dict, best: dict, reasons: list[str],
                   typical: float | None, currency: str) -> str:
    sym = {"USD": "$", "ILS": "₪", "EUR": "€"}.get(currency.upper(), currency.upper() + " ")
    dep, ret = best["departure_at"][:10], best["return_at"][:10]
    name = watch.get("destination_name") or watch["destination"]
    lines = [f"✈️ טיסה זולה ל{name}!",
             f"מחיר הלוך-חזור: {sym}{round(best['price'])}"]
    if "under_max" in reasons:
        lines.append(f"(מתחת לתקרה שלך: {sym}{round(watch['max_price'])})")
    if "below_typical" in reasons and typical:
        pct = round((1 - best["price"] / typical) * 100)
        lines.append(f"נמוך ב-{pct}% מהמחיר הרגיל ליעד ({sym}{round(typical)})")
    lines.append(f"📅 {dep} ← {ret} ({best['trip_days']} ימים)")
    lines.append("טיסה ישירה" if best.get("transfers", 0) == 0
                 else f"{best['transfers']} עצירות")
    if best.get("airline"):
        lines.append(f"חברה: {best['airline']}")
    if best.get("link"):
        lines.append("להזמנה: https://www.aviasales.com" + best["link"])
    lines.append("השוואה בגוגל: " + google_flights_link(watch["destination"], dep, ret))
    lines.append("⚠️ המחיר מבוסס על מטמון ועלול להשתנות - כדאי לאמת לפני הזמנה.")
    return "\n".join(lines)
