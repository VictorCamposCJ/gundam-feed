"""Recopila Gunpla de tiendas españolas, detecta novedades/reposiciones,
avisa por Telegram y genera data/productos.json + data/feed.xml."""
import json, os, re, sys, time, html, unicodedata, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
import requests
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin
from bs4 import BeautifulSoup

# type: "shopify" (usa /products.json) o "prestashop" (HTML, selectores habituales).
# Si una tienda falla, el log lo dice y se conservan sus datos anteriores.
STORES = [
    # type "shopify": usa /products.json | "html": página de categoría o de búsqueda fija (url)
    # "rev": súbelo (3, 4...) cuando cambies la url de una tienda: esa lectura cuenta como inicial y no manda avisos falsos.
    {"name": "Robotines",    "base": "https://www.robotines.com",     "type": "html", "url": "https://www.robotines.com/es/35-maquetas-gundam"},
    {"name": "Lost Bunker",  "base": "https://lostbunker.com",        "type": "shopify"},
    {"name": "La Escotilla", "base": "https://laescotillajuegos.com", "type": "shopify"},
    {"name": "Gold Saucer",  "base": "https://goldsaucerstore.es",    "type": "html", "url": "https://goldsaucerstore.es/60-model-kits-gundam"},
    {"name": "1001Hobbies",  "base": "https://www.1001hobbies.es",    "type": "html", "url": "https://www.1001hobbies.es/26736-gunpla"},
    {"name": "Pixelatoy",        "base": "https://www.pixelatoy.com",   "type": "html", "url": "https://www.pixelatoy.com/es/334-gunpla"},
    {"name": "Victoriano Yoake", "base": "https://victorianoyoake.com", "type": "shopify"},
    {"name": "Impact Store", "base": "https://impactstore.es", "type": "woo", "url": "https://impactstore.es/?s=gundam&post_type=product&dgwt_wcas=1", "rev": 3},
    {"name": "Kurogami", "base": "https://kurogami.com", "type": "html", "url": "https://kurogami.com/es/familia/figuras-bandai?tipo_stock=en_stock&licencias%5B%5D=113", "rev": 4},
    {"name": "Geosan Battle", "base": "https://geosanbattle.com", "type": "woo", "url": "https://geosanbattle.com/?s=gundam&post_type=product&dgwt_wcas=1&e-page-c069240=1", "fallback": "https://geosanbattle.com/categoria-producto/gundam/", "paginar": "param:e-page-c069240", "solo_gundam": True, "rev": 4},
    {"name": "Gunpla Loompa", "base": "https://gunplaloompa.com", "type": "html", "url": "https://gunplaloompa.com/model-kits-3/?q=Disponibilidad-En+stock", "solo_gundam": True, "pausa": 5, "max_paginas": 80, "rev": 4},
    {"name": "Gunpla.es", "base": "https://gunpla.es", "type": "html", "url": "https://gunpla.es/en/shop?category=&search=gundam&order=name+asc", "rev": 2},
    {"name": "Mecha Universe", "base": "https://www.mechauniverse.com/es", "type": "html", "url": "https://www.mechauniverse.com/es/gunpla-1411", "stock_url": "https://www.mechauniverse.com/es/gunpla-1411?out_of_stock=1", "completa_cada_h": 6, "rev": 3},
    {"name": "La Cueva Roja", "base": "https://lacuevaroja.com", "type": "html", "url": "https://lacuevaroja.com/bandai", "solo_gundam": True},
    # Empire Games devuelve 403 a los accesos automáticos (bloqueo de la tienda): desactivada.
    # {"name": "Empire Games", "base": "https://empiregames.es", "type": "auto"},
]

GUNDAM_RX = re.compile(r"gundam|gunpla|\b(?:hg|rg|mg|pg|eg|sd|mgsd)[a-z]{0,4}\b", re.I)  # para categorías de marca (p. ej. /bandai)
KEYWORDS = re.compile(r"gundam|gunpla|bandai|\b(hg|rg|mg|pg|sd|eg)\b|model kit|maqueta", re.I)
ACCESORIO = (r"\bGD\s?-?\d{2,3}\b|decals?\b|calcas?\b|pegatinas?|stickers?|marcador|marker|rotulador|pintura|\bpaint\b|spray|barniz|"
             r"imprimaci|cutter|nipper|alicate|herramienta|\btools?\b|pincel|aer[oó]grafo|peana|expositor|display stand|action base|"
             r"stand set|builders? parts|option parts|option set|weapons? (set|unit|pack)|gundam colou?r|\blibro\b|\bbook\b|revista|magazine")
NON_GUNDAM = re.compile(r"\b30 ?M[FSM]\b|armored core|frame arms|megami|zoids|\brosan\b|\bkotobukiya\b", re.I)

def keep(p):
    """Descarta líneas de Bandai/otras marcas que no son Gundam (salvo que el título diga Gundam/Gunpla)."""
    t = p["title"]
    return not (NON_GUNDAM.search(t) and not re.search(r"gundam|gunpla", t, re.I))

