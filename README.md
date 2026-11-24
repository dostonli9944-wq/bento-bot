# Bento — Telegram Mini App bot

Mijoz botni ochadi → **🍱 Меню** tugmasi → bugungi taomlarni savatga qo'shadi → ism, telefon, manzil → **Буюртма бериш**.
Buyurtma Telegram guruhga tushadi. Operator **✅ Қабул қилинди** va **🚚 Йўлга чиқди** tugmalarini bosadi, mijozga avtomatik xabar boradi.

Qoidalar:
- Buyurtma faqat **09:00–12:00** (Toshkent vaqti) oralig'ida qabul qilinadi.
- **3 va undan ko'p porsiya** bo'lsa yetkazish bepul, kam bo'lsa `DELIVERY_FEE` (standart 15 000 so'm, o'zgartirasiz).
- Mini app matnlari kirillda.

---

## 1-qadam. Bot ochish
1. Telegram'da **@BotFather** → `/newbot` → nom: `Bento — тушлик`, username: masalan `BentoObedBot`.
2. Berilgan **tokenni** saqlab qo'ying, hech kimga bermang.
3. `/setuserpic` bilan Bento logotipini qo'ying, `/setdescription` bilan qisqa tavsif yozing.

## 2-qadam. Buyurtmalar guruhi
1. Telegram'da yangi guruh oching (masalan "Bento buyurtmalar") va operatorlarni qo'shing.
2. Botni guruhga qo'shing va uni **admin** qiling. "Pin messages" huquqi bo'lishi shart, chunki menyu pin qilingan xabarda saqlanadi.

## 3-qadam. Serverga joylash (Render.com)
1. github.com'da akkaunt oching → yangi repo yarating → shu papkadagi barcha fayllarni yuklang.
2. render.com → GitHub bilan kiring → **New → Web Service** → repo'ni tanlang.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
3. **Environment** bo'limiga kiriting:
   | Kalit | Qiymat |
   |---|---|
   | `BOT_TOKEN` | BotFather bergan token |
   | `GROUP_ID` | hozircha `0` deb turing (5-qadamda to'g'rilaysiz) |
   | `ADMIN_IDS` | hozircha bo'sh |
   | `BASE_URL` | Render bergan manzil, masalan `https://bento-bot.onrender.com` |
   | `DELIVERY_FEE` | `15000` (yoki sizning narxingiz) |
4. **Deploy** tugmasini bosing.

> ⚠️ Render'ning bepul tarifi 15 daqiqa ishlatilmasa "uxlab qoladi". Shundan keyin birinchi ochilishi ~50 soniya kutadi. Sinov uchun bepul tarif yetadi, lekin ishga tushirgandan keyin doim yoniq turadigan pullik tarifga o'ting (Render Starter yoki arzon VPS).

## 4-qadam. ID'larni olish
- Botga shaxsiy chatda `/id` yozing. "Sizning ID" — bu **ADMIN_IDS** uchun. Menyuni yangilaydigan har bir odam shunday qiladi, ID'lar vergul bilan yoziladi.
- Guruhda `/id` yozing. "Chat ID" (`-100...` bilan boshlanadi) — bu **GROUP_ID**.
- Render → Environment'da ikkalasini yangilang, servis o'zi qayta ishga tushadi.

## 5-qadam. Mini app havolasi (reklama uchun)
@BotFather → `/newapp` → botingizni tanlang → nom, tavsif, rasm (640×360) → URL'ga `BASE_URL`'ni kiriting → qisqa nom: `menu`.
Natijada **`https://t.me/BentoObedBot/menu`** havolasi paydo bo'ladi. Uni Meta Ads trafik kampaniyasiga qo'ysangiz, odam bir bosishda menyuga tushadi.

(Bot o'zi ham chatdagi pastki "🍱 Меню" tugmasini avtomatik o'rnatadi.)

---

## Kunlik menyuni yangilash (adminlar)
Bot bilan **shaxsiy chatda** taom rasmini yuborasiz, izohiga (caption) quyidagini yozasiz:

```
1 | Ош | 35000 | Мол гўшти, салат билан
```
Tartib: **raqam | nomi | narxi | tavsif** (tavsif ixtiyoriy). 6 ta taom uchun 1 dan 6 gacha yuborasiz.
Bir xil raqamni qayta yuborsangiz, o'sha taom almashadi. Rasmsiz, faqat matn yuborsangiz, eski rasm qoladi.

| Komanda | Nima qiladi |
|---|---|
| `/menyu` | hozirgi menyuni ko'rsatadi |
| `/ochir 3` | 3-taomni o'chiradi |
| `/tozala` | butun menyuni tozalaydi (ertangi menyudan oldin) |
| `/yordam` | yo'riqnoma |

---

## Lokal sinov (ixtiyoriy)
```
pip install -r requirements.txt
export BOT_TOKEN=... GROUP_ID=... ADMIN_IDS=... USE_POLLING=1 BASE_URL=https://<ngrok-manzil>
python main.py
```
