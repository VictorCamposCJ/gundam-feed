# Gundam Feed

1. Sube esta carpeta a un repo de GitHub.
2. Telegram: habla con @BotFather → /newbot → copia el token. Escribe algo a tu bot y abre
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver tu `chat.id`.
3. Repo → Settings → Secrets and variables → Actions: crea los secrets `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
   (Opcional, en Variables: `WATCH` = `rg nu, mg sazabi` para limitar los avisos de reposición a esas palabras.)
4. Settings → Pages → Deploy from branch → main / root.
5. Actions → "Actualizar productos" → Run workflow. Revisa el log: las tiendas con ERR necesitan ajustar su adaptador en `STORES`.

Feed RSS: `data/feed.xml`. Prueba en local: `pip install requests beautifulsoup4 && python scraper.py`.