GRADES = [("Accesorio", ACCESORIO), ("PG", r"\bPG\b|perfect grade"), ("MG", r"\bMG\b|master grade"), ("RG", r"\bRG\b|real grade"),
          ("HG", r"\bHG\w*\b|high grade"), ("SD", r"\bSD\b|\bSDCS\b|super deformed"),
          ("EG", r"\bEG\b|entry grade"), ("Figura", r"figure|figura|gff|gundam fix")]
UA = {"User-Agent": "Mozilla/5.0 (compatible; GundamFeedBot/1.0; uso personal)"}
DATA = Path("data"); DATA.mkdir(exist_ok=True)
USED = {}  # fuente realmente usada por cada tienda en esta ejecución
BASELINE = "2000-01-01T00:00:00+00:00"  # fecha de los productos ya existentes al empezar a seguir una tienda

def grade(title):
    for g, rx in GRADES:
        if re.search(rx, title, re.I): return g
    return "Otro"

RESPECT_ROBOTS = True  # se ajusta con "respetar_robots" en config.json
BOT = "gundamfeedbot"
_ROBOTS = {}

class RobotsBlocked(requests.RequestException):
    pass

def parse_robots(text):
    """Reglas Allow/Disallow que aplican a este bot (o a '*'), con comodines * y $."""
    groups, agents, rules, in_rules = [], [], [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line: continue
        k, v = (x.strip() for x in line.split(":", 1)); k = k.lower()
        if k == "user-agent":
            if in_rules: groups.append((agents, rules)); agents, rules, in_rules = [], [], False
            agents.append(v.lower())
        elif k in ("allow", "disallow"):
            in_rules = True
            if v: rules.append((k == "allow", v))
    groups.append((agents, rules))
    pick = [g for g in groups if any(BOT in a for a in g[0])] or [g for g in groups if "*" in g[0]]
    return [r for g in pick for r in g[1]]

def rule_matches(pattern, target):
    rx = re.escape(pattern).replace(r"\*", ".*")
    if rx.endswith(r"\$"): rx = rx[:-2] + "$"
    return re.match(rx, target) is not None

def robots_allows(url):
    u = urlparse(url); origin = f"{u.scheme}://{u.netloc}"
    if origin not in _ROBOTS:
        rules = []
        try:
            r = requests.get(origin + "/robots.txt", headers=UA, timeout=20)
            if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower(): rules = parse_robots(r.text)
        except requests.RequestException:
            pass
        _ROBOTS[origin] = rules
    target = (u.path or "/") + (("?" + u.query) if u.query else "")
    best = None  # (longitud de la regla, permitido); gana la más larga y, a igualdad, Allow
    for allow, pat in _ROBOTS[origin]:
        if rule_matches(pat, target) and (best is None or len(pat) > best[0] or (len(pat) == best[0] and allow)):
            best = (len(pat), allow)
    return True if best is None else best[1]

_PAUSE = {}      # dominio -> pausa mínima tras recibir un 429
_DIAG = set()
_LIMITED = set() # dominios que siguieron respondiendo 429 tras los reintentos
_CTX = threading.local()  # límite de tiempo de la tienda que está leyendo cada hilo

class RateLimited(requests.RequestException):
    pass

class TimeBudget(requests.RequestException):
    pass

def get(url, pause=1.5, **kw):
    if RESPECT_ROBOTS and not robots_allows(url):
        print(f"   robots.txt no permite leer: {url}")
        raise RobotsBlocked(url)
    host = urlparse(url).netloc
    dl = getattr(_CTX, "deadline", None)
    def over(extra=0):
        if dl and time.time() + extra > dl:
            if ("t", host) not in _DIAG: _DIAG.add(("t", host)); print(f"   {host}: se alcanzó el tiempo máximo para esta tienda; se detiene la lectura")
            raise TimeBudget(f"tiempo máximo agotado en {host}")
    last, limited = None, False
    for i in range(3):
        over()
        time.sleep(max(pause, _PAUSE.get(host, 0)))
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 429:  # la tienda pide ir más despacio: se respeta
                if host not in _DIAG:
                    _DIAG.add(host)
                    print(f"   {host}: datos del 429 · servidor={r.headers.get('Server')} · cloudflare={'sí' if r.headers.get('CF-RAY') else 'no'}"
                          f" · retry-after={r.headers.get('Retry-After')} · url={url[:90]} · cuerpo={r.text[:90]!r}")
                ra = r.headers.get("Retry-After", "")
                wait = min(int(ra), 60) if ra.isdigit() else 30 * (i + 1)
                _PAUSE[host] = min(max(pause, _PAUSE.get(host, 0)) * 2, 8)
                print(f"   {host}: demasiadas peticiones (429); intento {i + 1} de 3")
                limited = True
                if i < 2: over(wait); time.sleep(wait)
                continue
            limited = False
            r.raise_for_status(); return r
        except TimeBudget:
            raise
        except requests.RequestException as e:
            last = e; limited = False
            if getattr(getattr(e, "response", None), "status_code", 0) in (403, 404): break
    if limited:
        _LIMITED.add(host)
        raise RateLimited(f"429 Too Many Requests en {host}; esta tienda se pone en pausa unas horas")
    raise last

def scrape_shopify(s):
    out = []
    for page in range(1, 21):
        items = get(f"{s['base']}/products.json?limit=250&page={page}").json().get("products", [])
        if not items: break
        for p in items:
            text = f"{p['title']} {p.get('vendor','')} {p.get('product_type','')} {' '.join(p.get('tags',[]))}"
            if not KEYWORDS.search(text): continue
            v = p["variants"]
            out.append({"id": f"{s['name']}:{p['id']}", "store": s["name"], "title": p["title"],
                        "price": min(float(x["price"]) for x in v),
                        "status": shopify_status(p, v), "in_stock": any(x.get("available") for x in v),
                        "image": p["images"][0]["src"] if p.get("images") else "",
                        "url": f"{s['base']}/products/{p['handle']}"})
    USED[s['name']] = 'shopify'
    return out

CARD_SELECTORS = ["article.product-miniature", "li.product", ".product-miniature", ".ajax_block_product",
                  ".product-item", ".product-card", ".product-thumb", "div.product"]
PREORDER = re.compile(r"pre-?order|pre-?venta|pre-?pedido|\breserva(s|r)?\b|pr[oó]ximamente|coming soon", re.I)

def card_text(node):
    """Texto visible + alt de imágenes + title de enlaces (Robotines pone 'Pre-order' en el alt)."""
    return " ".join([node.get_text(" ", strip=True)] + [i.get("alt", "") for i in node.select("img")]
                    + [a.get("title", "") for a in node.select("a[title]")])

def get_status(text, cls=""):
    if SOLD.search(text) or OUT_CLS.search(cls) or "outofstock" in cls: return "out"
    return "preorder" if PREORDER.search(text) else "stock"

def shopify_status(p, v):
    if not any(x.get("available") for x in v): return "out"
    return "preorder" if PREORDER.search(f"{p['title']} {' '.join(p.get('tags', []))} {p.get('product_type', '')}") else "stock"

SOLD = re.compile(r"agotado|sin stock|sin unidades|sin existencias|fuera de stock|no hay unidades|no quedan unidades|out of stock|no disponible|sold out", re.I)
OUT_CLS = re.compile(r"out[-_ ]?of[-_ ]?stock|sold[-_ ]?out|agotado|sin[-_ ]?(stock|unidades)", re.I)

def card_classes(node):
    """Clases CSS de la tarjeta y de todo lo que lleva dentro (p. ej. el distintivo out_of_stock de PrestaShop)."""
    return " ".join(" ".join(n.get("class", [])) for n in [node] + node.find_all(True))
PRICE = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}\s*€|€\s*\d+[.,]\d{2}|\d+[.,]\d{2}\s*€")
BAD = re.compile(r"card|carta|tcg|trading", re.I)
SEARCH_PATHS = ["/buscar?controller=search&s=gundam", "/search?controller=search&s=gundam",
                "/search?q=gundam", "/?s=gundam&post_type=product", "/busqueda?controller=search&s=gundam"]

