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
    {"name": "Robotines",    "base": "https://www.robotines.com",    "type": "html", "url": "https://www.robotines.com/es/35-maquetas-gundam"},
    {"name": "Lost Bunker",  "base": "https://lostbunker.com",       "type": "shopify"},
    {"name": "La Escotilla", "base": "https://laescotillajuegos.com","type": "shopify"},
    # Sin URL: el scraper busca solo el enlace "Gundam/Gunpla" de la portada y lo imprime en el log.
    # Cuando veas "AUTO url" correcta, copia esa URL aquí en "url" para fijarla.
    {"name": "Gold Saucer",  "base": "https://goldsaucerstore.es",   "type": "html"},
    {"name": "Impact Store", "base": "https://impactstore.es",       "type": "html"},
    {"name": "Kurogami",     "base": "https://kurogami.com",         "type": "html"},
    {"name": "1001Hobbies",  "base": "https://www.1001hobbies.es",   "type": "html"},
]

KEYWORDS = re.compile(r"gundam|gunpla|bandai|\b(hg|rg|mg|pg|sd|eg)\b|model kit|maqueta", re.I)
GRADES = [("PG", r"\bPG\b|perfect grade"), ("MG", r"\bMG\b|master grade"), ("RG", r"\bRG\b|real grade"),
          ("HG", r"\bHG\w*\b|high grade"), ("SD", r"\bSD\b|\bSDCS\b|super deformed"),
          ("EG", r"\bEG\b|entry grade"), ("Figura", r"figure|figura|gff|gundam fix")]
UA = {"User-Agent": "Mozilla/5.0 (compatible; GundamFeedBot/1.0; uso personal)"}
DATA = Path("data"); DATA.mkdir(exist_ok=True)

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

def find_gundam_url(s):
    soup = BeautifulSoup(get(s["base"]).text, "html.parser")
    best = None
    for a in soup.select("a[href]"):
        label, href = a.get_text(" ", strip=True), a["href"]
        if re.search(r"gundam|gunpla", f"{label} {href}", re.I) and not re.search(r"\d{4,}-.*\.html$|/products?/", href):
            url = requests.compat.urljoin(s["base"], href)
            if best is None or len(url) < len(best): best = url
    if not best: raise RuntimeError("no encontré enlace a Gundam/Gunpla en la portada")
    print(f"AUTO url {s['name']}: {best}")
    return best

def parse_cards(soup, s):
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if cards: break
    else:
        return [], None
    out = []
    for c in cards:
        a = c.select_one(".product-title a, h2 a, h3 a, a.product-name") or c.select_one("a[href]")
        if not a or not a.get("href"): continue
        img = c.select_one("img")
        t = c.select_one(".product-title, .woocommerce-loop-product__title, h2, h3, a.product-name")
        title = (t.get_text(" ", strip=True) if t else "") or a.get_text(" ", strip=True) or a.get("title") or (img.get("alt") if img else "")
        if not title: continue
        pr = c.select_one("[itemprop=price], .price ins .amount, .price .amount, .price, .product-price")
        raw = (pr.get("content") or pr.get_text(" ")) if pr else ""
        m = re.search(r"\d{1,3}(?:\.\d{3})*,\d{2}|\d+(?:\.\d+)?", raw)
        price = float(m.group().replace(".", "").replace(",", ".")) if m and "," in m.group() else (float(m.group()) if m else 0.0)
        src = ""
        if img: src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
        url = requests.compat.urljoin(s["base"], a["href"])
        out.append({"id": f"{s['name']}:{url}", "store": s["name"], "title": title, "price": price,
                    "in_stock": not SOLD.search(c.get_text(" ", strip=True)) and "outofstock" not in " ".join(c.get("class", [])),
                    "image": requests.compat.urljoin(s["base"], src) if src else "", "url": url})
    return out, sel

def scrape_html(s):
    url = s.get("url") or find_gundam_url(s)
    out, seen = [], set()
    for page in range(15):
        soup = BeautifulSoup(get(url).text, "html.parser")
        items, sel = parse_cards(soup, s)
        fresh = [p for p in items if p["id"] not in seen]
        if page == 0 and not items:
            print(f"   {s['name']}: sin tarjetas en {url} · título de página: {soup.title.string.strip() if soup.title and soup.title.string else '?'}")
        if not fresh: break
        seen.update(p["id"] for p in fresh); out += fresh
        nxt = soup.select_one("a[rel=next], link[rel=next], a.next, li.next a")
        if not nxt or not nxt.get("href"): break
        url = requests.compat.urljoin(url, nxt["href"])
    return out

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
    prev = {p["id"]: p for p in json.loads(f.read_text())["products"]} if f.exists() else {}
    known_stores = {p["store"] for p in prev.values()}
    current, new, restock, errors = {}, [], [], []
    watch = [w.strip().lower() for w in os.getenv("WATCH", "").split(",") if w.strip()]

    for s in STORES:
        try:
            items = scrape_shopify(s) if s["type"] == "shopify" else scrape_html(s)
            if not items: raise RuntimeError("0 productos (¿cambió la web o el selector?)")
            print(f"OK  {s['name']}: {len(items)}")
        except Exception as e:
            print(f"ERR {s['name']}: {e}", file=sys.stderr); errors.append(s["name"])
            current.update({k: v for k, v in prev.items() if v["store"] == s["name"]}); continue
        ids = set()
        for p in items:
            ids.add(p["id"]); old = prev.get(p["id"])
            p["first_seen"] = old["first_seen"] if old else now
            current[p["id"]] = p
            if s["name"] in known_stores:  # primera ejecución de una tienda: no avisar
                if not old: new.append(p)
                elif p["in_stock"] and not old["in_stock"]:
                    if not watch or any(w in p["title"].lower() for w in watch): restock.append(p)
        for k, v in prev.items():  # desaparecidos = sin stock
            if v["store"] == s["name"] and k not in ids:
                current[k] = {**v, "in_stock": False}

    prods = sorted(current.values(), key=lambda p: p["first_seen"], reverse=True)
    for p in prods: p["grade"] = grade(p["title"])
    f.write_text(json.dumps({"updated": now, "errors": errors, "products": prods}, ensure_ascii=False))

    items = "".join(f"<item><title>{escape(p['title'])} ({p['price']:.2f} €, {escape(p['store'])})</title>"
                    f"<link>{escape(p['url'])}</link><guid>{escape(p['id'])}</guid>"
                    f"<pubDate>{datetime.fromisoformat(p['first_seen']).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                    for p in prods[:100])
    (DATA / "feed.xml").write_text('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Novedades Gunpla España</title><link>.</link><description>Novedades de tiendas</description>{items}</channel></rss>")

    msg = ""
    if new: msg += f"🆕 <b>Novedades ({len(new)})</b>\n" + "\n".join(map(line, new[:25])) + "\n\n"
    if restock: msg += f"✅ <b>De nuevo en stock ({len(restock)})</b>\n" + "\n".join(map(line, restock[:25]))
    if msg: telegram(msg.strip())

if __name__ == "__main__":
    main()
