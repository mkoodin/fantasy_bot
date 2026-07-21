"""Entry point for the Sleeper fantasy football assistant.

Default: boots the always-on Telegram bot (command handlers + scheduled
pre/post-waiver and gameday digests).

One-shot testing: run with MODE set to send a single digest and exit, e.g.
    MODE=pre   python waiver_bot.py
    MODE=post  python waiver_bot.py
    MODE=gameday python waiver_bot.py
Useful for a quick smoke test or if you'd rather drive it from Railway Cron.
"""

import asyncio
import logging
import os
import sys

from bot import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fantasy_bot")


def _preflight() -> None:
    missing = config.missing_required()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error("See .env.example for the full list. Exiting.")
        sys.exit(1)


async def _run_once(mode: str) -> None:
    """Build one digest and push it to Telegram, then exit."""
    from telegram import Bot
    from telegram.constants import ParseMode

    from bot import analysis, digest
    from bot.sleeper import SleeperClient

    client = SleeperClient()
    ctx = await analysis.build_context(client, force=True)

    if mode == "post":
        text = await digest.build_post_waiver_digest(ctx, client)
    elif mode == "gameday":
        text = await digest.build_gameday_alert(ctx, client)
    else:
        text = await digest.build_pre_waiver_digest(ctx, client)

    bot = Bot(config.TELEGRAM_TOKEN)
    async with bot:
        for chunk in digest.split_for_telegram(text):
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    logger.info("One-shot %s digest sent.", mode)


def main() -> None:
    _preflight()

    mode = os.getenv("MODE")
    if mode:
        logger.info("Running one-shot digest: MODE=%s", mode)
        asyncio.run(_run_once(mode.lower()))
        return

    from bot.telegram_bot import build_application

    logger.info("Starting always-on Telegram bot…")
    app = build_application()
    # run_polling manages its own event loop and blocks until interrupted.
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
