#!/usr/bin/env python3
"""
Cinema City (cinemacity.hu) vetítésfigyelő.

Rendszeresen lekérdezi a Cinema City nyilvános adat-API-ját, és értesít,
ha egy keresett filmhez ÚJ vetítési időpont kerül fel.

Telepítés:
    pip install requests

Példák:
    # elérhető mozik listázása
    python cinemacity_figyelo.py --mozik-listazasa

    # film figyelése az összes moziban, 10 percenként, 30 napra előre
    python cinemacity_figyelo.py --film "film_cím"

    # több film egyszerre (szóközzel elválasztva, idézőjelek között)
    python cinemacity_figyelo.py --film "film_cím_1" "film_cím_2" "film_cím_3"

    # több mozi, egyszeri futtatás (pl. cronhoz / Feladatütemezőhöz)
    python cinemacity_figyelo.py --film "film_cím" --mozi "Allee,Westend"

    # csak egy adott dátum (vagy több, vesszővel; vagy tartomány ..-tal)
    python cinemacity_figyelo.py --film "film_cím" --datum 2026-12-05
    python cinemacity_figyelo.py --film "film_cím" --datum 2026-12-05,2026-12-12
    python cinemacity_figyelo.py --film "film_cím" --datum 2026-12-04..2026-12-06

    # csak egyszer
    --egyszer

    # állapot törlése
    --allapot-torles

    # lekérés intervallum (másodperc)
    --intervallum 600
    
Értesítési csatornák (mind opcionális, környezeti változókkal):
    NTFY_TOPIC                       -> push értesítés (https://ntfy.sh)
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID -> Telegram üzenet
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO -> e-mail
Konzolra mindig kiír.

FIGYELEM: a Cinema City nem hivatalos, dokumentálatlan API-t használ az
oldala, ez bármikor változhat. Ha a script hibát jelez, az útvonalakat
a BASE / list_cinemas / fetch_day függvényekben kell módosítani.
"""

import argparse
import json
import os
import smtplib
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

BASE = "https://www.cinemacity.hu/hu/data-api-service/v1/quickbook/10102"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}
STATE_FILE = Path(__file__).with_name("cinemacity_allapot.json")


# ----------------------------- segédfüggvények ---------------------------- #

def norm(s: str) -> str:
    """Kisbetű + ékezetek nélkül, hogy a keresés ékezetfüggetlen legyen."""
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def get_json(url: str, retries: int = 3) -> dict:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Sikertelen lekérdezés: {url} ({last})")


def list_cinemas(days: int) -> list[dict]:
    until = (date.today() + timedelta(days=days)).isoformat()
    url = f"{BASE}/cinemas/with-event/until/{until}?attr=&lang=hu_HU"
    return get_json(url).get("body", {}).get("cinemas", [])


def fetch_day(cinema_id: str, day: date) -> dict:
    url = (f"{BASE}/film-events/in-cinema/{cinema_id}"
           f"/at-date/{day.isoformat()}?attr=&lang=hu_HU")
    return get_json(url).get("body", {})


# ------------------------------- állapot ---------------------------------- #

def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8"))["seen"])
        except (ValueError, KeyError):
            pass
    return set()


def save_state(seen: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ------------------------------ értesítések ------------------------------- #

def notify(title: str, body: str) -> None:
    print(f"\n=== {title} ===\n{body}\n", flush=True)

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                          headers={"Title": title.encode("utf-8")}, timeout=15)
        except requests.RequestException as e:
            print(f"[ntfy hiba] {e}", file=sys.stderr)

    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": f"{title}\n\n{body}"},
                          timeout=15)
        except requests.RequestException as e:
            print(f"[Telegram hiba] {e}", file=sys.stderr)

    host, to = os.getenv("SMTP_HOST"), os.getenv("MAIL_TO")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["To"] = title, to
            msg["From"] = os.getenv("SMTP_USER", to)
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            print(f"[E-mail hiba] {e}", file=sys.stderr)


# ------------------------------ fő logika --------------------------------- #

def check(film_queries: list[str], cinema_filter: list[str], days: int,
          dates: list[date] | None = None) -> list[dict]:
    """Visszaadja a keresett filmekhez tartozó összes jelenlegi vetítést.

    Ha `dates` meg van adva, csak azokat a napokat nézi, különben a mai naptól
    számított `days` napot.
    """
    qs = [norm(x) for x in film_queries]
    if dates:
        check_days = list(dates)
        horizon = max((d - date.today()).days for d in dates)
    else:
        check_days = [date.today() + timedelta(days=o) for o in range(days + 1)]
        horizon = days
    cinemas = list_cinemas(max(horizon, 1))
    if cinema_filter:
        cinemas = [c for c in cinemas
                   if any(norm(f) in norm(c.get("displayName", ""))
                          for f in cinema_filter)]
    if not cinemas:
        raise RuntimeError("Nem található a megadott szűrésnek megfelelő mozi.")

    found = []
    for c in cinemas:
        for day in check_days:
            body = fetch_day(str(c["id"]), day)
            films = {f["id"]: f for f in body.get("films", [])}
            for ev in body.get("events", []):
                film = films.get(ev.get("filmId"))
                if not film:
                    continue
                name_n = norm(film.get("name", ""))
                if not any(q in name_n for q in qs):
                    continue
                found.append({
                    "key": f"{c['id']}:{ev.get('id')}",
                    "mozi": c.get("displayName", str(c["id"])),
                    "film": film.get("name", "?"),
                    "idopont": ev.get("eventDateTime", ""),
                    "formatum": ", ".join(ev.get("attributeIds", [])),
                    "elad_kifogyott": ev.get("soldOut", False),
                    "link": ev.get("bookingLink", ""),
                })
            time.sleep(0.3)  # ne terheljük feleslegesen a szervert
    return found