def to_price(raw):
    m = re.search(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+(?:[.,]\d+)?", raw or "")
    if not m: return 0.0
    t = m.group()
    t = t.replace(".", "").replace(",", ".") if "," in t else t
    return float(t)

def make(s, a, title, price_txt, text, img, extra_cls=""):
    url = requests.compat.urljoin(s["base"], a["href"])
    src = ""
    if img: src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
    return {"id": f"{s['name']}:{url}", "store": s["name"], "title": title, "price": to_price(price_txt),
            "status": get_status(text, extra_cls), "in_stock": get_status(text, extra_cls) != "out",
            "image": requests.compat.urljoin(s["base"], src) if src and not src.startswith("data:") else "", "url": url}

def parse_cards(soup, s):
    for sel in CARD_SELECTORS:
        out = []
        for c in soup.select(sel):
            a = c.select_one(".product-title a, h2 a, h3 a, a.product-name") or c.select_one("a[href]")
            if not a or not a.get("href"): continue
            img = c.select_one("img")
            t = c.select_one(".product-title, .woocommerce-loop-product__title, h2, h3, a.product-name")
            title = (t.get_text(" ", strip=True) if t else "") or a.get_text(" ", strip=True) or a.get("title") or (img.get("alt") if img else "")
            if not title: continue
            pr = c.select_one("[itemprop=price], .price ins .amount, .price .amount, .price, .product-price")
            raw = (pr.get("content") or pr.get_text(" ")) if pr else ""
            out.append(make(s, a, title, raw, card_text(c), img, card_classes(c)))
        if out: return out, sel
    return parse_generic(soup, s), "generic"

def parse_generic(soup, s):
    """Plan B: bloque más pequeño con enlace+imagen y un precio en €, sea cual sea el diseño."""
    out, seen = [], set()
    for img in soup.select("a[href] img"):
        a = img.find_parent("a"); node = a
        for _ in range(6):
            node = node.parent
            if node is None or node.name in ("body", "html"): break
            hrefs = {x["href"] for x in node.select("a[href]") if x.select_one("img")}
            if len(hrefs) > 1: break
            txt = node.get_text(" ", strip=True)
            m = PRICE.search(txt)
            if m:
                links = [x for x in node.select("a[href]") if len(x.get_text(strip=True)) > 8]
                href_a = max(links, key=lambda x: len(x.get_text(strip=True))) if links else a
                title = (href_a.get_text(" ", strip=True) if links else "") or a.get("title") or img.get("alt") or ""
                p = make(s, href_a, title, m.group(), card_text(node), img, card_classes(node))
                if title and p["id"] not in seen: seen.add(p["id"]); out.append(p)
                break
    return out

def gundam_candidates(s):
    soup = BeautifulSoup(get(s["base"]).text, "html.parser"); found = set()
    for a in soup.select("a[href]"):
        href = a["href"]; blob = f"{a.get_text(' ', strip=True)} {href}"
        if not re.search(r"gundam|gunpla", blob, re.I) or BAD.search(blob): continue
        if re.search(r"\d{4,}-.*\.html$|/products?/|/producto/", href): continue
        url = requests.compat.urljoin(s["base"], href)
        found.add((0 if re.search(r"maqueta|model|gunpla|kit", blob, re.I) else 1, len(url), url))
    return [u for _, _, u in sorted(found)][:4]

NEXT_TXT = {"siguiente", "next", "›", "»", "siguiente »", "next »", "siguiente ›", "next ›", ">"}

def next_link(soup, cur=1):
    n = soup.select_one('a[rel=next], link[rel=next], a.next, li.next a, a[aria-label*="next" i], a[aria-label*="siguiente" i]')
    cands = [n] if n else [a for a in soup.select("a[href]") if a.get_text(" ", strip=True).lower() in NEXT_TXT]
    for a in cands:
        h = a.get("href", "")
        if h and not h.startswith(("#", "javascript")): return a
    for a in soup.select("a[href]"):  # paginador numérico: enlace con el número de la página siguiente
        h = a.get("href", "")
        if a.get_text(strip=True) == str(cur + 1) and h and not h.startswith(("#", "javascript")): return a
    return None

PAGE_STYLES = [("param", "page"), ("param", "p"), ("param", "pagina"), ("param", "paged"), ("path", None)]

def parse_style(txt):
    if not txt: return None
    return ("path", None) if txt == "path" else ("param", txt.split(":", 1)[1])

def page_url(url, n, style):
    kind, name = style; u = urlparse(url)
    if kind == "param":
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != name] + [(name, str(n))]
        return urlunparse(u._replace(query=urlencode(q)))
    base = re.sub(r"/page/\d+/?$", "", u.path.rstrip("/"))
    return urlunparse(u._replace(path=f"{base}/page/{n}" + ("/" if u.path.endswith("/") else "")))

