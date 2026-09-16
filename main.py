import asyncio
import logging

from google import genai

import config
from data.json_store import JsonBookingStore
from pbx_bridge.mcp_client import McpClient
from pbx_bridge.ws_server import serve

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not config.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is not set (see .env.example).")

    store = JsonBookingStore(
        services_path=config.SEED_SERVICES_PATH,
        bookings_path=config.BOOKINGS_PATH,
        messages_path=config.MESSAGES_PATH,
        business_hours_start=config.BUSINESS_HOURS_START,
        business_hours_end=config.BUSINESS_HOURS_END,
        slot_increment_minutes=config.SLOT_INCREMENT_MINUTES,
    )
    gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

    mcp_client = None
    if config.MCP_SERVER_URL:
        mcp_client = McpClient(config.MCP_SERVER_URL, config.MCP_API_KEY)
        logger.info("MCP transfer/hangup enabled: %s", config.MCP_SERVER_URL)
    else:
        logger.warning("MCP_SERVER_URL not set; transfer_call/end_call will use WS-level fallback only")

    server = await serve(config.WS_HOST, config.WS_PORT, gemini_client, config.GEMINI_MODEL, store, mcp_client)
    logger.info("nailagent listening on ws://%s:%d", config.WS_HOST, config.WS_PORT)
    try:
        async with server:
            await asyncio.Future()  # run forever
    finally:
        if mcp_client is not None:
            await mcp_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
