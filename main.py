*** Begin Patch
*** Update File: main.py
@@
-BOT_VERSION = "3.1.2-OUTLOOK-ICS"
+BOT_VERSION = "3.1.3-OUTLOOK-ICS"
@@
-HEADERS = {"User-Agent": "SochiSiriusEventsBot/3.1.2"}
+HEADERS = {"User-Agent": "SochiSiriusEventsBot/3.1.3"}
@@
 async def send_highlight_proposals(chat_id):
     candidates = telegram_main_candidates()
-    if len(candidates) < 3:
-        await bot.send_message(
-            chat_id,
-            f"В Outlook помечено только {len(candidates)} из 3 событий "
-            f"с TELEGRAM_MAIN на сегодня.",
-        )
+    # TELEGRAM_MAIN — максимум 3 кандидата, а не обязательные 3.
+    # 0 → ничего не отправляем; 1–3 → показываем все найденные.
+    if not candidates:
+        logger.info("TELEGRAM_MAIN: на сегодня кандидатов нет")
         return

     HIGHLIGHT_PROPOSALS[chat_id] = candidates
     from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
-    keyboard = InlineKeyboardMarkup(inline_keyboard=[
-        [
-            InlineKeyboardButton(text="1", callback_data="highlight:0"),
-            InlineKeyboardButton(text="2", callback_data="highlight:1"),
-            InlineKeyboardButton(text="3", callback_data="highlight:2"),
-        ]
-    ])
+    buttons = [
+        InlineKeyboardButton(text=str(i + 1), callback_data=f"highlight:{i}")
+        for i in range(len(candidates))
+    ]
+    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
+
     text = "<b>Выбери событие для публикации в Telegram</b>\n\n"
     text += "\n\n".join(
         proposal_text(event, i + 1) for i, event in enumerate(candidates)
     )
     await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
@@
 async def daily_schedule_loop():
     sent_daily_day = None
     sent_highlight_day = None
     while True:
         now = datetime.now(MOSCOW_TZ)
         day = now.date()

         # 10:00–10:30: publish the complete Outlook agenda for today.
         if now.hour == 10 and 0 <= now.minute <= 30 and sent_daily_day != day:
-            await publish_daily_summary()
-            sent_daily_day = day
+            try:
+                await publish_daily_summary()
+                sent_daily_day = day
+            except Exception:
+                logger.exception("Ошибка утренней публикации")

-        # 11:00–11:30: offer exactly the three Outlook-marked candidates.
+        # 11:00–11:30: offer 1–3 events marked TELEGRAM_MAIN in Outlook.
+        # Fewer than three is normal:
+        # 1 → one choice, 2 → two choices, 3 → three choices, 0 → nothing.
         if now.hour == 11 and 0 <= now.minute <= 30 and sent_highlight_day != day:
             targets = list(SUBSCRIBERS)
             if targets:
-                await send_highlight_proposals(targets[0])
-            sent_highlight_day = day
+                success = False
+                for chat_id in targets:
+                    try:
+                        await send_highlight_proposals(chat_id)
+                        success = True
+                    except Exception:
+                        logger.exception(
+                            "Не удалось отправить выбор TELEGRAM_MAIN пользователю %s",
+                            chat_id,
+                        )
+                if success:
+                    sent_highlight_day = day

         await asyncio.sleep(30)
*** End Patch
