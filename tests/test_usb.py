from typing import Any

import pytest

from sinbosen_la548 import protocol, usb
from sinbosen_la548.protocol import Opcode

SERIAL = "6C61B7010730"


class _FakeDevice:
    """Stands in for a hidapi device - no real USB I/O in these tests."""

    def __init__(self, reports: list[bytes] | None = None) -> None:
        self.written: list[bytes] = []
        self.reads: list[tuple[int, int]] = []
        self.reports = list(reports or [])
        self.opened_path: bytes | None = None
        self.closed = False
        self.write_result: int | None = None
        self.read_error: Exception | None = None

    def open_path(self, path: bytes) -> None:
        self.opened_path = path

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data) if self.write_result is None else self.write_result

    def read(self, max_length: int, timeout_ms: int = 0) -> list[int]:
        self.reads.append((max_length, timeout_ms))
        if self.read_error is not None:
            raise self.read_error
        return list(self.reports.pop(0)) if self.reports else []

    def close(self) -> None:
        self.closed = True


class _FakeHid:
    """Stands in for the `hid` module."""

    def __init__(self, serials: list[str], *, other_products: list[str] | None = None) -> None:
        self.serials = serials
        self.other_products = other_products or []
        self.open_error: Exception | None = None
        self.devices: list[_FakeDevice] = []
        self.enumerated: list[tuple[int, int]] = []

    def enumerate(self, vendor_id: int, product_id: int) -> list[dict[str, Any]]:
        self.enumerated.append((vendor_id, product_id))
        # The real unit pads its product string with trailing spaces.
        units = [
            {"path": f"path-{s}".encode(), "serial_number": s, "product_string": "AudioSystem   "}
            for s in self.serials
        ]
        others = [
            {"path": f"other-{i}".encode(), "serial_number": f"OTHER{i}", "product_string": product}
            for i, product in enumerate(self.other_products)
        ]
        return others + units

    def device(self) -> _FakeDevice:
        device = _FakeDevice()
        if self.open_error is not None:
            error = self.open_error

            def fail(path: bytes) -> None:
                raise error

            device.open_path = fail  # type: ignore[method-assign]
        self.devices.append(device)
        return device


def _fake_hid(
    monkeypatch: pytest.MonkeyPatch, serials: list[str], *, other_products: list[str] | None = None
) -> _FakeHid:
    fake = _FakeHid(serials, other_products=other_products)
    monkeypatch.setattr(usb, "_import_hid", lambda: fake)
    return fake


@pytest.mark.parametrize(
    "address,expected",
    [
        ("usb", (True, None)),
        ("USB", (True, None)),
        (" usb ", (True, None)),
        ("usb:", (True, None)),
        (f"usb:{SERIAL}", (True, SERIAL)),
        (f"USB:{SERIAL}", (True, SERIAL)),
        ("192.168.1.100", (False, None)),
        ("usbhost.local", (False, None)),
    ],
)
def test_parse_address(address, expected):
    assert usb.parse_address(address) == expected


def test_sendto_pads_a_short_frame_to_one_report():
    dev = _FakeDevice()
    link = usb.HidLink(dev, "usb")
    frame = protocol.encode_frame(Opcode.READ_LOCK_STATE)
    assert link.sendto(frame, None) == len(frame)
    # Leading 0x00 is hidapi's "no report ID" byte, not part of the report.
    assert dev.written == [b"\x00" + frame + bytes(64 - len(frame))]


def test_sendto_sends_a_frame_that_exactly_fills_one_report():
    dev = _FakeDevice()
    frame = protocol.encode_frame(Opcode.WRITE_MODULE_ID, bytes(49))
    assert len(frame) == 64
    usb.HidLink(dev, "usb").sendto(frame, None)
    assert dev.written == [b"\x00" + frame]


def test_sendto_refuses_a_frame_longer_than_one_report_without_writing():
    # How a request must be split across reports has never been observed, and
    # a wrong guess could be parsed by the unit as some other command.
    dev = _FakeDevice()
    frame = protocol.encode_frame(Opcode.WRITE_FIR_COEFS, bytes(range(256)) * 8)  # 2063 bytes
    with pytest.raises(NotImplementedError, match="no packet was sent"):
        usb.HidLink(dev, "usb").sendto(frame, None)
    assert dev.written == []


def test_recvfrom_returns_one_report_and_the_link_address():
    report = protocol.encode_frame(Opcode.READ_LOCK_STATE, b"\x000000").ljust(64, b"\x00")
    link = usb.HidLink(_FakeDevice([report]), f"usb:{SERIAL}")
    assert link.recvfrom(4096) == (report, (f"usb:{SERIAL}", 0))


def test_recvfrom_times_out_when_no_report_arrives():
    link = usb.HidLink(_FakeDevice(), "usb")
    with pytest.raises(TimeoutError):
        link.recvfrom(4096)