def format_event(e: dict) -> str:
    try:
        t = datetime.fromisoformat(e["idopont"]).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        t = e["idopont"]
    extra = " [KIFOGYOTT]" if e["elad_kifogyott"] else ""
    fmt = f" ({e['formatum']})" if e["formatum"] else ""
    link = f"\n   {e['link']}" if e["link"] else ""
    return f"- {t} | {e['mozi']} | {e['film']}{fmt}{extra}{link}"


def run_once(args, seen: set[str]) -> set[str]:
    cinema_filter = [x.strip() for x in args.mozi.split(",") if x.strip()]
    events = check(args.film, cinema_filter, args.napok, args.datumok)
    new = [e for e in events if e["key"] not in seen]

    stamp = datetime.now().strftime("%H:%M:%S")
    if new:
        new.sort(key=lambda e: (e["film"], e["idopont"], e["mozi"]))
        filmek = ", ".join(sorted({e["film"] for e in new}))
        if len(filmek) > 70:
            filmek = filmek[:67] + "..."
        notify(f"Új vetítés: {filmek} ({len(new)} db)",
               "\n".join(format_event(e) for e in new))
        seen |= {e["key"] for e in new}
        save_state(seen)
    else:
        print(f"[{stamp}] Nincs új vetítés (összesen {len(events)} ismert).",
              flush=True)
    return seen


def parse_one_date(text: str) -> date:
    """2026-12-05, 2026.12.05 vagy 2026.12.05. formátum."""
    t = text.strip().strip(".").replace(".", "-")
    return datetime.strptime(t, "%Y-%m-%d").date()


def parse_dates(text: str) -> list[date]:
    """Vesszővel elválasztott dátumok és/vagy 'tól..ig' tartományok."""
    out: list[date] = []
    for part in (x.strip() for x in text.split(",") if x.strip()):
        if ".." in part and part.count("..") == 1 and "-" in part:
            a, b = part.split("..", 1)
            start, end = parse_one_date(a), parse_one_date(b)
            if end < start:
                raise ValueError(f"A tartomány vége a kezdete előtt van: {part}")
            out += [start + timedelta(days=i)
                    for i in range((end - start).days + 1)]
        else:
            out.append(parse_one_date(part))
    today = date.today()
    past = [d for d in out if d < today]
    if past:
        print("Figyelmeztetés: múltbeli dátumok kihagyva: "
              + ", ".join(d.isoformat() for d in past), file=sys.stderr)
    return sorted({d for d in out if d >= today})


def main() -> None:
    p = argparse.ArgumentParser(description="Cinema City vetítésfigyelő")
    p.add_argument("--film", nargs="+",
                   help="a film(ek) címének részlete, ékezetfüggetlenül; "
                        "több film szóközzel elválasztva, idézőjelben")
    p.add_argument("--mozi", default="",
                   help="mozi(k) nevének részlete vesszővel elválasztva "
                        "(alapból mind)")
    p.add_argument("--napok", type=int, default=30,
                   help="hány napra előre nézze (alap: 30)")
    p.add_argument("--datum", default="",
                   help="csak ezeket a napokat figyeli: 2026-12-05, "
                        "vesszővel több nap, vagy tartomány: "
                        "2026-12-04..2026-12-06 (felülírja a --napok-at)")
    p.add_argument("--intervallum", type=int, default=600,
                   help="ellenőrzések közti idő másodpercben (alap: 600)")
    p.add_argument("--egyszer", action="store_true",
                   help="csak egyszer fusson le, ne ismételje")
    p.add_argument("--mozik-listazasa", action="store_true",
                   help="kiírja az elérhető mozikat, majd kilép")
    p.add_argument("--allapot-torles", action="store_true",
                   help="törli a már látott vetítések listáját")
    args = p.parse_args()

    if args.mozik_listazasa:
        for c in list_cinemas(args.napok):
            print(f"{c.get('id')}\t{c.get('displayName')}")
        return
    if args.allapot_torles and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("Állapotfájl törölve.")
    if not args.film:
        p.error("add meg a --film paramétert")
    try:
        args.datumok = parse_dates(args.datum) if args.datum else []
    except ValueError as e:
        p.error(f"hibás --datum érték ({e}); formátum: ÉÉÉÉ-HH-NN")
    if args.datum and not args.datumok:
        p.error("a megadott dátum(ok) mind a múltban vannak")

    seen = load_state()
    if args.datumok:
        mikor = ", ".join(d.isoformat() for d in args.datumok)
    else:
        mikor = f"{args.napok} nap"
    print(f"Figyelés: '{' / '.join(args.film)}' | mozi: {args.mozi or 'mind'} | "
          f"{mikor} | {args.intervallum} mp", flush=True)

    while True:
        try:
            seen = run_once(args, seen)
        except Exception as e:  # a figyelő ne álljon le egy hálózati hibától
            print(f"[hiba] {e}", file=sys.stderr, flush=True)
        if args.egyszer:
            break
        time.sleep(args.intervallum)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nLeállítva.")
