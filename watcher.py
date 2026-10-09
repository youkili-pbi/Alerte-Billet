"""
Surveillance des billets business à prix cassé (Paris / Marseille -> Asie).

Fonctionnement à chaque passage :
  1. Génère toutes les combinaisons origine x destination x date de config.yaml
     (aller-retour ou aller simple selon la période).
  2. Vérifie un lot de `searches_per_run` combinaisons, puis mémorise où il s'est
     arrêté : le passage suivant reprend la suite (rotation complète en quelques heures).
  3. Récupère le prix business le plus bas sur Google Flights (via fast-flights).
  4. Envoie un e-mail récapitulatif si un prix passe sous `max_price`
     (et, en option, en cas de chute brutale vs l'historique).

Usage :
  python watcher.py            # passage réel
  python watcher.py --fake     # données simulées, sans réseau ni e-mail (test)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import smtplib
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
HISTORY_PATH = ROOT / "history.json"
HISTORY_RETENTION_DAYS = 60


# ----------------------------------------------------------------- modèles
@dataclass
class Search:
    origin: str
    destination: str
    depart: date
    ret: date | None          # None = aller simple
    max_stops: int | None
    max_price: float | None

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}|{self.depart}|{self.ret or 'OW'}"

    @property
    def label(self) -> str:
        if self.ret:
            return f"A/R {self.depart:%d/%m/%y} → {self.ret:%d/%m/%y}"
        return f"Aller simple {self.depart:%d/%m/%y}"

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass
class Quote:
    price: float
    airlines: str
    url: str


@dataclass
class Alert:
    search: Search
    quote: Quote
    baseline: float | None
    reason: str


# ----------------------------------------------------------------- config
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_searches(cfg: dict, today: date) -> list[Search]:
    """Toutes les combinaisons origine x destination x date encore dans le futur."""
    searches = []
    for w in cfg["windows"]:
        start = max(date.fromisoformat(str(w["from"])), today + timedelta(days=1))
        end = date.fromisoformat(str(w["to"]))
        d = date.fromisoformat(str(w["from"]))
        dates = []
        while d <= end:  # grille fixe ancrée sur "from" : les mêmes dates reviennent
            if d >= start:
                dates.append(d)
            d += timedelta(days=w["every_days"])
        for origin in cfg["origins"]:
            for dest in cfg["destinations"]:
                for dep in dates:
                    ret = dep + timedelta(days=w["stay_days"]) if w["trip"] == "round-trip" else None
                    searches.append(Search(origin, dest, dep, ret,
                                           cfg.get("max_stops"), cfg.get("max_price")))
    return searches


def next_batch(searches: list[Search], state: dict, size: int) -> list[Search]:
    """Rotation : reprend là où le passage précédent s'est arrêté."""
    if not searches:
        return []
    cursor = state.get("cursor", 0) % len(searches)
    batch = [searches[(cursor + i) % len(searches)] for i in range(min(size, len(searches)))]
    state["cursor"] = (cursor + len(batch)) % len(searches)
    return batch


# ----------------------------------------------------------------- prix
def fetch_quote(s: Search, currency: str) -> Quote | None:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    from fast_flights.exceptions import FlightsNotFound

    legs = [FlightQuery(date=s.depart.isoformat(), from_airport=s.origin,
                        to_airport=s.destination, max_stops=s.max_stops)]
    if s.ret:
        legs.append(FlightQuery(date=s.ret.isoformat(), from_airport=s.destination,
                                to_airport=s.origin, max_stops=s.max_stops))
    query = create_query(
        flights=legs,
        seat="business",
        trip="round-trip" if s.ret else "one-way",
        passengers=Passengers(adults=1),
        language="fr",
        currency=currency,
    )
    try:
        results = get_flights(query)
    except FlightsNotFound:
        return None
    priced = [f for f in results if f.price]
    if not priced:
        return None
    best = min(priced, key=lambda f: f.price)
    return Quote(price=float(best.price), airlines=", ".join(best.airlines), url=query.url())


def fake_quote(s: Search, currency: str) -> Quote:
    """Prix simulés : ~3 000 € (A/R) ou ~1 900 € (aller simple), 5 % de chances d'une 'erreur de prix'."""
    base = 3000 if s.ret else 1900
    price = base * random.uniform(0.85, 1.15)
    if random.random() < 0.05:
        price *= random.uniform(0.35, 0.6)
    return Quote(price=round(price), airlines="Compagnie test", url="https://www.google.com/travel/flights")


# ----------------------------------------------------------------- historique
def load_history() -> dict:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {}


def save_history(history: dict, today: date) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    cleaned = {}
    for key, entry in history.items():
        if key == "_state":
            cleaned[key] = entry
            continue
        depart = date.fromisoformat(key.split("|")[1])
        if depart < today:
            continue  # vol passé : on oublie
        entry["prices"] = [p for p in entry["prices"] if p[0] >= cutoff]
        if entry["prices"]:
            cleaned[key] = entry
    HISTORY_PATH.write_text(json.dumps(cleaned, indent=1, sort_keys=True), encoding="utf-8")