def crawl(s, url):
    """Lee TODAS las páginas: sigue el enlace 'siguiente'; si no lo hay, prueba ?page=N, ?p=N, ?pagina=N, ?paged=N y /page/N.
    En config se puede forzar con "paginar": "param:nombre" o "path"."""
    first, out, seen, pages = url, [], set(), 0
    style = parse_style(s.get("paginar"))
    limit = int(s.get("max_paginas", 40)); pause = float(s.get("pausa", 1.5))
    for page in range(limit):
        r = get(url, pause=pause); soup = BeautifulSoup(r.text, "html.parser")
        items, sel = parse_cards(soup, s)
        fresh = [p for p in items if p["id"] not in seen]
        if page == 0 and not items:
            print(f"   {s['name']}: sin productos en {url} · título: {soup.title.string.strip() if soup.title and soup.title.string else '?'}"
                  f" · '€' en HTML: {r.text.count('€')} · ld+json: {'ld+json' in r.text} · bytes: {len(r.text)}")
        if not fresh: break
        seen.update(p["id"] for p in fresh); out += fresh; pages += 1
        if style: url = page_url(first, page + 2, style); continue
        nxt = next_link(soup, page + 1)
        if nxt: url = urljoin(url, nxt["href"]); continue
        if page == 0 and len(fresh) >= 6:  # sin enlace "siguiente": probar patrones habituales de paginación
            for st in PAGE_STYLES:
                cand = page_url(first, 2, st)
                try: items2, _ = parse_cards(BeautifulSoup(get(cand, pause=pause).text, "html.parser"), s)
                except requests.RequestException: continue
                if any(p["id"] not in seen for p in items2):
                    style, url = st, cand
                    print(f"   {s['name']}: paginación detectada ({st[0]}{':' + st[1] if st[1] else ''}); añade \"paginar\" en STORES para fijarla")
                    break
            if style: continue
        break
    print(f"   {s['name']}: {pages} página(s) leída(s)" + (f" · LÍMITE de {limit} alcanzado: puede haber más productos; sube max_paginas" if pages >= limit else ""))
    return out

