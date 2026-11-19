#!/usr/bin/env python3
"""Tar skjermbilder av sidene og sjekker layoutreglene i CLAUDE.md med tall.

Laget så en agent kan se resultatet av egne endringer og bevise at de holder,
i stedet for bare å beskrive dem. Brukes av Claude i GitHub Actions
(.github/workflows/claude.yml), og virker likt lokalt:

  pip install playwright
  python -m playwright install chromium
  python scripts/skjermbilde.py                      # alle sidene
  python scripts/skjermbilde.py index.html           # bare forsiden
  python scripts/skjermbilde.py index.html --klikk ".snomann" --vent 1500

Scriptet starter en lokal webserver i repoet, åpner sidene i Chromium og
lagrer PNG-er i --ut (standard /tmp/skjermbilder). Infoskjermsidene tas i
1920 x 1080, som TV-en. Forsiden tas også i mobilstørrelse, fordi den åpnes i
vanlige nettlesere.

På infoskjermsidene sjekkes to regler fra CLAUDE.md:
  - ingen .kolonne har scrollHeight over clientHeight (alt får plass)
  - ingen .streker line krysser hverandre
Brudd gir exit-kode 1, så resultatet kan brukes som bevis i en PR.

Med --klikk tas et ekstra bilde etter at elementet er klikket, og etter
--vent millisekunder. Slik kan interaksjoner, som et easter egg, også ses.

Begrensning: Playwright bruker en ny Chromium, mens TV-en kjører Chromium 94.
CSS eller JS som er for ny for TV-en, ser derfor riktig ut her. Det må fortsatt
sjekkes mot caniuse, se CLAUDE.md.
"""

import argparse
import functools
import http.server
import os
import socketserver
import sys
import threading

from playwright.sync_api import sync_playwright

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TV_SIDER = ["betjente.html", "selvbetjente.html", "oslomarka.html"]
ALLE_SIDER = ["index.html"] + TV_SIDER

TV = {"navn": "tv-1920x1080", "width": 1920, "height": 1080}
MOBIL = {"navn": "mobil-390x844", "width": 390, "height": 844}

# Kjøres i siden. Teller kolonner som flyter over og streker som krysser.
# Streker som bare møtes i et endepunkt regnes ikke som kryssing.
SJEKK_JS = """
() => {
  const kolonner = [...document.querySelectorAll('.kolonne')].map((k, i) => ({
    kolonne: k.className + ' #' + i,
    scrollHeight: k.scrollHeight,
    clientHeight: k.clientHeight,
  }));
  const overflyt = kolonner.filter(k => k.scrollHeight > k.clientHeight);

  const linjer = [...document.querySelectorAll('.streker line')].map(l => ({
    a: { x: +l.getAttribute('x1'), y: +l.getAttribute('y1') },
    b: { x: +l.getAttribute('x2'), y: +l.getAttribute('y2') },
  }));
  const kryss = (p, q, r) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
  const krysser = (s, t) => {
    const d1 = kryss(s.a, s.b, t.a), d2 = kryss(s.a, s.b, t.b);
    const d3 = kryss(t.a, t.b, s.a), d4 = kryss(t.a, t.b, s.b);
    return d1 * d2 < 0 && d3 * d4 < 0;
  };
  const kryssinger = [];
  for (let i = 0; i < linjer.length; i++)
    for (let j = i + 1; j < linjer.length; j++)
      if (krysser(linjer[i], linjer[j])) kryssinger.push([i, j]);

  return { kolonner: kolonner.length, overflyt, streker: linjer.length, kryssinger };
}
"""


class StilleHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def start_server():
    """Statisk webserver i repoet på en ledig port, i en egen tråd."""
    handler = functools.partial(StilleHandler, directory=REPO)
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sider", nargs="*", default=ALLE_SIDER, help="sider å ta bilde av (standard: alle)")
    p.add_argument("--ut", default="/tmp/skjermbilder", help="mappe for PNG-ene")
    p.add_argument("--klikk", help="CSS-selektor å klikke før et ekstra bilde")
    p.add_argument("--vent", type=int, default=1000, help="millisekunder å vente etter klikk")
    args = p.parse_args()

    os.makedirs(args.ut, exist_ok=True)
    server = start_server()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    brudd = 0
    klikket = False

    with sync_playwright() as pw:
        nettleser = pw.chromium.launch()
        for side in args.sider:
            storrelser = [TV] if side in TV_SIDER else [TV, MOBIL]
            for st in storrelser:
                sidenavn = side.removesuffix(".html")
                fane = nettleser.new_page(viewport={"width": st["width"], "height": st["height"]})
                feil = []
                fane.on("pageerror", lambda e: feil.append(str(e)))
                fane.goto(base + side, wait_until="networkidle")
                if side in TV_SIDER:
                    fane.wait_for_selector(".kort", timeout=15000)
                fane.wait_for_timeout(500)

                fil = os.path.join(args.ut, f"{sidenavn}-{st['navn']}.png")
                fane.screenshot(path=fil)
                print(f"Bilde: {fil}")

                for f in feil:
                    print(f"  FEIL i JavaScript: {f}")
                    brudd += 1

                if side in TV_SIDER:
                    r = fane.evaluate(SJEKK_JS)
                    ok_kol = not r["overflyt"]
                    ok_str = not r["kryssinger"]
                    print(f"  {'OK ' if ok_kol else 'BRUDD'} {r['kolonner']} kolonner, "
                          f"{len(r['overflyt'])} flyter over")
                    for k in r["overflyt"]:
                        print(f"        {k['kolonne']}: scrollHeight {k['scrollHeight']} > clientHeight {k['clientHeight']}")
                    print(f"  {'OK ' if ok_str else 'BRUDD'} {r['streker']} streker, "
                          f"{len(r['kryssinger'])} kryssinger")
                    brudd += (not ok_kol) + (not ok_str)

                if args.klikk:
                    mal = fane.query_selector(args.klikk)
                    if mal:
                        klikket = True
                        mal.click()
                        fane.wait_for_timeout(args.vent)
                        fil = os.path.join(args.ut, f"{sidenavn}-{st['navn']}-etter-klikk.png")
                        fane.screenshot(path=fil)
                        print(f"Bilde etter klikk på {args.klikk}: {fil}")
                fane.close()
        nettleser.close()
    server.shutdown()

    if args.klikk and not klikket:
        print(f"Fant ikke {args.klikk} på noen av sidene.")
        brudd += 1
    print()
    print("Alt OK." if not brudd else f"{brudd} brudd, se over.")
    print("Husk: Chromium her er nyere enn TV-ens Chromium 94. Sjekk ny CSS og JS mot caniuse.")
    return 1 if brudd else 0


if __name__ == "__main__":
    sys.exit(main())
