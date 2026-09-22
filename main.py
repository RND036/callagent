import asyncio
import logging
import uvicorn
from google import genai

import config
from dashboard.server import create_app
from data.call_manager import CallManager
from data.json_store import JsonBookingStore
from data.settings_store import SettingsStore
from pbx_bridge.audiosocket_server import serve
from pbx_bridge.mcp_client import McpClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not config.GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is not set (see .env.example).")

    settings_store = SettingsStore(settings_path=config.SETTINGS_PATH)
    current_settings = await settings_store.get_settings()

    store = JsonBookingStore(
        services_path=config.SEED_SERVICES_PATH,
        bookings_path=config.BOOKINGS_PATH,
        messages_path=config.MESSAGES_PATH,
        business_hours_start=current_settings.business_hours_start,
        business_hours_end=current_settings.business_hours_end,
        slot_increment_minutes=config.SLOT_INCREMENT_MINUTES,
    )
    call_manager = CallManager(calls_path=config.CALLS_PATH)
    gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

    mcp_client = None
    if config.MCP_SERVER_URL:
        mcp_client = McpClient(config.MCP_SERVER_URL, config.MCP_API_KEY)
        logger.info("MCP transfer/hangup enabled: %s", config.MCP_SERVER_URL)
    else:
        logger.warning("MCP_SERVER_URL not set; transfer_call/end_call tool calls will log and no-op")

    pbx_server = await serve(
        config.WS_HOST,
        config.WS_PORT,
        gemini_client,
        config.GEMINI_MODEL,
        store,
        mcp_client,
        call_manager,
        settings_store,
    )
    logger.info("nailagent AudioSocket bridge listening on %s:%d", config.WS_HOST, config.WS_PORT)

    dash_task = None
    if config.DASHBOARD_ENABLED:
        dash_app = create_app(store, call_manager, settings_store, gemini_client, config.GEMINI_MODEL)
        dash_config = uvicorn.Config(
            dash_app,
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            log_level="warning",
            access_log=False,
        )
        dash_server = uvicorn.Server(dash_config)
        dash_task = asyncio.create_task(dash_server.serve())
        logger.info(
            "Booking & Calls Dashboard available at http://%s:%d",
            "localhost" if config.DASHBOARD_HOST == "0.0.0.0" else config.DASHBOARD_HOST,
            config.DASHBOARD_PORT,
        )

    try:
        async with pbx_server:
            if dash_task:
                await asyncio.gather(dash_task, asyncio.Future())
            else:
                await asyncio.Future()  # run forever
    finally:
        if dash_task and not dash_task.done():
            dash_task.cancel()
        if mcp_client is not None:
            await mcp_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