def ficha_status(page_html):
    """Estado real a partir de la ficha del producto (Odoo): 'out', 'stock', 'preorder' o None si no se puede saber."""
    soup = BeautifulSoup(page_html, "html.parser")
    msg = soup.select_one("#out_of_stock_message")
    if msg and SOLD.search(msg.get_text(" ", strip=True)): return "out"
    w = soup.select_one("#o_wsale_cta_wrapper")  # botón de compra: oculto y con clase out_of_stock cuando no hay unidades
    if w is None: return None
    cls = " ".join(w.get("class", []))
    if "out_of_stock" in cls: return "out"
    if "d-none" in cls: return None
    title = soup.select_one("h1")
    return "preorder" if title and PREORDER.search(title.get_text(" ", strip=True)) else "stock"

def apply_stock_list(s, items, filtered=None):
    """Tiendas con filtro 'ocultar agotados' (stock_url): lo que sale en la lista filtrada tiene stock; lo demás, no."""
    if filtered is None: filtered = crawl(s, s["stock_url"])
    if s.get("solo_gundam"): filtered = [p for p in filtered if GUNDAM_RX.search(p["title"])]
    fmap = {p["id"]: p for p in filtered}
    in_stock = set(fmap)
    ids = {p["id"] for p in items}
    if not in_stock: raise RuntimeError("la lista filtrada por stock salió vacía")
    if len(in_stock) >= 0.98 * len(ids): raise RuntimeError("el filtro de stock no filtra nada (misma cantidad que la lista completa)")
    if len(in_stock & ids) < 0.9 * len(in_stock): raise RuntimeError("la lista filtrada no coincide con la completa (¿cambió la web?)")
    extra = [p for p in filtered if p["id"] not in ids]  # con stock pero ausentes de la lista completa: se añaden
    for p in extra: p["status"] = "preorder" if p["status"] == "preorder" else "stock"; p["in_stock"] = True
    items.extend(extra)
    n_out = 0
    for p in items:
        if p["id"] in in_stock:
            f = fmap[p["id"]]; p["price"], p["title"], p["url"] = f["price"], f["title"], f["url"]; p["image"] = f["image"] or p.get("image", "")
            p["status"] = "preorder" if (p["status"] == "preorder" or f["status"] == "preorder") else "stock"
        elif p["status"] == "preorder": pass  # no sale en la lista con stock, pero su tarjeta indica preventa
        else: p["status"] = "out"; n_out += 1
        p["in_stock"] = p["status"] != "out"; p["sv"] = True; p.pop("unverified", None)  # sv = stock verificado con la lista de la tienda
    print(f"   {s['name']}: {len(in_stock)} con stock y {n_out} agotados según el filtro de la tienda"
          + (f"; {len(extra)} productos solo estaban en la lista filtrada y se han añadido" if extra else ""))

def read_filtered(s):
    """Lee la lista con stock; si la tienda la rechaza (429, bloqueo...) devuelve None sin romper nada."""
    try: return crawl(s, s["stock_url"])
    except requests.RequestException as e:
        print(f"   {s['name']}: no se pudo leer la lista con stock ({e})"); return None

def mark_unverified(items, prev):
    """Sin lista con stock no se sabe el estado real: se conserva lo ya verificado y el resto se muestra 'sin verificar'."""
    for p in items:
        old = prev.get(p["id"])
        if old and old.get("sv"): p["status"], p["in_stock"], p["sv"] = old["status"], old["status"] != "out", True
        elif p["status"] != "out": p["unverified"] = True

