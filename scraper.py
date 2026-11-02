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
    {"name": "Robotines",       "base": "https://www.robotines.com",   "type": "prestashop", "url": "https://www.robotines.com/gundam"},
    {"name": "Lost Bunker",     "base": "https://lostbunker.com",      "type": "shopify"},
    {"name": "Gold Saucer",     "base": "https://goldsaucerstore.es",  "type": "shopify"},
    {"name": "Impact Store",    "base": "https://impactstore.es",      "type": "shopify"},
    {"name": "Kurogami",        "base": "https://kurogami.com",        "type": "prestashop", "url": "https://kurogami.com/gundam"},
    {"name": "La Escotilla",    "base": "https://laescotillajuegos.com","type": "shopify"},
    {"name": "1001Hobbies",     "base": "https://www.1001hobbies.es",  "type": "prestashop", "url": "https://www.1001hobbies.es/gunpla.html"},
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
    time.sleep(1.5)  # trato amable con las tiendas
    r = requests.get(url, headers=UA, timeout=25, **kw); r.raise_for_status(); return r

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

def scrape_prestashop(s):
    out, seen = [], set()
    for page in range(1, 11):
        sep = "&" if "?" in s["url"] else "?"
        soup = BeautifulSoup(get(f"{s['url']}{sep}page={page}").text, "html.parser")
        cards = soup.select("article.product-miniature, .product-miniature, li.product, .product_list .ajax_block_product")
        new = 0
        for c in cards:
            a = c.select_one(".product-title a, h2 a, h3 a, a.product-name")
            if not a or not a.get("href"): continue
            url = requests.compat.urljoin(s["base"], a["href"])
            if url in seen: continue
            seen.add(url); new += 1
            pr = c.select_one(".price, .product-price, [itemprop=price]")
            m = re.search(r"[\d.]+,\d{2}|\d+(?:\.\d+)?", (pr.get("content") or pr.get_text()) if pr else "")
            price = float(m.group().replace(".", "").replace(",", ".")) if m else 0.0
            img = c.select_one("img")
            src = (img.get("data-src") or img.get("src") or "") if img else ""
            txt = c.get_text(" ", strip=True).lower()
            sold = bool(re.search(r"agotado|sin stock|out of stock", txt))
            out.append({"id": f"{s['name']}:{url}", "store": s["name"], "title": a.get_text(strip=True),
                        "price": price, "in_stock": not sold, "image": src, "url": url})
        if not new: break
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
            items = scrape_shopify(s) if s["type"] == "shopify" else scrape_prestashop(s)
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
