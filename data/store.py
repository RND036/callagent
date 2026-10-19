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
    price_varies: bool = False  # the team confirms the price (length/design); the AI never quotes a number


@dataclass(frozen=True)
class Booking:
    booking_id: str
    service_name: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM, 24h
    customer_name: str
    customer_phone: str
    status: str = "confirmed"  # requested | confirmed | declined | cancelled
    branch: str = ""
    notes: str = ""  # design idea, gel/acrylic, other services in the same visit
    requested_date: str = ""  # a reschedule the caller asked for, waiting for staff
    requested_time: str = ""

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


ACTIVE_STATUSES = ("requested", "confirmed")


class BookingStore(Protocol):
    async def list_services(self) -> list[Service]: ...

    async def get_service(self, name: str) -> Service | None: ...

    async def upsert_service(self, service: Service) -> Service: ...

    async def delete_service(self, name: str) -> bool: ...

    async def check_availability(self, service_name: str, date: str, branch: str = "") -> list[str]:
        """Return open HH:MM time slots for the given service on the given date at the branch."""
        ...

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
        """A booking REQUEST (status "requested"): staff confirm it on the dashboard."""
        ...

    async def get_booking(self, booking_id: str) -> Booking | None: ...

    async def find_bookings(
        self, name: str | None = None, phone: str | None = None, date: str | None = None
    ) -> list[Booking]: ...

    async def request_change(self, booking_id: str, date: str | None = None, time: str | None = None) -> Booking:
        """Record a reschedule request; the booking keeps its slot until staff approve."""
        ...

    async def update_booking(self, booking_id: str, **fields: str) -> Booking:
        """Correct the details of a still-requested booking."""
        ...

    async def set_status(self, booking_id: str, status: str) -> Booking: ...

    async def approve_change(self, booking_id: str) -> Booking: ...

    async def reject_change(self, booking_id: str) -> Booking: ...

    async def cancel_booking(self, booking_id: str) -> None: ...

    async def list_bookings(self) -> list[Booking]: ...

    async def take_message(
        self,
        caller_name: str | None,
        caller_phone: str | None,
        message: str,
        urgent: bool = False,
        category: str = "general",
        branch: str = "",
    ) -> dict: ...

    async def list_messages(self) -> list[dict]: ...

    async def mark_message_done(self, message_id: str) -> bool: ...