def verify_fichas(s, items, prev, now, watch=()):
    """El listado de esta tienda no indica el stock: se abren fichas, máximo s['fichas'] por pasada.
    Prioridad: sin verificar -> agotados (por si se reponen) -> el resto, de la comprobación más antigua a la más reciente."""
    for p in items:
        old = prev.get(p["id"])
        if p["status"] == "out": p["checked_at"] = now  # el propio listado lo marca agotado: es fiable
        elif old and old.get("checked_at"):             # conservar lo ya verificado hasta volver a comprobarlo
            p["status"], p["in_stock"], p["checked_at"] = old["status"], old["status"] != "out", old["checked_at"]
    budget = int(s["fichas"])
    hot = [p for p in items if watch and matches(p["title"], watch)][:budget]  # tu lista de vigilancia: se comprueba en CADA pasada
    hot_ids = {p["id"] for p in hot}
    rest = [p for p in items if p["id"] not in hot_ids]
    unv = [p for p in rest if not p.get("checked_at")]
    out = sorted([p for p in rest if p.get("checked_at") and p["status"] == "out"], key=lambda p: p["checked_at"])
    ok = sorted([p for p in rest if p.get("checked_at") and p["status"] != "out"], key=lambda p: p["checked_at"])
    n_out = min(len(out), (budget - len(hot)) // 3)
    chosen = hot + out[:n_out]; chosen += unv[:budget - len(chosen)]; chosen += ok[:budget - len(chosen)]; chosen += out[n_out:][:budget - len(chosen)]
    checked = changed = 0
    for p in chosen:
        try: st = ficha_status(get(p["url"]).text)
        except requests.RequestException: continue
        if st is None: continue
        checked += 1; changed += st != p["status"]
        p["status"], p["in_stock"], p["checked_at"] = st, st != "out", now
    for p in items:
        if p.get("checked_at"): p.pop("unverified", None)
        else: p["unverified"] = True
    pend = sum(1 for p in items if p.get("unverified"))
    print(f"   {s['name']}: {checked} fichas comprobadas, {changed} con estado distinto al del listado; sin verificar aún: {pend} de {len(items)}")

def scrape_woo(s):
    """WooCommerce: API pública de productos. Si no está disponible, se usa la página HTML (url / fallback)."""
    out = []
    try:
        for page in range(1, 81):
            r = get(f"{s['base']}/wp-json/wc/store/v1/products?search=gundam&per_page=100&page={page}")
            data = r.json()
            if not isinstance(data, list) or not data: break
            total_pages = int(r.headers.get("X-WP-TotalPages", 0) or 0)
            if page == 1: api_total, received, dropped = r.headers.get("X-WP-Total", "?"), 0, []
            received += len(data)
            for p in data:
                pr = p.get("prices") or {}
                price = float(pr.get("price") or 0) / (10 ** int(pr.get("currency_minor_unit", 2)))
                name = html.unescape(p.get("name", ""))
                cats = " ".join(f"{c.get('name', '')} {c.get('slug', '')}" for c in p.get("categories", []))
                if s.get("solo_gundam") and not re.search(r"gundam|gunpla", f"{name} {cats}", re.I):  # tienda generalista
                    if len(dropped) < 3: dropped.append(name)
                    continue
                back = p.get("is_on_backorder") or (p.get("stock_availability") or {}).get("class") == "available-on-backorder"
                st = "out" if not p.get("is_in_stock", True) else ("preorder" if back or PREORDER.search(f"{name} {cats}") else "stock")
                out.append({"id": f"{s['name']}:{p['id']}", "store": s["name"], "title": name, "price": price,
                            "status": st, "in_stock": st != "out",
                            "image": ((p.get("images") or [{}])[0]).get("src", ""), "url": p.get("permalink", "")})
            if len(data) < 100 or (total_pages and page >= total_pages): break
    except (requests.RequestException, ValueError, KeyError, TypeError):
        out = []
    out = [p for p in out if p["url"] and (s.get("solo_gundam") or KEYWORDS.search(p["title"]))]
    if out:
        print(f"   {s['name']}: leída con la API de WooCommerce · la tienda declara {api_total} resultados, leídos {received}, "
              f"guardados {len(out)} · ejemplos guardados: {[p['title'][:40] for p in out[:3]]} · ejemplos descartados: {[d[:40] for d in dropped]}")
        USED[s["name"]] = "woo-api"; return out
    print(f"   {s['name']}: la API de WooCommerce no respondió; uso la página HTML")
    return scrape_html(s)

def scrape_auto(s):
    try:
        items = scrape_shopify(s)
        if items: print(f"   {s['name']}: detectada como Shopify"); return items
    except (requests.RequestException, ValueError, KeyError):
        pass
    return scrape_html(s)

def scrape_html(s):
    if s.get("url"): cands = [(s["url"], bool(re.search(r"[?&](s|q|search|texto|mot_q)=", s["url"])))]
    else:
        cands = []
        try: cands = [(u, False) for u in gundam_candidates(s)]
        except requests.RequestException as e: print(f"   {s['name']}: portada no disponible ({e})")
        cands += [(s["base"] + p, True) for p in SEARCH_PATHS]
    if s.get("url") and s.get("fallback"):
        cands.append((s["fallback"], bool(re.search(r"[?&](s|q|search|texto|mot_q)=", s["fallback"]))))
    for url, is_search in cands:
        try: items = crawl(s, url)
        except requests.RequestException: continue
        if is_search:
            all_items = items; n_all = len(items); items = [p for p in items if KEYWORDS.search(p["title"])]
            print(f"   {s['name']}: {n_all} resultados de búsqueda, {len(items)} tras filtrar por palabras clave")
            if n_all and not items: print(f"   {s['name']}: ejemplos descartados: {[p['title'][:45] for p in all_items[:5]]}")
        if s.get("solo_gundam") and items:  # categoría de marca con más cosas aparte de Gundam
            n_cat = len(items); items = [p for p in items if GUNDAM_RX.search(p["title"])]
            print(f"   {s['name']}: {n_cat} productos en la categoría, {len(items)} son de Gundam/Gunpla")
        if items: print(f"AUTO url {s['name']}: {url}"); USED[s["name"]] = url; return items
    return []

def telegram(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat): print("(sin Telegram configurado)\n" + text); return
    for i in range(0, len(text), 3800):
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                      json={"chat_id": chat, "text": text[i:i+3800], "parse_mode": "HTML",
                            "disable_web_page_preview": True})

DEFAULT_CFG = {"avisar_novedades": True, "avisar_reposiciones": True, "avisar_preorders": True,
               "solo_lista_vigilancia": False, "vigilar": [], "ignorar": [], "respetar_robots": True}

