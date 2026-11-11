"""Error / warning code catalogue.

Code families
  E1xx  vízáramlás hibák                  -> vészleállás
  E2xx  hőmérséklet hibák                 -> vészleállás (ha be van állítva)
  E3xx  hardver hibák
  W2xx  hőmérséklet figyelmeztetések      -> nincs leállás
  W3xx  hardver figyelmeztetések
  W4xx  miner figyelmeztetések            -> SOHA nem állítja le a rendszert
  I5xx  tájékoztató állapotok
"""

from __future__ import annotations

SENSOR_INDEX = {"t1": 1, "t2": 2, "t3": 3, "t4": 4}

CATALOG = {
    "E101": "Belső kör: nem indult meg a vízáramlás a szivattyú bekapcsolása után",
    "E102": "Belső kör: megszűnt a vízáramlás üzem közben",
    "E103": "Külső kör: nem indult meg a vízáramlás a szivattyú bekapcsolása után",
    "E104": "Külső kör: megszűnt a vízáramlás üzem közben",
    "E201": "T1 vízhőmérséklet a megengedett tartományon kívül",
    "E202": "T2 vízhőmérséklet a megengedett tartományon kívül",
    "E203": "T3 vízhőmérséklet a megengedett tartományon kívül",
    "E204": "T4 vízhőmérséklet a megengedett tartományon kívül",
    "E211": "T1 hőmérséklet-szenzor meghibásodott",
    "E212": "T2 hőmérséklet-szenzor meghibásodott",
    "E213": "T3 hőmérséklet-szenzor meghibásodott",
    "E214": "T4 hőmérséklet-szenzor meghibásodott",
    "E301": "GPIO hiba – a szivattyúk nem vezérelhetők",
    "W201": "T1 vízhőmérséklet a tartományon kívül",
    "W202": "T2 vízhőmérséklet a tartományon kívül",
    "W203": "T3 vízhőmérséklet a tartományon kívül",
    "W204": "T4 vízhőmérséklet a tartományon kívül",
    "W211": "T1 szenzorhiba",
    "W212": "T2 szenzorhiba",
    "W213": "T3 szenzorhiba",
    "W214": "T4 szenzorhiba",
    "W301": "Az ADC (analóg-digitális átalakító) nem elérhető",
    "W302": "Konfigurációs figyelmeztetés",
    "W303": "Áramlásfigyelés kikapcsolva",
    "W304": "Beragadt nyomógomb",
    "W401": "Miner nem elérhető",
    "W402": "Miner hibát jelez",
    "W403": "Miner parancs sikertelen",
    "W404": "Miner nem vezérelhető (csak olvasható típus)",
    "I501": "Utóhűtés aktív",
    "I502": "Kézi szivattyú üzem (légtelenítés)",
    "I503": "Szimulációs mód",
}


def flow_code(loop: int, never_started: bool) -> str:
    if loop == 1:
        return "E101" if never_started else "E102"
    return "E103" if never_started else "E104"


def limit_code(sensor_id: str, emergency: bool) -> str:
    return ("E20%d" if emergency else "W20%d") % SENSOR_INDEX[sensor_id]


def fault_code(sensor_id: str, emergency: bool) -> str:
    return ("E21%d" if emergency else "W21%d") % SENSOR_INDEX[sensor_id]


def describe(code: str) -> str:
    return CATALOG.get(code, code)
