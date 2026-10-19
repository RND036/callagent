"""Local JSON-file implementation of BookingStore. Fine for a single-instance
deployment; swap for data/sqlite_store.py or a real booking platform later
behind the same interface if concurrency/write volume grows.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from data.store import ACTIVE_STATUSES, Booking, Service


class BookingNotFoundError(Exception):
    pass


class SlotUnavailableError(Exception):
    pass


class JsonBookingStore:
    def __init__(
        self,
        services_path: Path,
        bookings_path: Path,
        messages_path: Path,
        business_hours_start: str,
        business_hours_end: str,
        slot_increment_minutes: int,
    ) -> None:
        self._services_path = services_path
        self._bookings_path = bookings_path
        self._messages_path = messages_path
        self._business_hours_start = business_hours_start
        self._business_hours_end = business_hours_end
        self._slot_increment_minutes = slot_increment_minutes
        self._lock = asyncio.Lock()

        self._services: list[Service] = self._load_services()
        if not self._bookings_path.exists():
            self._write_json(self._bookings_path, [])
        if not self._messages_path.exists():
            self._write_json(self._messages_path, [])

    def _load_services(self) -> list[Service]:
        raw = json.loads(self._services_path.read_text())
        return [
            Service(name=s["name"], price_usd=s["price_usd"], duration_minutes=s["duration_minutes"],
                    price_varies=bool(s.get("price_varies", False)))
            for s in raw
        ]

    @staticmethod
    def _service_dict(s: Service) -> dict:
        return {"name": s.name, "price_usd": s.price_usd, "duration_minutes": s.duration_minutes,
                "price_varies": s.price_varies}

    @staticmethod
    def _read_json(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return json.loads(path.read_text())

    @staticmethod
    def _write_json(path: Path, data: list[dict]) -> None:
        path.write_text(json.dumps(data, indent=2))

    def _all_slots(self) -> list[str]:
        slots = []
        start = datetime.strptime(self._business_hours_start, "%H:%M")
        end = datetime.strptime(self._business_hours_end, "%H:%M")
        current = start
        while current < end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=self._slot_increment_minutes)
        return slots

    async def list_services(self) -> list[Service]:
        return list(self._services)

    async def get_service(self, name: str) -> Service | None:
        name_lower = name.strip().lower()
        for service in self._services:
            if service.name.lower() == name_lower:
                return service
        return None

    async def upsert_service(self, service: Service) -> Service:
        async with self._lock:
            updated = False
            new_services = []
            for s in self._services:
                if s.name.lower() == service.name.lower():
                    new_services.append(service)
                    updated = True
                else:
                    new_services.append(s)
            if not updated:
                new_services.append(service)
            self._services = new_services
            self._write_json(
                self._services_path,
                [
                    self._service_dict(s)
                    for s in self._services
                ],
            )
            return service

    async def delete_service(self, name: str) -> bool:
        async with self._lock:
            name_lower = name.strip().lower()
            initial_count = len(self._services)
            self._services = [s for s in self._services if s.name.lower() != name_lower]
            if len(self._services) < initial_count:
                self._write_json(
                    self._services_path,
                    [
                        self._service_dict(s)
                        for s in self._services
                    ],
                )
                return True
            return False

    @staticmethod
    def _same_branch(booking_branch: str, branch: str) -> bool:
        """Old bookings without a branch block every branch; a lookup without a branch matches all."""
        return not branch or not booking_branch or booking_branch.strip().lower() == branch.strip().lower()

    @classmethod
    def _taken_slots(cls, bookings: list[dict], date: str, branch: str) -> set[str]:
        """Slots held by requested/confirmed bookings (and by pending reschedules) at this branch on this date."""
        taken: set[str] = set()
        for b in bookings:
            if b.get("status") not in ACTIVE_STATUSES or not cls._same_branch(b.get("branch", ""), branch):
                continue
            if b["date"] == date:
                taken.add(b["time"])
            if b.get("requested_date") == date and b.get("requested_time"):
                taken.add(b["requested_time"])
        return taken

    async def check_availability(self, service_name: str, date: str, branch: str = "") -> list[str]:
        async with self._lock:
            taken = self._taken_slots(self._read_json(self._bookings_path), date, branch)
            return [slot for slot in self._all_slots() if slot not in taken]

    async def create_booking(
        self,
        service_name: str,
        date: str,
        time: str,
        customer_name: str,
        customer_phone: str,
        branch: str = "",
        notes: str = "",
    ) -> Booking:
        service = await self.get_service(service_name)
        if service is None:
            raise ValueError(f"Unknown service: {service_name}")

        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            if time in self._taken_slots(bookings, date, branch):
                raise SlotUnavailableError(f"{date} {time} is already booked")

            booking = Booking(
                booking_id=str(uuid.uuid4())[:8],
                service_name=service.name,
                date=date,
                time=time,
                customer_name=customer_name,
                customer_phone=customer_phone,
                status="requested",
                branch=branch,
                notes=notes,
            )
            bookings.append(booking.__dict__)
            self._write_json(self._bookings_path, bookings)
            return booking

    async def get_booking(self, booking_id: str) -> Booking | None:
        async with self._lock:
            for b in self._read_json(self._bookings_path):
                if b["booking_id"] == booking_id:
                    return Booking(**b)
            return None

    async def find_bookings(
        self, name: str | None = None, phone: str | None = None, date: str | None = None
    ) -> list[Booking]:
        """Active bookings matching the name (forgiving), phone (last 9 digits) and/or date."""
        wanted_name = " ".join((name or "").lower().split())
        wanted_phone = "".join(c for c in (phone or "") if c.isdigit())[-9:]
        found = []
        async with self._lock:
            for b in self._read_json(self._bookings_path):
                if b.get("status") not in ACTIVE_STATUSES:
                    continue
                if date and date not in (b["date"], b.get("requested_date")):
                    continue
                if wanted_phone and "".join(c for c in b["customer_phone"] if c.isdigit())[-9:] != wanted_phone:
                    continue
                if wanted_name:
                    have = " ".join(b["customer_name"].lower().split())
                    if wanted_name not in have and have not in wanted_name and \
                            difflib.SequenceMatcher(None, wanted_name, have).ratio() < 0.7:
                        continue
                found.append(Booking(**b))
        return found

    def _find(self, bookings: list[dict], booking_id: str) -> dict:
        for b in bookings:
            if b["booking_id"] == booking_id:
                return b
        raise BookingNotFoundError(f"No booking with id {booking_id}")

    async def request_change(self, booking_id: str, date: str | None = None, time: str | None = None) -> Booking:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            if b["status"] not in ACTIVE_STATUSES:
                raise BookingNotFoundError(f"Booking {booking_id} is not active")
            new_date, new_time = date or b["date"], time or b["time"]
            others = [x for x in bookings if x["booking_id"] != booking_id]
            if new_time in self._taken_slots(others, new_date, b.get("branch", "")):
                raise SlotUnavailableError(f"{new_date} {new_time} is already booked")
            b["requested_date"], b["requested_time"] = new_date, new_time
            self._write_json(self._bookings_path, bookings)
            return Booking(**b)

    async def update_booking(self, booking_id: str, **fields: str) -> Booking:
        """Correct the details of a still-requested booking (service, branch, date, time, name, phone, notes)."""
        allowed = {"service_name", "branch", "date", "time", "customer_name", "customer_phone", "notes"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update: {', '.join(sorted(unknown))}")
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            if b["status"] != "requested":
                raise BookingNotFoundError(f"Booking {booking_id} is not a pending request")
            merged = {**b, **{k: v for k, v in fields.items() if v is not None}}
            others = [x for x in bookings if x["booking_id"] != booking_id]
            if merged["time"] in self._taken_slots(others, merged["date"], merged.get("branch", "")):
                raise SlotUnavailableError(f"{merged['date']} {merged['time']} is already booked")
            b.update(merged)
            self._write_json(self._bookings_path, bookings)
            return Booking(**b)

    async def set_status(self, booking_id: str, status: str) -> Booking:
        if status not in ("requested", "confirmed", "declined", "cancelled"):
            raise ValueError(f"Unknown status: {status}")
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            b["status"] = status
            if status in ("declined", "cancelled"):
                b["requested_date"] = b["requested_time"] = ""
            self._write_json(self._bookings_path, bookings)
            return Booking(**b)

    async def approve_change(self, booking_id: str) -> Booking:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            if not b.get("requested_date"):
                raise BookingNotFoundError(f"Booking {booking_id} has no change request")
            b["date"], b["time"] = b["requested_date"], b["requested_time"] or b["time"]
            b["requested_date"] = b["requested_time"] = ""
            self._write_json(self._bookings_path, bookings)
            return Booking(**b)

    async def reject_change(self, booking_id: str) -> Booking:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            b["requested_date"] = b["requested_time"] = ""
            self._write_json(self._bookings_path, bookings)
            return Booking(**b)

    async def cancel_booking(self, booking_id: str) -> None:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            b = self._find(bookings, booking_id)
            b["status"] = "cancelled"
            b["requested_date"] = b["requested_time"] = ""
            self._write_json(self._bookings_path, bookings)

    async def take_message(
        self,
        caller_name: str | None,
        caller_phone: str | None,
        message: str,
        urgent: bool = False,
        category: str = "general",
        branch: str = "",
    ) -> dict:
        async with self._lock:
            messages = self._read_json(self._messages_path)
            entry = {
                "message_id": str(uuid.uuid4())[:8],
                "caller_name": caller_name,
                "caller_phone": caller_phone,
                "message": message,
                "urgent": urgent,
                "category": category,
                "branch": branch,
                "status": "open",
                "taken_at": datetime.now().isoformat(timespec="seconds"),
            }
            messages.append(entry)
            self._write_json(self._messages_path, messages)
            return entry

    async def mark_message_done(self, message_id: str) -> bool:
        async with self._lock:
            messages = self._read_json(self._messages_path)
            for m in messages:
                if m.get("message_id") == message_id:
                    m["status"] = "done"
                    self._write_json(self._messages_path, messages)
                    return True
            return False

    async def list_bookings(self) -> list[Booking]:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            result = []
            for b in bookings:
                try:
                    known = Booking.__dataclass_fields__
                    result.append(Booking(**{k: v for k, v in b.items() if k in known}))
                except Exception:
                    pass
            return result

    async def list_messages(self) -> list[dict]:
        async with self._lock:
            return self._read_json(self._messages_path)
