import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.server import create_app
from data.call_manager import CallManager
from data.json_store import JsonBookingStore
from pbx_bridge.caller_id import pick_caller_number

_UUID = "494fc852-bcec-11f1-9f1e-02fff725974b"


@pytest.mark.parametrize(
    "caller, pai, name, expected",
    [
        ("0781827743", "", "781827743", "0781827743"),  # as seen in the live CDRs
        ("+94781827743", "", "", "+94781827743"),
        ("ucmtrunk", '"781827743" <sip:0781827743@10.0.0.5>', "", "0781827743"),  # trunk name in From
        ("ucmtrunk", "<tel:+94781827743>", "", "+94781827743"),
        ("ucmtrunk", "", "0781827743", "0781827743"),
        ("anonymous", "", "Anonymous", None),
        ("", "", "", None),
        ("192.168.1.160", "", "", None),  # the UCM's own IP is not a phone number
        ("192.168.1.160", '"x" <sip:0781827743@192.168.1.160>', "", "0781827743"),
        ("3550", "", "", None),  # an internal extension, not a phone number
    ],
)
def test_pick_caller_number(caller, pai, name, expected) -> None:
    assert pick_caller_number(caller, pai, name) == expected


def test_pending_call_is_claimed_once_and_matches_any_uuid_case() -> None:
    manager = CallManager()
    manager.register_pending_call(_UUID.upper(), "0781827743", "ta")

    pending = manager.take_pending_call(_UUID)
    assert pending is not None
    assert (pending.caller_number, pending.language) == ("0781827743", "ta")
    assert manager.take_pending_call(_UUID) is None
    assert manager.take_pending_call(None) is None


def test_pending_call_expires(monkeypatch) -> None:
    import data.call_manager as call_manager_module

    manager = CallManager()
    manager.register_pending_call(_UUID, "0781827743", "si")
    monkeypatch.setattr(call_manager_module, "_PENDING_CALL_TTL_SECONDS", 0)

    assert manager.take_pending_call(_UUID) is None


@pytest.fixture
def app(tmp_path: Path):
    services_path = tmp_path / "services.json"
    services_path.write_text(json.dumps([]))
    store = JsonBookingStore(
        services_path=services_path,
        bookings_path=tmp_path / "bookings.json",
        messages_path=tmp_path / "messages.json",
        business_hours_start="09:00",
        business_hours_end="19:00",
        slot_increment_minutes=30,
    )
    manager = CallManager()
    return create_app(store, manager), manager


@pytest.mark.asyncio
async def test_call_start_endpoint_registers_caller_without_login(app) -> None:
    dash_app, manager = app
    transport = ASGITransport(app=dash_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/pbx/call-start", params={"uuid": _UUID, "caller": "ucmtrunk", "name": "0781827743"}
        )

    assert res.status_code == 200
    assert res.json()["caller_number"] == "0781827743"
    assert manager.take_pending_call(_UUID).caller_number == "0781827743"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lang, expected",
    [("ta", "ta"), ("en", "en"), ("si", "si"), ("3", "en"), ("", None), ("fr", "si")],
)
async def test_call_start_endpoint_registers_ivr_language(app, lang, expected) -> None:
    dash_app, manager = app
    transport = ASGITransport(app=dash_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/pbx/call-start", params={"uuid": _UUID, "caller": "0781827743", "lang": lang})

    assert res.status_code == 200
    # No lang= from the dialplan -> None: the agent's own language menu decides.
    assert res.json()["language"] == expected
    assert manager.take_pending_call(_UUID).language == expected


@pytest.mark.asyncio
async def test_call_start_endpoint_registers_language_even_when_number_is_withheld(app) -> None:
    dash_app, manager = app
    transport = ASGITransport(app=dash_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/pbx/call-start", params={"uuid": _UUID, "caller": "anonymous", "lang": "ta"})

    assert res.json()["caller_number"] is None
    pending = manager.take_pending_call(_UUID)
    assert (pending.caller_number, pending.language) == (None, "ta")


@pytest.mark.asyncio
async def test_call_start_endpoint_rejects_requests_through_the_public_proxy(app) -> None:
    dash_app, manager = app
    transport = ASGITransport(app=dash_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/pbx/call-start",
            params={"uuid": _UUID, "caller": "0700000000"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )

    assert res.status_code == 403
    assert manager.take_pending_call(_UUID) is None


@pytest.mark.asyncio
async def test_call_start_endpoint_rejects_non_local_clients(app) -> None:
    dash_app, manager = app
    transport = ASGITransport(app=dash_app, client=("203.0.113.9", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/pbx/call-start", params={"uuid": _UUID, "caller": "0700000000"})

    assert res.status_code == 403
    assert manager.take_pending_call(_UUID) is None


_QOS = "ssrc=1;themssrc=2;lp=4;rxjitter=0.003;rxcount=9750;txjitter=0.041;txcount=9800;rlp=312;rtt=0.18"


@pytest.mark.asyncio
async def test_call_end_endpoint_stores_line_quality_for_an_active_call(app) -> None:
    dash_app, manager = app
    await manager.call_started(_UUID, "0781827743")
    transport = ASGITransport(app=dash_app, client=("127.0.0.1", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/pbx/call-end", params={"uuid": _UUID.upper(), "qos": _QOS})
    assert res.status_code == 200
    record = await manager.get_call(_UUID)
    assert record["line_quality"]["rlp"] == 312 and record["line_quality"]["txcount"] == 9800


@pytest.mark.asyncio
async def test_line_quality_that_arrives_before_the_call_ends_is_kept(app) -> None:
    from data.call_manager import parse_qos

    _, manager = app
    await manager.call_started(_UUID, "0781827743")
    await manager.set_line_quality(_UUID, parse_qos(_QOS))
    await manager.call_ended(_UUID)
    assert (await manager.get_call(_UUID))["line_quality"]["rtt"] == 0.18

    other = "11111111-bcec-11f1-9f1e-02fff725974b"
    await manager.set_line_quality(other, parse_qos(_QOS))  # reported before the call record is closed
    await manager.call_started(other, "0770000000")
    await manager.call_ended(other)
    assert (await manager.get_call(other))["line_quality"]["lp"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"client": ("203.0.113.9", 40000)}, {"client": ("127.0.0.1", 40000), "proxied": True}])
async def test_call_end_endpoint_is_localhost_only(app, kwargs) -> None:
    dash_app, manager = app
    headers = {"x-forwarded-for": "203.0.113.9"} if kwargs.get("proxied") else {}
    transport = ASGITransport(app=dash_app, client=kwargs["client"])
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/pbx/call-end", params={"uuid": _UUID, "qos": _QOS}, headers=headers)
    assert res.status_code == 403


def test_parse_qos_and_description() -> None:
    from data.call_manager import describe_line_quality, lost_to_caller_percent, parse_qos

    qos = parse_qos(_QOS)
    assert lost_to_caller_percent(qos) == 3.2
    assert "far end lost 312 (3.2%)" in describe_line_quality(qos)
    assert parse_qos("") == {} and parse_qos("garbage;x=y") == {}
