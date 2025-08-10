# ucuzaucak.net → Telegram ücretsiz takip botu

**Amaç:** ucuzaucak.net ana sayfasındaki “ucuz uçak bileti” ilanlarını **10 dakikada bir** tarayıp **sadece yeni** ilanları **Telegram**’a Türkçe mesaj olarak göndermek.

- **Dil:** Python 3 (`requests`, `beautifulsoup4`, `lxml`, `PyYAML`)
- **Zamanlayıcı/Hosting:** GitHub Actions (cron). Yerelde de çalışır.
- **Durum Yönetimi:** `data/state.json` içinde **görülen ilan id’leri** saklanır (idempotent).
- **Gizli Bilgiler:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` **GitHub Secrets** olarak.

---

## 1) Telegram bot oluşturma

1. Telegram’da **@BotFather** ile konuşun → `/newbot`
2. Bir ad ve kullanıcı adı verin. Size bir **bot token** (ör: `123456:ABC...`) döner.
3. Botunuzu kendinize mesaj atmak için kullanılacak **chat_id**’yi öğrenin:
   - Botunuza bir kez **/start** yazın.
   - Tarayıcıda (veya curl ile) şu endpoint’e gidin:  
     `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
   - Dönen JSON içinde `message.chat.id` değeriniz **CHAT_ID**’dir.
   - Alternatif: **@userinfobot** da chat id verir.

> **Not:** Botun size mesaj atabilmesi için önce **siz** bota `/start` yazarak konuşmayı başlatmalısınız.

---

## 2) Repo kurulumu (yerel)

```bash
git clone <repo-url>
cd <repo-folder>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
