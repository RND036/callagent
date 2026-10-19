from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google import genai

import config
from data.call_manager import CallManager, TranscriptLog, parse_qos
from audio.prompt_audio import MAX_UPLOAD_BYTES, ffmpeg_available
from data.faq_store import FaqStore
from data.ivr_store import PROMPT_SLOTS, IvrStore
from data.json_store import BookingNotFoundError
from data.settings_store import AI_VOICES, SalonSettings, SettingsStore, validate_ai_settings
from data.store import BookingStore, Service
from gemini_session.persona import LANGUAGES, normalize_language
from gemini_session.live_session import run_agent
from pbx_bridge.call_session import call_status
from pbx_bridge.caller_id import pick_caller_number
from tools.context import CallContext

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_COOKIE_NAME = "nailagent_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60


class BrowserTransport:
    """The Test Inbound Call page as a phone line: mic PCM16@16kHz in, agent PCM16@24kHz out (the Web Audio API
    resamples on playback), JSON control frames for events ({"type": "hangup"} from the page ends the call)."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.closed = False
        self.dtmf: asyncio.Queue[str] = asyncio.Queue()

    async def receive(self) -> bytes | None:
        while not self.closed:
            try:
                message = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                self.closed = True
                return None
            if message["type"] == "websocket.disconnect":
                self.closed = True
                return None
            if message.get("bytes"):
                return message["bytes"]
            if message.get("text"):
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("type") == "hangup":
                    return None
        return None

    async def send(self, pcm24k: bytes) -> None:
        if not self.closed:
            try:
                await self.ws.send_bytes(pcm24k)
            except (WebSocketDisconnect, RuntimeError):
                self.closed = True

    async def clear(self) -> None:
        await self.event({"type": "interrupted"})

    async def event(self, data: dict) -> None:
        if not self.closed:
            try:
                await self.ws.send_text(json.dumps(data))
            except (WebSocketDisconnect, RuntimeError):
                self.closed = True

    async def close(self) -> None:
        pass  # the page plays what it has; the socket is closed by the endpoint


async def _then(coro) -> None:
    await coro


def create_app(
    store: BookingStore,
    call_manager: CallManager,
    settings_store: SettingsStore | None = None,
    gemini_client: genai.Client | None = None,
    gemini_model: str = "",
    faq_store: FaqStore | None = None,
    ivr_store: IvrStore | None = None,
) -> FastAPI:
    if settings_store is None:
        settings_store = SettingsStore()
    if faq_store is None:
        faq_store = FaqStore()
    if ivr_store is None:
        ivr_store = IvrStore()

    app = FastAPI(title="Classy Nail Spa - Voice Agent Dashboard")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    active_sessions: set[str] = set()

    def _is_authenticated(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        return bool(token) and token in active_sessions

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in ("/login", "/api/login") or path.startswith("/static/"):
            return await call_next(request)
        if path in ("/api/pbx/call-start", "/api/pbx/call-end"):
            # Called by Asterisk's dialplan CURL() on this same box, not a
            # browser. Caddy proxies public traffic in from 127.0.0.1 too, so
            # also reject anything carrying proxy headers.
            host = request.client.host if request.client else ""
            proxied = "x-forwarded-for" in request.headers or "forwarded" in request.headers
            if host not in ("127.0.0.1", "::1") or proxied:
                return JSONResponse({"detail": "Forbidden"}, status_code=403)
            return await call_next(request)
        if path.startswith("/api/"):
            if not _is_authenticated(request):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return await call_next(request)
        if path == "/" and not _is_authenticated(request):
            return RedirectResponse(url="/login")
        return await call_next(request)

    @app.get("/login")
    async def login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "login.html")

    @app.post("/api/login")
    async def login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if not secrets.compare_digest(username, config.DASHBOARD_USERNAME) or not secrets.compare_digest(
            password, config.DASHBOARD_PASSWORD
        ):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        token = secrets.token_urlsafe(32)
        active_sessions.add(token)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return {"status": "success"}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        active_sessions.discard(token)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return {"status": "success"}

    @app.get("/api/pbx/call-start")
    async def pbx_call_start(
        uuid: str, caller: str = "", pai: str = "", name: str = "", lang: str = ""
    ) -> dict[str, Any]:
        """Registers the caller's number and IVR language choice for an
        AudioSocket call about to connect -- see pbx_bridge/caller_id.py for
        the dialplan."""
        number = pick_caller_number(caller, pai, name)
        if not number:
            logger.info("No caller number for call %s (caller=%r, pai=%r, name=%r)", uuid, caller, pai, name)
        language = normalize_language(lang) if lang.strip() else None  # no lang= -> the agent plays the menu
        call_manager.register_pending_call(uuid, number, language)
        return {"status": "ok", "caller_number": number, "language": language}

    @app.get("/api/pbx/call-end")
    async def pbx_call_end(uuid: str, qos: str = "") -> dict[str, Any]:
        """Called by the dialplan's hangup handler with Asterisk's RTCP stats for the call (RTPAUDIOQOS), so a
        caller who could not hear the agent can be traced to packet loss on the line."""
        stats = parse_qos(qos)
        if stats:
            await call_manager.set_line_quality(uuid, stats)
        return {"status": "ok", "fields": len(stats)}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        s = await settings_store.get_settings()
        return {**s.to_dict(), "ai_voices": AI_VOICES}

    @app.post("/api/settings")
    async def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = validate_ai_settings(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updated = await settings_store.update_settings(payload)
        if hasattr(store, "_business_hours_start"):
            store._business_hours_start = updated.business_hours_start
        if hasattr(store, "_business_hours_end"):
            store._business_hours_end = updated.business_hours_end
        await call_manager.broadcast("settings_updated", updated.to_dict())
        return {"status": "success", "settings": updated.to_dict()}

    async def _ivr_state() -> dict[str, Any]:
        return {**await ivr_store.get(), "languages": LANGUAGES, "ffmpeg_available": ffmpeg_available(),
                "active": ivr_store.is_active()}

    @app.get("/api/ivr")
    async def get_ivr() -> dict[str, Any]:
        return await _ivr_state()

    @app.post("/api/ivr")
    async def update_ivr(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            await ivr_store.update(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        state = await _ivr_state()
        await call_manager.broadcast("ivr_updated", state)
        return {"status": "success", "ivr": state}

    @app.put("/api/ivr/prompts/{slot}")
    async def upload_ivr_prompt(slot: str, request: Request, filename: str = "") -> dict[str, Any]:
        # The recording is the raw request body, so no multipart dependency is needed.
        if slot not in PROMPT_SLOTS:
            raise HTTPException(status_code=404, detail="Unknown recording")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="The file is too large (15 MB maximum).")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="The file is too large (15 MB maximum).")
        try:
            await ivr_store.save_prompt(slot, bytes(body), filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state = await _ivr_state()
        await call_manager.broadcast("ivr_updated", state)
        return {"status": "success", "ivr": state}

    @app.get("/api/ivr/prompts/{slot}")
    async def get_ivr_prompt(slot: str) -> FileResponse:
        path = ivr_store.prompt_path(slot)
        if path is None or not path.exists():
            raise HTTPException(status_code=404, detail="No recording uploaded")
        return FileResponse(path, media_type="audio/wav", filename=f"{slot}.wav")

    @app.delete("/api/ivr/prompts/{slot}")
    async def delete_ivr_prompt(slot: str) -> dict[str, Any]:
        if slot not in PROMPT_SLOTS:
            raise HTTPException(status_code=404, detail="Unknown recording")
        await ivr_store.delete_prompt(slot)
        state = await _ivr_state()
        await call_manager.broadcast("ivr_updated", state)
        return {"status": "success", "ivr": state}

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
        requested = sum(1 for b in bookings if b.status == "requested" or b.requested_date)

        messages = await store.list_messages()
        urgent = sum(1 for m in messages if m.get("urgent", False) and m.get("status") != "done")

        return {
            "active_calls_count": len(active_calls),
            "calls_today_count": calls_today,
            "total_bookings_count": len(bookings),
            "confirmed_bookings_count": confirmed,
            "requested_bookings_count": requested,
            "total_messages_count": len(messages),
            "urgent_messages_count": urgent,
        }

    @app.get("/api/calls/active")
    async def get_active_calls() -> list[dict[str, Any]]:
        return await call_manager.get_active_calls()

    @app.get("/api/calls/history")
    async def get_call_history(limit: int = 50) -> list[dict[str, Any]]:
        return await call_manager.get_call_history(limit=limit)

    @app.get("/api/calls/{call_id}")
    async def get_call_detail(call_id: str) -> dict[str, Any]:
        call = await call_manager.get_call(call_id)
        if call is None:
            raise HTTPException(status_code=404, detail="Call not found")
        return call

    @app.get("/api/calls/{call_id}/recording")
    async def get_call_recording(call_id: str) -> FileResponse:
        # The file is found through the stored call record, never by building
        # a path from the URL.
        path = call_manager.recording_path(call_id)
        if path is None:
            raise HTTPException(status_code=404, detail="No recording for this call")
        return FileResponse(path, media_type="audio/wav", filename=f"call-{call_id}.wav")

    @app.get("/api/bookings")
    async def get_bookings() -> list[dict[str, Any]]:
        bookings = await store.list_bookings()
        # Newest booking first (the store keeps them in the order they were made).
        return [b.__dict__ for b in reversed(bookings)]

    async def _booking_action(booking_id: str, event: str, action) -> dict[str, Any]:
        try:
            booking = await action(booking_id)
        except BookingNotFoundError:
            raise HTTPException(status_code=404, detail=f"Booking {booking_id} not found")
        await call_manager.broadcast(event, {"booking_id": booking_id})
        return {"status": "success", "booking_id": booking_id, "booking": getattr(booking, "__dict__", None)}

    @app.post("/api/bookings/{booking_id}/cancel")
    async def cancel_booking(booking_id: str) -> dict[str, Any]:
        return await _booking_action(booking_id, "booking_cancelled", lambda i: _then(store.cancel_booking(i)))

    @app.post("/api/bookings/{booking_id}/confirm")
    async def confirm_booking(booking_id: str) -> dict[str, Any]:
        return await _booking_action(booking_id, "booking_modified", lambda i: store.set_status(i, "confirmed"))

    @app.post("/api/bookings/{booking_id}/decline")
    async def decline_booking(booking_id: str) -> dict[str, Any]:
        return await _booking_action(booking_id, "booking_modified", lambda i: store.set_status(i, "declined"))

    @app.post("/api/bookings/{booking_id}/change/approve")
    async def approve_change(booking_id: str) -> dict[str, Any]:
        return await _booking_action(booking_id, "booking_modified", store.approve_change)

    @app.post("/api/bookings/{booking_id}/change/reject")
    async def reject_change(booking_id: str) -> dict[str, Any]:
        return await _booking_action(booking_id, "booking_modified", store.reject_change)

    @app.get("/api/messages")
    async def get_messages() -> list[dict[str, Any]]:
        messages = await store.list_messages()
        return list(reversed(messages))

    @app.post("/api/messages/{message_id}/done")
    async def message_done(message_id: str) -> dict[str, Any]:
        if not await store.mark_message_done(message_id):
            raise HTTPException(status_code=404, detail="Message not found")
        await call_manager.broadcast("message_updated", {"message_id": message_id})
        return {"status": "done", "message_id": message_id}

    @app.get("/api/faqs")
    async def get_faqs() -> list[dict[str, Any]]:
        return await faq_store.list_faqs()

    @app.post("/api/faqs")
    async def create_faq(data: dict[str, Any]) -> dict[str, Any]:
        try:
            faq = await faq_store.upsert(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await call_manager.broadcast("faqs_updated", {})
        return faq

    @app.put("/api/faqs/{faq_id}")
    async def update_faq(faq_id: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            faq = await faq_store.upsert(data, faq_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="FAQ not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await call_manager.broadcast("faqs_updated", {})
        return faq

    @app.delete("/api/faqs/{faq_id}")
    async def delete_faq(faq_id: str) -> dict[str, Any]:
        if not await faq_store.delete(faq_id):
            raise HTTPException(status_code=404, detail="FAQ not found")
        await call_manager.broadcast("faqs_updated", {})
        return {"status": "deleted", "faq_id": faq_id}

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

        new_svc = Service(name=name, price_usd=price_usd, duration_minutes=duration_minutes,
                          price_varies=bool(service_data.get("price_varies", False)))
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

        new_svc = Service(name=target_name, price_usd=price_usd, duration_minutes=duration_minutes,
                          price_varies=bool(service_data.get("price_varies", False)))
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
        session -- the same run_agent agent used for actual PBX calls --
        so "Test Inbound Call" talks to the live AI receptionist instead of
        a canned script."""
        if websocket.cookies.get(SESSION_COOKIE_NAME) not in active_sessions:
            await websocket.close(code=4401)
            return
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
            faq_store=faq_store,
            # Stands in for the phone IVR menu choice.
            language=normalize_language(websocket.query_params.get("lang")),
        )
        status = "completed"
        result = None
        try:
            transport = BrowserTransport(websocket)
            transcript = TranscriptLog(call_manager, call_id)
            try:
                result = await run_agent(transport, ctx, transcript, gemini_client, gemini_model)
                logger.info("Simulated call %s ended: %s (Live reconnects: %d)", call_id, result.reason,
                            result.reconnects)
            finally:
                await transcript.flush()
            status = call_status(ctx, result)
        except Exception:  # noqa: BLE001 - log and end the simulated call, don't crash the dashboard
            logger.exception("Simulated call %s ended with an error", call_id)
            status = "failed"
        finally:
            await call_manager.call_ended(
                call_id, status=status, end_reason=result.reason if result else "error",
                error=result.error if result else None, reconnects=result.reconnects if result else 0,
                customer=ctx.customer,
            )

    @app.post("/api/calls/{call_id}/hangup")
    async def hangup_call(call_id: str) -> dict[str, Any]:
        record = await call_manager.call_ended(call_id, status="completed")
        return {"status": "hungup", "call_id": call_id}

    @app.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket) -> None:
        if websocket.cookies.get(SESSION_COOKIE_NAME) not in active_sessions:
            await websocket.close(code=4401)
            return
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
