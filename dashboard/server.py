from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai

from data.call_manager import CallManager
from data.json_store import BookingNotFoundError
from data.settings_store import SalonSettings, SettingsStore
from data.store import BookingStore, Service
from gemini_session.session import GeminiLiveSession
from tools.context import CallContext

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _pump_browser_to_gemini(websocket: WebSocket, session: GeminiLiveSession, ctx: CallContext) -> None:
    """Reads mic PCM16@16kHz binary frames and JSON control frames from the
    browser and forwards audio straight to Gemini -- browser mic audio needs
    no resampling since the browser already downsamples to 16kHz."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data:
            session.send_audio_chunk_nowait(data)
        else:
            text = message.get("text")
            if text:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                if payload.get("type") == "hangup":
                    ctx.hangup_requested = True
        if ctx.hangup_requested:
            return


async def _pump_gemini_to_browser(
    websocket: WebSocket, session: GeminiLiveSession, call_manager: CallManager, call_id: str
) -> None:
    """Streams Gemini's PCM16@24kHz audio straight back to the browser --
    the Web Audio API resamples on playback, so no transcoding is needed --
    plus JSON control frames for barge-in and a transcript for the live feed."""
    async for event in session.events():
        if event.interrupted:
            await websocket.send_text(json.dumps({"type": "interrupted"}))
        if event.audio:
            await websocket.send_bytes(event.audio)
        if event.text:
            await call_manager.record_action(
                call_id,
                f"AI Receptionist: {event.text}",
                details={"speaker": "ai", "text": event.text},
            )
        if event.turn_complete:
            await websocket.send_text(json.dumps({"type": "turn_complete"}))


def create_app(
    store: BookingStore,
    call_manager: CallManager,
    settings_store: SettingsStore | None = None,
    gemini_client: genai.Client | None = None,
    gemini_model: str = "",
) -> FastAPI:
    if settings_store is None:
        settings_store = SettingsStore()

    app = FastAPI(title="Classy Nail Spa - Voice Agent Dashboard")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        s = await settings_store.get_settings()
        return s.to_dict()

    @app.post("/api/settings")
    async def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
        updated = await settings_store.update_settings(payload)
        if hasattr(store, "_business_hours_start"):
            store._business_hours_start = updated.business_hours_start
        if hasattr(store, "_business_hours_end"):
            store._business_hours_end = updated.business_hours_end
        await call_manager.broadcast("settings_updated", updated.to_dict())
        return {"status": "success", "settings": updated.to_dict()}

    @app.get("/api/stats")
    async def get_stats() -> dict[str, Any]:
        active_calls = await call_manager.get_active_calls()
        history = await call_manager.get_call_history(limit=500)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        calls_today = sum(
            1 for c in (active_calls + history)
            if c.get("started_at", "").startswith(today_str)
        )

        bookings = await store.list_bookings()
        confirmed = sum(1 for b in bookings if b.status == "confirmed")

        messages = await store.list_messages()
        urgent = sum(1 for m in messages if m.get("urgent", False))

        return {
            "active_calls_count": len(active_calls),
            "calls_today_count": calls_today,
            "total_bookings_count": len(bookings),
            "confirmed_bookings_count": confirmed,
            "total_messages_count": len(messages),
            "urgent_messages_count": urgent,
        }

    @app.get("/api/calls/active")
    async def get_active_calls() -> list[dict[str, Any]]:
        return await call_manager.get_active_calls()

    @app.get("/api/calls/history")
    async def get_call_history(limit: int = 50) -> list[dict[str, Any]]:
        return await call_manager.get_call_history(limit=limit)

    @app.get("/api/bookings")
    async def get_bookings() -> list[dict[str, Any]]:
        bookings = await store.list_bookings()
        # Sort by date and time
        return [b.__dict__ for b in sorted(bookings, key=lambda x: (x.date, x.time), reverse=True)]

    @app.post("/api/bookings/{booking_id}/cancel")
    async def cancel_booking(booking_id: str) -> dict[str, Any]:
        try:
            await store.cancel_booking(booking_id)
            await call_manager.broadcast("booking_cancelled", {"booking_id": booking_id})
            return {"status": "success", "booking_id": booking_id}
        except BookingNotFoundError:
            raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")

    @app.get("/api/messages")
    async def get_messages() -> list[dict[str, Any]]:
        messages = await store.list_messages()
        return list(reversed(messages))

    @app.get("/api/services")
    async def get_services() -> list[dict[str, Any]]:
        services = await store.list_services()
        return [s.__dict__ for s in services]

    @app.post("/api/services")
    async def create_service(service_data: dict[str, Any]) -> dict[str, Any]:
        name = str(service_data.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="Service name is required")
        try:
            price_usd = float(service_data.get("price_usd", 0.0))
            duration_minutes = int(service_data.get("duration_minutes", 30))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid price or duration")

        new_svc = Service(name=name, price_usd=price_usd, duration_minutes=duration_minutes)
        saved = await store.upsert_service(new_svc)
        await call_manager.broadcast("service_updated", saved.__dict__)
        return saved.__dict__

    @app.put("/api/services/{name}")
    async def update_service(name: str, service_data: dict[str, Any]) -> dict[str, Any]:
        target_name = str(service_data.get("name", name)).strip()
        if not target_name:
            raise HTTPException(status_code=400, detail="Service name is required")
        try:
            price_usd = float(service_data.get("price_usd", 0.0))
            duration_minutes = int(service_data.get("duration_minutes", 30))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid price or duration")

        if target_name.lower() != name.strip().lower():
            await store.delete_service(name.strip())

        new_svc = Service(name=target_name, price_usd=price_usd, duration_minutes=duration_minutes)
        saved = await store.upsert_service(new_svc)
        await call_manager.broadcast("service_updated", saved.__dict__)
        return saved.__dict__

    @app.delete("/api/services/{name}")
    async def delete_service(name: str) -> dict[str, Any]:
        deleted = await store.delete_service(name.strip())
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Service '{name}' not found")
        await call_manager.broadcast("service_deleted", {"name": name.strip()})
        return {"status": "deleted", "name": name.strip()}

    @app.websocket("/ws/simulate-call")
    async def websocket_simulate_call(websocket: WebSocket) -> None:
        """Bridges the browser's microphone/speakers to a real Gemini Live
        session -- the same GeminiLiveSession used for actual PBX calls --
        so "Test Inbound Call" talks to the live AI receptionist instead of
        a canned script."""
        await websocket.accept()

        if gemini_client is None:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Gemini client is not configured on the server.",
            }))
            await websocket.close()
            return

        call_id = f"sim-{uuid.uuid4().hex[:8]}"
        await call_manager.call_started(call_id, "Browser Simulation")
        await websocket.send_text(json.dumps({"type": "ready", "call_id": call_id}))

        ctx = CallContext(
            store=store,
            mcp=None,
            call_sid=call_id,
            other_leg_call_id=None,
            account_id=None,
            caller_number="Browser Simulation",
            call_manager=call_manager,
            settings_store=settings_store,
        )
        status = "completed"
        try:
            async with GeminiLiveSession(gemini_client, gemini_model, ctx) as session:
                recv_task = asyncio.create_task(
                    _pump_browser_to_gemini(websocket, session, ctx), name="browser-to-gemini"
                )
                send_task = asyncio.create_task(
                    _pump_gemini_to_browser(websocket, session, call_manager, call_id), name="gemini-to-browser"
                )

                await session.trigger_greeting()

                done, pending = await asyncio.wait(
                    {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done | pending:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

                if ctx.ws_transfer_target:
                    status = "transferred"
        except Exception:  # noqa: BLE001 - log and end the simulated call, don't crash the dashboard
            logger.exception("Simulated call %s ended with an error", call_id)
            status = "failed"
        finally:
            await call_manager.call_ended(call_id, status=status)

    @app.post("/api/calls/{call_id}/hangup")
    async def hangup_call(call_id: str) -> dict[str, Any]:
        record = await call_manager.call_ended(call_id, status="completed")
        return {"status": "hungup", "call_id": call_id}

    @app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket) -> None:
        await websocket.accept()
        q = call_manager.subscribe()
        try:
            # Send initial snapshot upon connection
            stats = await get_stats()
            active = await call_manager.get_active_calls()
            curr_settings = (await settings_store.get_settings()).to_dict()
            await websocket.send_text(json.dumps({
                "type": "init",
                "data": {
                    "stats": stats,
                    "active_calls": active,
                    "settings": curr_settings,
                }
            }))

            async def listen_events():
                try:
                    while True:
                        event = await q.get()
                        await websocket.send_text(json.dumps(event))
                except WebSocketDisconnect:
                    pass

            async def listen_incoming():
                try:
                    while True:
                        data = await websocket.receive_text()
                        if data == "ping":
                            await websocket.send_text(json.dumps({"type": "pong"}))
                except WebSocketDisconnect:
                    pass

            listen_task = asyncio.create_task(listen_events())
            recv_task = asyncio.create_task(listen_incoming())
            done, pending = await asyncio.wait(
                {listen_task, recv_task},
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            call_manager.unsubscribe(q)

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app