def recent_prices(entries: list, days: int) -> list[float]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [p for ts, p in entries if ts >= cutoff]


def route_baseline(history: dict, route: str, days: int) -> float | None:
    prices = []
    for key, entry in history.items():
        if key.startswith(route + "|"):
            prices += recent_prices(entry["prices"], days)
    return statistics.median(prices) if len(prices) >= 5 else None


# ----------------------------------------------------------------- détection
def evaluate(s: Search, q: Quote, history: dict, det: dict) -> Alert | None:
    entry = history.get(s.key, {"prices": []})
    own = recent_prices(entry["prices"], det["baseline_days"])

    if len(own) >= det["min_observations"]:
        baseline = statistics.median(own)
    else:
        baseline = route_baseline(history, s.route, det["baseline_days"])

    reasons = []
    if s.max_price and q.price < s.max_price:
        reasons.append(f"sous {s.max_price:.0f}")
    if det.get("drop_threshold") and baseline and q.price <= baseline * (1 - det["drop_threshold"]):
        reasons.append(f"−{(1 - q.price / baseline):.0%} vs référence")
    if not reasons:
        return None

    # Anti-spam : pas de nouvelle alerte si le prix n'a pas encore baissé
    last = entry.get("last_alert")
    if last:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])
        if q.price > last["price"] * (1 - det["realert_drop"]) and age.days < det["realert_after_days"]:
            return None

    return Alert(search=s, quote=q, baseline=baseline, reason=" · ".join(reasons))


# ----------------------------------------------------------------- e-mail
def render_email(alerts: list[Alert], currency: str) -> tuple[str, str]:
    best = min(alerts, key=lambda a: a.quote.price)
    subject = (f"✈️ Business à {best.quote.price:.0f} {currency} — {best.search.route} "
               f"({len(alerts)} offre(s))")
    rows = ""
    for a in sorted(alerts, key=lambda a: a.quote.price):
        ref = f"{a.baseline:.0f}" if a.baseline else "—"
        rows += (
            f"<tr><td><b>{a.search.route}</b></td>"
            f"<td>{a.search.label}</td>"
            f"<td><b>{a.quote.price:.0f} {currency}</b></td><td>{ref}</td>"
            f"<td>{a.reason}</td><td>{a.quote.airlines}</td>"
            f"<td><a href='{a.quote.url}'>Voir</a></td></tr>"
        )
    html = f"""
    <p>Billets business, 1 adulte, prix total. Vérifie vite : ce type de tarif disparaît souvent en quelques heures.</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;font-size:14px">
      <tr style="background:#f0f0f0"><th>Trajet</th><th>Dates</th><th>Prix</th><th>Référence</th>
      <th>Motif</th><th>Compagnie(s)</th><th>Lien</th></tr>
      {rows}
    </table>"""
    return subject, html


def send_email(subject: str, html: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)

    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(html, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port) as server:
            server.login(user, password)
            server.sendmail(user, to.split(","), msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, to.split(","), msg.as_string())


# ----------------------------------------------------------------- main
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="données simulées, pas d'e-mail")
    args = parser.parse_args()

    cfg = load_config()
    currency = cfg.get("currency", "EUR")
    det = cfg["detection"]
    today = date.today()
    now = datetime.now(timezone.utc).isoformat()

    history = load_history()
    state = history.setdefault("_state", {})
    all_searches = build_searches(cfg, today)
    searches = next_batch(all_searches, state, cfg.get("searches_per_run", 50))
    alerts: list[Alert] = []
    failures = 0

    print(f"{len(searches)} recherches sur {len(all_searches)} combinaisons "
          f"(rotation complète en {-(-len(all_searches) // max(len(searches), 1))} passages)")
    for s in searches:
        try:
            q = fake_quote(s, currency) if args.fake else fetch_quote(s, currency)
        except Exception as e:  # blocage, changement de format Google…
            failures += 1
            print(f"  ! {s.key} : {type(e).__name__} {e}")
            continue
        if not args.fake:
            time.sleep(random.uniform(3, 7))  # rester discret
        if q is None:
            print(f"  – {s.key} : aucun vol")
            continue

        alert = evaluate(s, q, history, det)  # évalué AVANT d'ajouter le prix du jour
        entry = history.setdefault(s.key, {"prices": []})
        entry["prices"].append([now, q.price])
        flag = ""
        if alert:
            alerts.append(alert)
            entry["last_alert"] = {"ts": now, "price": q.price}
            flag = f"  🔔 {alert.reason}"
        print(f"  {s.key} : {q.price:.0f} {currency}{flag}")

    save_history(history, today)

    if alerts:
        subject, html = render_email(alerts, currency)
        if args.fake:
            print(f"\n[--fake] e-mail non envoyé : {subject}")
        else:
            send_email(subject, html)
            print(f"\nE-mail envoyé : {subject}")
    else:
        print("\nAucune baisse notable.")

    # Échec total = probablement un blocage : on fait échouer le job pour être notifié par GitHub
    if searches and failures == len(searches):
        print("Toutes les recherches ont échoué.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
