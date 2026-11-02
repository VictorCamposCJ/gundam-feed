"""Recopila Gunpla de tiendas españolas, detecta novedades/reposiciones,
avisa por Telegram y genera data/productos.json + data/feed.xml."""
import json, os, re, sys, time, html
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
import requests
from bs4 import BeautifulSoup

# type: "shopify" (usa /products.json) o "prestashop" (HTML, selectores habituales).
# Si una tienda falla, el log lo dice y se conservan sus datos anteriores.
STORES = [
    {"name": "Robotines",    "base": "https://www.robotines.com",     "type": "html", "url": "https://www.robotines.com/es/35-maquetas-gundam"},
    {"name": "Lost Bunker",  "base": "https://lostbunker.com",        "type": "shopify"},
    {"name": "La Escotilla", "base": "https://laescotillajuegos.com", "type": "shopify"},
    {"name": "Gold Saucer",  "base": "https://goldsaucerstore.es",    "type": "html", "url": "https://goldsaucerstore.es/60-model-kits-gundam"},
    {"name": "Impact Store", "base": "https://impactstore.es",        "type": "html", "url": "https://impactstore.es/?s=gundam&post_type=product"},
    {"name": "Kurogami",     "base": "https://kurogami.com",          "type": "html", "url": "https://kurogami.com/buscar?controller=search&s=gundam"},
    {"name": "1001Hobbies",  "base": "https://www.1001hobbies.es",    "type": "html", "url": "https://www.1001hobbies.es/26736-gunpla"},
]

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
BASELINE = "2000-01-01T00:00:00+00:00"  # fecha de los productos ya existentes al empezar a seguir una tienda

def grade(title):
    for g, rx in GRADES:
        if re.search(rx, title, re.I): return g
    return "Otro"

def get(url, **kw):
    last = None
    for i in range(3):
        time.sleep(1.5)
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw); r.raise_for_status(); return r
        except requests.RequestException as e:
            last = e
            if getattr(getattr(e, "response", None), "status_code", 0) in (403, 404): break
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
                        "in_stock": any(x.get("available") for x in v),
                        "image": p["images"][0]["src"] if p.get("images") else "",
                        "url": f"{s['base']}/products/{p['handle']}"})
    return out

CARD_SELECTORS = ["article.product-miniature", "li.product", ".product-miniature", ".ajax_block_product",
                  ".product-item", ".product-card", ".product-thumb", "div.product"]
SOLD = re.compile(r"agotado|sin stock|out of stock|no disponible|sold out", re.I)
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
            "in_stock": not SOLD.search(text) and "outofstock" not in extra_cls,
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
            out.append(make(s, a, title, raw, c.get_text(" ", strip=True), img, " ".join(c.get("class", []))))
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
                p = make(s, href_a, title, m.group(), txt, img)
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

def crawl(s, url):
    out, seen = [], set()
    for page in range(15):
        r = get(url); soup = BeautifulSoup(r.text, "html.parser")
        items, sel = parse_cards(soup, s)
        fresh = [p for p in items if p["id"] not in seen]
        if page == 0 and not items:
            print(f"   {s['name']}: sin productos en {url} · título: {soup.title.string.strip() if soup.title and soup.title.string else '?'}"
                  f" · '€' en HTML: {r.text.count('€')} · ld+json: {'ld+json' in r.text} · bytes: {len(r.text)}")
        if not fresh: break
        print(f"   {s['name']}: página {page + 1} -> {len(fresh)} productos nuevos") if os.getenv("DEBUG_PAGES") else None
        seen.update(p["id"] for p in fresh); out += fresh
        nxt = soup.select_one("a[rel=next], link[rel=next], a.next, li.next a")
        if not nxt or not nxt.get("href"): break
        url = requests.compat.urljoin(url, nxt["href"])
    return out

