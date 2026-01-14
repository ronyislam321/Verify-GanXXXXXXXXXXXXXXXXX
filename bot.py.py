import os
import io
import imghdr
import logging
import asyncio
from typing import List, Optional, Dict

from PIL import Image

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types


# ----------------- Config -----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("img-edit-bot")

MODEL = "gemini-2.5-flash-image"
MAX_IMAGES = 3
MAX_SIDE = 1536  # বড় ছবি হলে resize করে পাঠাবো (speed + reliability)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY env var")

client = genai.Client(api_key=GEMINI_API_KEY)

# per-user generation lock (avoid overlapping runs)
USER_LOCKS: Dict[int, asyncio.Lock] = {}


def _get_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]


# ----------------- Helpers -----------------
def _guess_mime(b: bytes) -> str:
    k = imghdr.what(None, h=b)
    if k == "png":
        return "image/png"
    if k in ("jpeg", "jpg"):
        return "image/jpeg"
    if k == "webp":
        return "image/webp"
    return "image/jpeg"


def _downscale_image_bytes(image_bytes: bytes) -> bytes:
    """Telegram photo কখনও বড় হলে downscale করে model-এ পাঠাই"""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        m = max(w, h)
        if m <= MAX_SIDE:
            return image_bytes

        scale = MAX_SIDE / float(m)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception:
        return image_bytes


def _get_state(context: ContextTypes.DEFAULT_TYPE):
    """
    context.user_data:
      images: List[bytes]
      prompt: Optional[str]
    """
    context.user_data.setdefault("images", [])
    context.user_data.setdefault("prompt", None)
    return context.user_data["images"], context.user_data["prompt"]


async def _download_image_bytes(update: Update) -> Optional[bytes]:
    msg = update.effective_message
    if not msg:
        return None

    # PHOTO
    if msg.photo:
        best = msg.photo[-1]  # best resolution usually last
        tg_file = await best.get_file()
        return bytes(await tg_file.download_as_bytearray())

    # Image document (send as file)
    if msg.document and (msg.document.mime_type or "").startswith("image/"):
        tg_file = await msg.document.get_file()
        return bytes(await tg_file.download_as_bytearray())

    return None


async def _generate_edited_image(prompt: str, image_bytes_list: List[bytes]) -> bytes:
    parts = []
    for b in image_bytes_list:
        parts.append(types.Part.from_bytes(data=b, mime_type=_guess_mime(b)))

    if not image_bytes_list:
        raise RuntimeError("No images uploaded. Please send at least 1 image.")

    instruction = (
        "Edit the FIRST provided image as the base. "
        "If more images are provided, use them only as reference. "
        "Preserve the original style unless asked.\n"
        f"Instruction: {prompt}\n"
        "Return only the edited image."
    )

    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=parts + [instruction],
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )

    for part in getattr(resp, "parts", []) or []:
        if part.inline_data is not None:
            pil_img = part.as_image()
            out = io.BytesIO()
            pil_img.save(out, format="PNG")
            return out.getvalue()

    raise RuntimeError("No image returned (blocked/empty output). Try another prompt or image.")


# ----------------- Telegram Handlers -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Image Edit Bot\n\n"
        "কাজ:\n"
        f"1) 1-{MAX_IMAGES}টা ছবি পাঠান\n"
        "2) তারপর একটা prompt লিখুন (যেমন: make her touching her hair)\n\n"
        "Commands:\n"
        "/status - কত ছবি আপলোড হয়েছে দেখাবে\n"
        "/clear - সব reset করবে"
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["images"] = []
    context.user_data["prompt"] = None
    await update.message.reply_text("✅ Cleared. Images & prompt reset.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    images, prompt = _get_state(context)
    await update.message.reply_text(
        f"Images uploaded: {len(images)}/{MAX_IMAGES}\n"
        f"Prompt: {prompt if prompt else 'None'}"
    )


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    images, _ = _get_state(context)

    if len(images) >= MAX_IMAGES:
        await update.message.reply_text(
            f"⚠️ Max {MAX_IMAGES} images reached. Use /clear to start a new project."
        )
        return

    b = await _download_image_bytes(update)
    if not b:
        await update.message.reply_text("আমি শুধু image (photo/file) নিতে পারি।")
        return

    b = _downscale_image_bytes(b)
    images.append(b)

    idx = len(images)
    await update.message.reply_text(
        f"Image {idx} received ✅\nNow send a text prompt to describe the changes."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text or text.startswith("/"):
        return

    user_id = update.effective_user.id
    lock = _get_lock(user_id)

    images, _ = _get_state(context)
    if len(images) == 0:
        await msg.reply_text("আগে অন্তত 1টা ছবি পাঠান 🙂 তারপর prompt দিন।")
        return

    # prevent overlapping generations
    if lock.locked():
        await msg.reply_text("⏳ Already processing… একটু অপেক্ষা করুন বা /clear দিন।")
        return

    async with lock:
        context.user_data["prompt"] = text

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        progress_msg = await msg.reply_text("Your request is progressing...")

        try:
            out_png = await _generate_edited_image(text, images)

            bio = io.BytesIO(out_png)
            bio.name = "result.png"
            bio.seek(0)

            await msg.reply_photo(photo=bio, caption=f'✅ Generated: "{text}"')

            # reset project (video-style)
            context.user_data["images"] = []
            context.user_data["prompt"] = None
            await msg.reply_text("Process finished ✅\nSend new images to start again.")

        except Exception as e:
            log.exception("Generation failed")
            await msg.reply_text(f"❌ Failed: {e}\nTip: /clear করে আবার চেষ্টা করুন।")

        finally:
            try:
                await progress_msg.delete()
            except Exception:
                pass


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ছবি পাঠান (Photo বা image file)। Commands: /status /clear")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~(filters.PHOTO | filters.Document.IMAGE | filters.TEXT), on_other))

    app.run_polling()


if __name__ == "__main__":
    main()