def test_recvfrom_passes_the_timeout_in_milliseconds():
    dev = _FakeDevice()
    link = usb.HidLink(dev, "usb")
    link.settimeout(0.2)
    with pytest.raises(TimeoutError):
        link.recvfrom(4096)
    assert dev.reads == [(64, 200)]


def test_read_errors_surface_as_oserror():
    dev = _FakeDevice()
    dev.read_error = OSError("read error")
    link = usb.HidLink(dev, "usb")
    with pytest.raises(OSError, match="read error"):
        link.recvfrom(4096)


def test_a_failed_write_raises_oserror():
    dev = _FakeDevice()
    dev.write_result = -1
    link = usb.HidLink(dev, "usb")
    with pytest.raises(OSError):
        link.sendto(protocol.encode_frame(Opcode.READ_LOCK_STATE), None)


def test_close_closes_the_device():
    dev = _FakeDevice()
    usb.HidLink(dev, "usb").close()
    assert dev.closed


def test_multi_report_reply_reassembles_through_unpack_frames():
    # A READ_GROUP reply is 2464 payload bytes: 2479 on the wire, which the
    # LA448 sends as 39 reports with the last one zero-padded.
    payload = bytes(i % 251 for i in range(2464))
    wire = protocol.encode_frame(Opcode.READ_GROUP, payload, version=0x0F)
    padded = wire.ljust(39 * 64, b"\x00")
    reports = [padded[i : i + 64] for i in range(0, len(padded), 64)]
    link = usb.HidLink(_FakeDevice(reports), "usb")
    buf = b""
    frames: list[protocol.Frame] = []
    for _ in reports:
        found, buf = protocol.unpack_frames(buf + link.recvfrom(4096)[0])
        frames += found
    assert [f.payload for f in frames] == [payload]
    assert buf == b""


def test_list_devices_returns_attached_serials(monkeypatch):
    fake = _fake_hid(monkeypatch, [SERIAL, "AAAA"])
    assert usb.list_devices() == [SERIAL, "AAAA"]
    assert fake.enumerated == [(0x0483, 0x5750)]


def test_open_link_without_a_device_raises_connection_error(monkeypatch):
    _fake_hid(monkeypatch, [])
    with pytest.raises(ConnectionError, match="no LA448 found on USB"):
        usb.open_link("usb")


def test_open_link_with_two_devices_and_no_serial_lists_both(monkeypatch):
    _fake_hid(monkeypatch, [SERIAL, "AAAA"])
    with pytest.raises(ConnectionError, match=rf"usb:{SERIAL}.*usb:AAAA"):
        usb.open_link("usb")


def test_open_link_selects_by_serial(monkeypatch):
    fake = _fake_hid(monkeypatch, ["AAAA", SERIAL])
    link = usb.open_link(f"usb:{SERIAL}")
    assert fake.devices[0].opened_path == f"path-{SERIAL}".encode()
    assert link.address == f"usb:{SERIAL}"


def test_open_link_names_the_single_device_it_opened(monkeypatch):
    _fake_hid(monkeypatch, [SERIAL])
    assert usb.open_link("usb").address == f"usb:{SERIAL}"


def test_open_link_with_an_unknown_serial_lists_what_is_attached(monkeypatch):
    _fake_hid(monkeypatch, [SERIAL])
    with pytest.raises(ConnectionError, match=rf"usb:NOPE.*usb:{SERIAL}"):
        usb.open_link("usb:NOPE")


def test_open_link_without_hidapi_names_the_extra(monkeypatch):
    def missing() -> Any:
        raise ImportError("No module named 'hid'")

    monkeypatch.setattr(usb, "_import_hid", missing)
    with pytest.raises(RuntimeError, match="uv sync --extra usb"):
        usb.open_link("usb")


def test_another_gadget_sharing_the_usb_ids_is_not_treated_as_a_unit(monkeypatch):
    # 0483:5750 is STMicro's stock custom-HID ID, used by unrelated devices.
    _fake_hid(monkeypatch, [SERIAL], other_products=["STM32 Custom HID"])
    assert usb.list_devices() == [SERIAL]
    assert usb.open_link("usb").address == f"usb:{SERIAL}"


def test_only_another_gadget_attached_means_no_unit_found(monkeypatch):
    fake = _fake_hid(monkeypatch, [], other_products=["STM32 Custom HID"])
    with pytest.raises(ConnectionError, match="no LA448 found on USB"):
        usb.open_link("usb")
    assert fake.devices == []


def test_open_failure_is_a_connection_error_with_a_hint(monkeypatch):
    fake = _fake_hid(monkeypatch, [SERIAL])
    fake.open_error = OSError("open failed")
    with pytest.raises(ConnectionError, match="another program"):
        usb.open_link("usb")