def scrape_html(s):
    if s.get("url"): cands = [(s["url"], False)]
    else:
        cands = []
        try: cands = [(u, False) for u in gundam_candidates(s)]
        except requests.RequestException as e: print(f"   {s['name']}: portada no disponible ({e})")
        cands += [(s["base"] + p, True) for p in SEARCH_PATHS]
    for url, is_search in cands:
        try: items = crawl(s, url)
        except requests.RequestException: continue
        if is_search: items = [p for p in items if KEYWORDS.search(p["title"])]
        if items: print(f"AUTO url {s['name']}: {url}"); return items
    return []

def telegram(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat): print("(sin Telegram configurado)\n" + text); return
    for i in range(0, len(text), 3800):
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                      json={"chat_id": chat, "text": text[i:i+3800], "parse_mode": "HTML",
                            "disable_web_page_preview": True})

def line(p):
    return f"• <a href=\"{html.escape(p['url'])}\">{html.escape(p['title'])}</a> — {p['price']:.2f} € ({p['store']})"

def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    f = DATA / "productos.json"
    old_data = json.loads(f.read_text()) if f.exists() else {}
    prev = {p["id"]: p for p in old_data.get("products", [])}
    counts = dict(old_data.get("counts", {}))
    known_stores = {p["store"] for p in prev.values()}
    current, new, restock, errors = {}, [], [], []
    watch = [w.strip().lower() for w in os.getenv("WATCH", "").split(",") if w.strip()]

    for s in STORES:
        try:
            items = scrape_shopify(s) if s["type"] == "shopify" else scrape_html(s)
            items = [p for p in items if keep(p)]
            n0 = counts.get(s["name"], 0)
            if n0 >= 20 and len(items) < 0.7 * n0:
                raise RuntimeError(f"solo {len(items)} productos frente a {n0} la vez anterior: lectura incompleta, se conservan los datos anteriores")
            if not items: raise RuntimeError("0 productos (¿cambió la web o el selector?)")
            print(f"OK  {s['name']}: {len(items)}"); counts[s["name"]] = len(items)
        except Exception as e:
            print(f"ERR {s['name']}: {e}", file=sys.stderr); errors.append(s["name"])
            current.update({k: v for k, v in prev.items() if v["store"] == s["name"] and keep(v)}); continue
        ids = set()
        for p in items:
            ids.add(p["id"]); old = prev.get(p["id"])
            p["first_seen"] = old["first_seen"] if old else (now if s["name"] in known_stores else BASELINE)
            current[p["id"]] = p
            if s["name"] in known_stores:  # primera ejecución de una tienda: no avisar
                if not old: new.append(p)
                elif p["in_stock"] and not old["in_stock"]:
                    if not watch or any(w in p["title"].lower() for w in watch): restock.append(p)
        for k, v in prev.items():  # desaparecidos = sin stock
            if v["store"] == s["name"] and k not in ids and keep(v):
                current[k] = {**v, "in_stock": False}

    prods = sorted(current.values(), key=lambda p: p["first_seen"], reverse=True)
    for p in prods: p["grade"] = grade(p["title"])
    f.write_text(json.dumps({"updated": now, "errors": errors, "counts": counts, "products": prods}, ensure_ascii=False))

    items = "".join(f"<item><title>{escape(p['title'])} ({p['price']:.2f} €, {escape(p['store'])})</title>"
                    f"<link>{escape(p['url'])}</link><guid>{escape(p['id'])}</guid>"
                    f"<pubDate>{datetime.fromisoformat(p['first_seen']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                    for p in prods[:100])
    (DATA / "feed.xml").write_text('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Novedades Gunpla España</title><link>.</link><description>Novedades de tiendas</description>{items}</channel></rss>")

    msg = ""
    if len(new) > 40: msg += f"🆕 {len(new)} novedades de golpe (probable cambio en la lectura de una tienda); no las listo.\n\n"
    elif new: msg += f"🆕 <b>Novedades ({len(new)})</b>\n" + "\n".join(map(line, new[:25])) + "\n\n"
    if len(restock) > 40: msg += f"✅ {len(restock)} reposiciones de golpe (probable lectura incompleta antes); no las listo."
    elif restock: msg += f"✅ <b>De nuevo en stock ({len(restock)})</b>\n" + "\n".join(map(line, restock[:25]))
    if msg: telegram(msg.strip())

if __name__ == "__main__":
    main()