def load_cfg():
    try: return {**DEFAULT_CFG, **json.loads(Path("config.json").read_text(encoding="utf-8"))}
    except FileNotFoundError: return dict(DEFAULT_CFG)
    except ValueError as e: print(f"config.json no es válido ({e}); uso valores por defecto", file=sys.stderr); return dict(DEFAULT_CFG)

def words(t):
    t = unicodedata.normalize("NFKD", str(t).lower()).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", t)

def matches(title, entries):
    """True si TODAS las palabras de alguna entrada aparecen como palabras completas en el título."""
    tw = set(words(title))
    return any(e and all(w in tw for w in e) for e in (words(x) for x in entries))

def main():
    global RESPECT_ROBOTS
    t0 = time.time()
    cfg = load_cfg()
    RESPECT_ROBOTS = bool(cfg["respetar_robots"])
    if not RESPECT_ROBOTS: print("robots.txt: no se comprueba (respetar_robots = false en config.json)")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch0 = list(cfg["vigilar"]) + [w.strip() for w in os.getenv("WATCH", "").split(",") if w.strip()]
    f = DATA / "productos.json"
    est = DATA / "estado.json"
    old_p = json.loads(f.read_text()) if f.exists() else {}
    old_e = json.loads(est.read_text()) if est.exists() else {}
    prev = {p["id"]: p for p in old_p.get("products", [])}
    counts = dict(old_e.get("counts", old_p.get("counts", {})))
    sources = dict(old_e.get("sources", old_p.get("sources", {})))
    known_stores = {p["store"] for p in prev.values()}
    current, new, restock, notes, errors = {}, [], [], {}, []
    runners = {"shopify": scrape_shopify, "auto": scrape_auto, "woo": scrape_woo}

    full_info = dict(old_e.get("full", {})); full_done = {}
    cooldown = dict(old_e.get("pausa_hasta", {}))
    def cache_fresh(s):
        """True si la lista completa de esta tienda se leyó hace menos de 'completa_cada_h' horas."""
        h, info = s.get("completa_cada_h"), full_info.get(s["name"])
        if not (h and s.get("stock_url") and info and info.get("rev") == s.get("rev", 1)): return False
        if not any(p["store"] == s["name"] for p in prev.values()): return False
        return (datetime.fromisoformat(now) - datetime.fromisoformat(info["at"])).total_seconds() / 3600 < h

    def read_store(s):
        _CTX.deadline = time.time() + float(s.get("max_minutos", 10)) * 60  # una tienda lenta no puede bloquear al resto
        try:
            if cooldown.get(s["name"], "") > now: raise RuntimeError(f"en pausa hasta {cooldown[s['name']]} por un 429 anterior (para no insistir)")
            if cache_fresh(s):  # solo se relee la lista con stock; el resto sale de la última lectura completa
                filtered = read_filtered(s)
                if filtered is None: raise RuntimeError("no se pudo leer la lista con stock; se conservan los datos anteriores")
                items = [dict(p) for p in prev.values() if p["store"] == s["name"]]
                apply_stock_list(s, items, filtered)
                print(f"   {s['name']}: lista completa en caché (< {s['completa_cada_h']} h); solo se ha leído la lista con stock")
            else:
                filtered = read_filtered(s) if s.get("stock_url") else None  # primero la lista con stock (pocas peticiones)
                items = runners.get(s["type"], scrape_html)(s)
                if s.get("stock_url") and items:
                    if filtered is not None: apply_stock_list(s, items, filtered)
                    else: mark_unverified(items, prev); print(f"   {s['name']}: sin lista con stock; los productos se muestran como 'sin verificar'")
                if s.get("completa_cada_h") and items: full_done[s["name"]] = {"at": now, "rev": s.get("rev", 1)}
            if s.get("fichas") and items: verify_fichas(s, items, prev, now, watch0)
            return s, items, None
        except Exception as e: return s, None, e
    with ThreadPoolExecutor(max_workers=len(STORES)) as ex:  # todas las tiendas a la vez; cada una mantiene su ritmo
        results = list(ex.map(read_store, STORES))

    for s in STORES:  # tiendas que siguieron respondiendo 429: pausa de 3 h antes de volver a probar
        if urlparse(s["base"]).netloc in _LIMITED:
            cooldown[s["name"]] = (datetime.fromisoformat(now) + timedelta(hours=3)).isoformat(timespec="seconds")
    cooldown = {k: v for k, v in cooldown.items() if v > now}

    for s, items, err in results:
        try:
            if err is not None: raise err
            items = [p for p in items if keep(p)]
            rev = s.get("rev", 1)
            sig = f"{rev}|{USED.get(s['name'], s.get('url', s['type']))}"
            stored = sources.get(s["name"])
            rebase = (stored is not None and stored != sig) or (stored is None and rev > 1 and s["name"] in known_stores)
            n0 = counts.get(s["name"], 0)
            changed = rebase
            if not rebase and n0 >= 20 and len(items) < 0.7 * n0:
                raise RuntimeError(f"solo {len(items)} productos frente a {n0} la vez anterior: lectura incompleta, se conservan los datos anteriores")
            if not items: raise RuntimeError("0 productos (¿cambió la web o el selector?)")
            grew = not rebase and n0 >= 10 and len(items) > 1.5 * n0 + 10  # p. ej. se arregló la paginación
            rebase = rebase or grew
            print(f"OK  {s['name']}: {len(items)}" + ("  (fuente cambiada: lectura inicial, sin avisos)" if changed else f"  (crecimiento brusco de {n0} a {len(items)}: lectura inicial, sin avisos)" if grew else "")); counts[s["name"]] = len(items); sources[s["name"]] = sig
            if s["name"] in full_done: full_info[s["name"]] = full_done[s["name"]]
        except Exception as e:
            print(f"ERR {s['name']}: {e}", file=sys.stderr); errors.append(s["name"])
            for k, v in prev.items():  # se conservan los datos anteriores; si su stock nunca se verificó, se marca
                if v["store"] == s["name"] and keep(v): current[k] = {**v, "unverified": True} if (s.get("stock_url") and not v.get("sv") and v.get("status") != "out") else v
            continue
        first_read = s["name"] not in known_stores or rebase
        ids = set()
        for p in items:
            ids.add(p["id"]); old = prev.get(p["id"])
            p["first_seen"] = BASELINE if (rebase or (first_read and not old)) else (old["first_seen"] if old else now)
            p["restocked_at"] = None if rebase else (old.get("restocked_at") if old else None)
            current[p["id"]] = p
            if first_read: continue  # primera lectura (o fuente nueva) de una tienda: no avisar
            if not old: new.append(p); continue
            before = old.get("status") or ("stock" if old.get("in_stock") else "out")
            if (before == "out" and p["status"] in ("stock", "preorder")) or (before == "preorder" and p["status"] == "stock"):
                p["restocked_at"] = now; restock.append(p)
                notes[p["id"]] = ("ya a la venta (salió de preorder)" if before == "preorder"
                                  else "de nuevo en preorder" if p["status"] == "preorder" else "repuesto")
        for k, v in prev.items():  # desaparecidos = agotados
            if v["store"] == s["name"] and k not in ids and keep(v) and not rebase:
                current[k] = {**v, "status": "out", "in_stock": False}

    prods = sorted(current.values(), key=lambda p: p["first_seen"], reverse=True)
    for p in prods: p["grade"] = grade(p["title"])
    def write_if_changed(path, text):
        if not path.exists() or path.read_text() != text: path.write_text(text)
    write_if_changed(f, json.dumps({"products": prods}, ensure_ascii=False))  # solo cambia si cambian los productos
    est.write_text(json.dumps({"updated": now, "errors": errors, "counts": counts, "sources": sources, "full": full_info, "pausa_hasta": cooldown}, ensure_ascii=False))

    items = "".join(f"<item><title>{escape(p['title'])} ({p['price']:.2f} €, {escape(p['store'])})</title>"
                    f"<link>{escape(p['url'])}</link><guid>{escape(p['id'])}</guid>"
                    f"<pubDate>{datetime.fromisoformat(p['first_seen']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                    for p in prods[:100])
    write_if_changed(DATA / "feed.xml", '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Novedades Gunpla España</title><link>.</link><description>Novedades de tiendas</description>{items}</channel></rss>")

    print(f"Duración total: {time.time() - t0:.0f} s")

    # ---- Avisos de Telegram, según config.json ----
    watch = list(cfg["vigilar"]) + [w.strip() for w in os.getenv("WATCH", "").split(",") if w.strip()]
    star = lambda p: bool(watch) and matches(p["title"], watch)
    def allowed(p):
        if cfg["ignorar"] and matches(p["title"], cfg["ignorar"]): return False
        return not (p["status"] == "preorder" and not cfg["avisar_preorders"])
    def pick(lst, on):
        lst = [p for p in lst if allowed(p)] if on else []
        return [p for p in lst if star(p)] if cfg["solo_lista_vigilancia"] else lst
    def line(p):
        tag = " · 📦 PREORDER" if p["status"] == "preorder" else ""
        note = f" · {notes[p['id']]}" if p["id"] in notes else ""
        return (f"• {'⭐ ' if star(p) else ''}<a href=\"{html.escape(p['url'])}\">{html.escape(p['title'])}</a>"
                f" — {p['price']:.2f} € ({p['store']}){tag}{note}")
    def section(lst, name, emoji):
        if not lst: return ""
        lst = sorted(lst, key=lambda p: not star(p))
        if len(lst) > 40:
            out = f"{emoji} {len(lst)} {name} de golpe (probable cambio en la lectura de una tienda); no las listo.\n"
            return out + "".join(line(p) + "\n" for p in lst if star(p))[:1500] + "\n"
        return f"{emoji} <b>{name.capitalize()} ({len(lst)})</b>\n" + "\n".join(map(line, lst[:25])) + ("\n…" if len(lst) > 25 else "") + "\n\n"
    msg = section(pick(new, cfg["avisar_novedades"]), "novedades", "🆕") + section(pick(restock, cfg["avisar_reposiciones"]), "reposiciones", "✅")
    if msg.strip(): telegram(msg.strip())

if __name__ == "__main__":
    main()
