"""Booking backend interface. Swap JsonBookingStore for a real calendar/booking
platform later by writing a new class against this same Protocol — nothing in
tools/ or gemini_session/ needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Service:
    name: str
    price_usd: float
    duration_minutes: int


@dataclass(frozen=True)
class Booking:
    booking_id: str
    service_name: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM, 24h
    customer_name: str
    customer_phone: str
    status: str = "confirmed"  # confirmed | cancelled


class BookingStore(Protocol):
    async def list_services(self) -> list[Service]: ...

    async def get_service(self, name: str) -> Service | None: ...

    async def upsert_service(self, service: Service) -> Service: ...

    async def delete_service(self, name: str) -> bool: ...

    async def check_availability(self, service_name: str, date: str) -> list[str]:
        """Return open HH:MM time slots for the given service on the given date."""
        ...

    async def create_booking(
        self,
        service_name: str,
        date: str,
        time: str,
        customer_name: str,
        customer_phone: str,
    ) -> Booking: ...

    async def get_booking(self, booking_id: str) -> Booking | None: ...

    async def modify_booking(
        self, booking_id: str, date: str | None = None, time: str | None = None
    ) -> Booking: ...

    async def cancel_booking(self, booking_id: str) -> None: ...

    async def list_bookings(self) -> list[Booking]: ...

    async def take_message(
        self,
        caller_name: str | None,
        caller_phone: str | None,
        message: str,
        urgent: bool = False,
    ) -> None: ...

    async def list_messages(self) -> list[dict]: ...
