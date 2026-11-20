#!/usr/bin/env python3
"""Сборщик задержек вылетов по аэропортам.

Запускается каждые 2 часа (GitHub Actions). Для каждого аэропорта:
  1. берёт табло вылетов за окно [сейчас-5ч, сейчас+1ч] (в первый запуск дня — с 00:00),
  2. сливает рейсы в файл дня docs/data/flights/<IATA>/<дата>.json,
  3. пересчитывает минуты задержки и потери в рублях за сегодняшние местные сутки,
  4. пишет сводку docs/data/latest.json, которую показывает сайт.

Только стандартная библиотека Python 3.9+.
Источник выбирается переменной FLIGHT_SOURCE: "aerodatabox" (по умолчанию, нужен ключ) или "demo".
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "docs" / "data"
KEEP_DAYS = 30

CANCELLED = {"canceled", "cancelled", "canceleduncertain"}
DEPARTED = {"departed", "enroute", "arrived", "approaching", "diverted"}


# ---------------------------------------------------------------- utils

def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def parse_time(value) -> datetime | None:
    """AeroDataBox отдаёт '2026-11-11 09:05+03:00' или '2026-11-11 06:05Z'."""
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("utc") or value.get("local")
        if not value:
            return None
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


# ---------------------------------------------------------------- sources

class AeroDataBox:
    """FIDS-эндпоинт AeroDataBox: https://doc.aerodatabox.com/ (через RapidAPI или API.market).

    Окно запроса — не больше 12 часов. Один вызов = 2 «юнита» тарифа (tier 2).
    """

    def __init__(self):
        self.key = os.environ.get("AERODATABOX_KEY", "").strip()
        if not self.key:
            sys.exit("Нет AERODATABOX_KEY. Добавьте секрет в настройках репозитория или запустите с FLIGHT_SOURCE=demo")
        # По умолчанию — прямой тариф aerodatabox.com. Для RapidAPI или API.Market задайте AERODATABOX_BASE.
        self.base = (os.environ.get("AERODATABOX_BASE") or "https://api.aerodatabox.com").rstrip("/")
        host = urllib.parse.urlparse(self.base).netloc
        if "rapidapi" in host:
            self.headers = {"X-RapidAPI-Key": self.key, "X-RapidAPI-Host": host}
        elif "api.market" in host:
            self.headers = {"x-api-market-key": self.key}
        else:  # https://api.aerodatabox.com
            self.headers = {"X-Api-Key": self.key}
        self.headers["Accept"] = "application/json"

    def departures(self, iata: str, start: datetime, end: datetime, tz: ZoneInfo) -> list[dict]:
        out = []
        cur = start
        while cur < end:
            stop = min(cur + timedelta(hours=12), end)
            out += self._window(iata, cur.astimezone(tz), stop.astimezone(tz))
            cur = stop
        return out

    def _window(self, iata, a: datetime, b: datetime) -> list[dict]:
        fmt = "%Y-%m-%dT%H:%M"
        url = (f"{self.base}/flights/airports/iata/{iata}/{a.strftime(fmt)}/{b.strftime(fmt)}?"
               + urllib.parse.urlencode({
                   "direction": "Departure", "withLeg": "false", "withCancelled": "true",
                   "withCodeshared": "false", "withCargo": "false", "withPrivate": "false",
                   "withLocation": "false"}))
        for attempt in range(4):
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    if r.status == 204:
                        return []
                    body = json.loads(r.read().decode("utf-8") or "{}")
                    return [self._normalize(f) for f in body.get("departures", [])]
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise RuntimeError(f"{iata}: HTTP {e.code} {e.read()[:200]!r}") from None
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise RuntimeError(f"{iata}: {e}") from None
        return []

    @staticmethod
    def _normalize(f: dict) -> dict:
        dep = f.get("departure") or f.get("movement") or {}
        arr = f.get("arrival") or {}
        sched = parse_time(dep.get("scheduledTime") or dep.get("scheduledTimeUtc") or dep.get("scheduledTimeLocal"))
        actual = parse_time(dep.get("revisedTime") or dep.get("actualTimeUtc") or dep.get("actualTimeLocal"))
        # в FIDS-ответе для вылетов аэропорт назначения лежит в movement.airport
        dest = arr.get("airport") or dep.get("airport") or {}
        return {
            "number": (f.get("number") or "").strip(),
            "airline": (f.get("airline") or {}).get("name"),
            "to": dest.get("iata") or dest.get("icao") or dest.get("name"),
            "to_name": dest.get("municipalityName") or dest.get("name"),
            "model": (f.get("aircraft") or {}).get("model"),
            "status": f.get("status") or "Unknown",
            "scheduled": iso(sched),
            "actual": iso(actual),
        }


class Demo:
    """Синтетические данные для проверки сайта без ключа. Детерминированы по аэропорту и дате."""

    MODELS = ["Airbus A320", "Airbus A321", "Boeing 737-800", "Sukhoi Superjet 100", "Airbus A319",
              "Boeing 777-300ER", "Airbus A330-300", "ATR 72", "Embraer 190"]
    AIRLINES = ["Аэрофлот", "Россия", "S7 Airlines", "Победа", "Уральские авиалинии", "Ютэйр",
                "Северный ветер", "Азимут", "Smartavia", "Ред Вингс"]
    DEST = ["SVO", "LED", "AER", "KZN", "SVX", "OVB", "MRV", "KGD", "UFA", "IKT", "KHV", "VVO", "MCX", "KJA"]
    SIZE = {"SVO": 450, "DME": 220, "VKO": 260, "LED": 300, "AER": 170, "SVX": 120, "OVB": 110,
            "KZN": 80, "UFA": 60, "KUF": 55, "MRV": 50, "MCX": 45, "ZIA": 25, "KGD": 70, "KJA": 60,
            "IKT": 45, "KHV": 50, "VVO": 55, "TJM": 35, "CEK": 40, "PEE": 35, "GOJ": 35, "MMK": 25, "OMS": 30}

    def departures(self, iata, start, end, tz):
        day = start.astimezone(tz).date()
        rnd = random.Random(f"{iata}-{day}")
        n = self.SIZE.get(iata, 40)
        bad_day = rnd.random() < 0.25          # иногда у аэропорта «плохой день»
        now = parse_time(os.environ["NOW"]) if os.environ.get("NOW") else datetime.now(timezone.utc)
        out = []
        for i in range(n):
            local = datetime(day.year, day.month, day.day, 5, 0, tzinfo=tz) + timedelta(minutes=int(rnd.random() * 19 * 60))
            sched = local.astimezone(timezone.utc)
            r = rnd.random()
            if r < (0.02 if bad_day else 0.005):
                status, actual = "Canceled", None
            else:
                p_delay = 0.45 if bad_day else 0.18
                delay = int(rnd.expovariate(1 / (70 if bad_day else 30))) if rnd.random() < p_delay else rnd.choice([0, 0, 0, 2, 5, -3])
                actual = sched + timedelta(minutes=delay)
                status = "Departed" if actual <= now else ("Delayed" if delay >= 15 else "Expected")
            if not (start <= sched < end):
                continue
            out.append({
                "number": f"{rnd.choice(['SU','FV','S7','DP','U6','UT','N4','A4','5N','WZ'])} {rnd.randint(10, 6999)}",
                "airline": rnd.choice(self.AIRLINES),
                "to": rnd.choice([d for d in self.DEST if d != iata]),
                "to_name": None,
                "model": rnd.choice(self.MODELS),
                "status": status,
                "scheduled": iso(sched),
                "actual": iso(actual),
            })
        return out


# ---------------------------------------------------------------- methodology

def seats_for(model: str | None, m: dict) -> int:
    if model:
        low = model.lower()
        for key, seats in m["seats_by_model"]:
            if key.lower() in low:
                return seats
    return m["default_seats"]


def evaluate(flight: dict, now: datetime, m: dict) -> dict:
    """Минуты задержки и потери по одному вылету.

    * Отменённые рейсы считаются отдельно, минуты по ним не начисляются.
    * Вылетевший рейс: задержка = фактическое (уточнённое) время отправления − плановое.
    * Не вылетевший рейс, у которого плановое время уже прошло: задержка, накопленная к текущему моменту,
      = min(уточнённое время, сейчас) − плановое; если уточнённого нет — сейчас − плановое.
      При следующем запуске значение обновится.
    * Отрицательные значения (ранний вылет) = 0.
    """
    sched = parse_time(flight.get("scheduled"))
    actual = parse_time(flight.get("actual"))
    status = (flight.get("status") or "").lower()
    res = {"delay_min": 0, "cancelled": False, "final": False, "seats": seats_for(flight.get("model"), m)}
    if status in CANCELLED:
        res["cancelled"] = True
        res["final"] = True
        return res
    if not sched:
        return res
    if status in DEPARTED:
        if actual:
            res["delay_min"] = max(0, round((actual - sched).total_seconds() / 60))
        res["final"] = True
    elif now > sched:
        if actual and actual > sched:
            res["delay_min"] = max(0, round((min(actual, now) - sched).total_seconds() / 60))
        elif not actual:
            # нет данных о вылете — не начисляем, чтобы не завышать
            res["delay_min"] = 0
    if res["delay_min"] < m["count_from_minutes"]:
        res["delay_min"] = 0
    pax = res["seats"] * m["load_factor"]
    res["pax"] = round(pax)
    res["loss_pax"] = pax * res["delay_min"] / 60 * m["passenger_value_of_time_rub_per_hour"]
    res["loss_airline"] = res["seats"] * res["delay_min"] * m["airline_cost_rub_per_seat_minute"]
    return res


# ---------------------------------------------------------------- main

def collect():
    airports = load_json(CONFIG / "airports.json")
    m = load_json(CONFIG / "methodology.json")
    source_name = os.environ.get("FLIGHT_SOURCE", "aerodatabox").lower()
    source = Demo() if source_name == "demo" else AeroDataBox()
    only = {s.strip().upper() for s in os.environ.get("AIRPORTS", "").split(",") if s.strip()}

    now = parse_time(os.environ["NOW"]) if os.environ.get("NOW") else datetime.now(timezone.utc)
    summary_rows, errors = [], []
    for ap in airports:
        if only and ap["iata"] not in only:
            continue
        tz = ZoneInfo(ap["tz"])
        local_now = now.astimezone(tz)
        day = local_now.date()
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
        day_end = day_start + timedelta(days=1)
        path = DATA / "flights" / ap["iata"] / f"{day}.json"
        state = load_json(path) or {"airport": ap["iata"], "date": str(day), "flights": {}, "fetched": []}

        start = day_start if not state["flights"] else max(day_start, now - timedelta(hours=5))
        end = min(day_end, now + timedelta(hours=1))
        try:
            fetched = source.departures(ap["iata"], start, end, tz)
        except Exception as e:  # один аэропорт не должен ронять весь запуск
            errors.append(str(e))
            print("ERROR", e, file=sys.stderr)
            fetched = None

        if fetched is not None:
            for f in fetched:
                if not f.get("scheduled"):
                    continue
                sched = parse_time(f["scheduled"])
                if not (day_start <= sched < day_end):
                    continue
                key = f"{f['number']}|{f['scheduled']}"
                prev = state["flights"].get(key, {})
                if prev.get("actual") and not f.get("actual"):
                    f["actual"] = prev["actual"]
                state["flights"][key] = f
            state["fetched"].append(iso(now))
            state["fetched"] = state["fetched"][-48:]
            save_json(path, state)

        summary_rows.append(summarize(ap, state, now, m, error=fetched is None))
        prune(DATA / "flights" / ap["iata"], day)

    out = {
        "generated_at": iso(now),
        "source": source_name,
        "methodology": {k: v for k, v in m.items() if k not in ("_comment",)},
        "airports": summary_rows,
        "errors": errors,
    }
    save_json(DATA / "latest.json", out)
    append_history(out)
    update_daily(airports, now, m)
    print(f"OK: {len(summary_rows)} аэропортов, ошибок: {len(errors)}")
    if errors and len(errors) == len(summary_rows):
        sys.exit(1)


def summarize(ap, state, now, m, error=False):
    totals = dict(flights=0, departed=0, cancelled=0, delayed=0, delay_min=0, pax_hours=0.0,
                  loss_pax=0.0, loss_airline=0.0)
    worst = []
    for f in state["flights"].values():
        r = evaluate(f, now, m)
        totals["flights"] += 1
        if r["cancelled"]:
            totals["cancelled"] += 1
            continue
        # «вылеты по расписанию»: рейс улетел или его плановое время уже прошло
        sched = parse_time(f.get("scheduled"))
        if r["final"] or (sched and sched <= now):
            totals["departed"] += 1
        if r["delay_min"] >= m["delayed_flight_threshold_min"]:
            totals["delayed"] += 1
        totals["delay_min"] += r["delay_min"]
        totals["pax_hours"] += r["pax"] * r["delay_min"] / 60
        totals["loss_pax"] += r["loss_pax"]
        totals["loss_airline"] += r["loss_airline"]
        if r["delay_min"] > 0:
            worst.append({**f, "delay_min": r["delay_min"], "final": r["final"],
                          "loss": round(r["loss_pax"] + r["loss_airline"])})
    worst.sort(key=lambda x: -x["delay_min"])
    return {
        "iata": ap["iata"], "name": ap["name"], "city": ap["city"], "tz": ap["tz"],
        "date": state["date"],
        "flights": totals["flights"], "departed": totals["departed"],
        "cancelled": totals["cancelled"], "delayed": totals["delayed"],
        "delay_min": totals["delay_min"],
        "pax_hours": round(totals["pax_hours"]),
        "loss_pax": round(totals["loss_pax"]),
        "loss_airline": round(totals["loss_airline"]),
        "loss_total": round(totals["loss_pax"] + totals["loss_airline"]),
        "last_fetch": state["fetched"][-1] if state.get("fetched") else None,
        "stale": error,
        "worst": worst[:10],
    }


DAILY_KEYS = ("flights", "departed", "cancelled", "delayed", "delay_min", "pax_hours",
              "loss_pax", "loss_airline", "loss_total")


def update_daily(airports, now, m):
    """Архив итогов по суткам: docs/data/daily.json = {дата: {IATA: итоги}}. Хранится бессрочно.

    Пересчитывается из всех файлов рейсов, которые ещё лежат на диске (последние 30 дней),
    поэтому вчерашние сутки получают окончательные цифры. Более старые записи не трогаются.
    """
    path = DATA / "daily.json"
    daily = load_json(path, {})
    for ap in airports:
        folder = DATA / "flights" / ap["iata"]
        for f in sorted(folder.glob("*.json")) if folder.exists() else []:
            state = load_json(f)
            if not state or not state.get("flights"):
                continue
            # прошедшие сутки замораживаются в одну минуту для всех аэропортов — в полночь
            # по Калининграду (самый западный пояс). Восточные аэропорты к этому моменту
            # уже давно в следующих сутках, поэтому итог за вчера больше не меняется.
            d = datetime.strptime(state["date"], "%Y-%m-%d")
            cutoff = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo("Europe/Kaliningrad")) + timedelta(days=1)
            row = summarize(ap, state, min(now, cutoff.astimezone(timezone.utc)), m)
            daily.setdefault(state["date"], {})[ap["iata"]] = {k: row[k] for k in DAILY_KEYS}
    daily = dict(sorted(daily.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(daily, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def append_history(out):
    """Почасовой срез итогов по каждому аэропорту за сегодня — для графика."""
    path = DATA / "history.json"
    hist = load_json(path, {})
    for row in out["airports"]:
        series = hist.setdefault(row["iata"], {}).setdefault(row["date"], [])
        series.append([out["generated_at"], row["delay_min"], row["loss_total"]])
    # оставляем последние 7 дат
    for iata, days in hist.items():
        for d in sorted(days)[:-7]:
            del days[d]
    save_json(path, hist)


def prune(folder: Path, today):
    if not folder.exists():
        return
    cutoff = today - timedelta(days=KEEP_DAYS)
    for p in folder.glob("*.json"):
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d").date() < cutoff:
                p.unlink()
        except ValueError:
            pass


if __name__ == "__main__":
    collect()
