"""Local JSON-file implementation of BookingStore. Fine for a single-instance
deployment; swap for data/sqlite_store.py or a real booking platform later
behind the same interface if concurrency/write volume grows.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from data.store import Booking, Service


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
            Service(name=s["name"], price_usd=s["price_usd"], duration_minutes=s["duration_minutes"])
            for s in raw
        ]

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
                    {"name": s.name, "price_usd": s.price_usd, "duration_minutes": s.duration_minutes}
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
                        {"name": s.name, "price_usd": s.price_usd, "duration_minutes": s.duration_minutes}
                        for s in self._services
                    ],
                )
                return True
            return False

    async def check_availability(self, service_name: str, date: str) -> list[str]:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            taken = {
                b["time"]
                for b in bookings
                if b["date"] == date and b["status"] == "confirmed"
            }
            return [slot for slot in self._all_slots() if slot not in taken]

    async def create_booking(
        self,
        service_name: str,
        date: str,
        time: str,
        customer_name: str,
        customer_phone: str,
    ) -> Booking:
        service = await self.get_service(service_name)
        if service is None:
            raise ValueError(f"Unknown service: {service_name}")

        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            for b in bookings:
                if b["date"] == date and b["time"] == time and b["status"] == "confirmed":
                    raise SlotUnavailableError(f"{date} {time} is already booked")

            booking = Booking(
                booking_id=str(uuid.uuid4())[:8],
                service_name=service.name,
                date=date,
                time=time,
                customer_name=customer_name,
                customer_phone=customer_phone,
                status="confirmed",
            )
            bookings.append(booking.__dict__)
            self._write_json(self._bookings_path, bookings)
            return booking

    async def get_booking(self, booking_id: str) -> Booking | None:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            for b in bookings:
                if b["booking_id"] == booking_id:
                    return Booking(**b)
            return None

    async def modify_booking(
        self, booking_id: str, date: str | None = None, time: str | None = None
    ) -> Booking:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            for b in bookings:
                if b["booking_id"] == booking_id:
                    if b["status"] != "confirmed":
                        raise BookingNotFoundError(f"Booking {booking_id} is not active")
                    if date:
                        b["date"] = date
                    if time:
                        b["time"] = time
                    self._write_json(self._bookings_path, bookings)
                    return Booking(**b)
            raise BookingNotFoundError(f"No booking with id {booking_id}")

    async def cancel_booking(self, booking_id: str) -> None:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            for b in bookings:
                if b["booking_id"] == booking_id:
                    b["status"] = "cancelled"
                    self._write_json(self._bookings_path, bookings)
                    return
            raise BookingNotFoundError(f"No booking with id {booking_id}")

    async def take_message(
        self,
        caller_name: str | None,
        caller_phone: str | None,
        message: str,
        urgent: bool = False,
    ) -> None:
        async with self._lock:
            messages = self._read_json(self._messages_path)
            messages.append(
                {
                    "caller_name": caller_name,
                    "caller_phone": caller_phone,
                    "message": message,
                    "urgent": urgent,
                    "taken_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self._write_json(self._messages_path, messages)

    async def list_bookings(self) -> list[Booking]:
        async with self._lock:
            bookings = self._read_json(self._bookings_path)
            result = []
            for b in bookings:
                try:
                    result.append(Booking(**b))
                except Exception:
                    pass
            return result

    async def list_messages(self) -> list[dict]:
        async with self._lock:
            return self._read_json(self._messages_path)
