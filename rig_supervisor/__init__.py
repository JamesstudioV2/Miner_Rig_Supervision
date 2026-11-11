"""Rig Supervisor - vízhűtéses Antminer bányatelep felügyelő rendszer Raspberry Pi-re.

A csomag egy önálló (külső függőség nélküli) Python alkalmazás, amely
 - vezérli a két keringető szivattyút (SSR-en keresztül),
 - figyeli az áramláskapcsolókat, a 4 db NTC vízhőmérő szenzort és a fizikai gombokat,
 - LAN-on lekérdezi és vezérli a minereket,
 - naplózza az adatokat, és
 - webes felületet szolgáltat (telefonra és számítógépre optimalizálva).
"""

__version__ = "1.0.0"
APP_NAME = "Rig Supervisor"
