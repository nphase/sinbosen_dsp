"""USB HID link for units that carry the command protocol over USB (LA448).

The LA448 enumerates as a vendor HID device with 64-byte input and output
reports and no report ID. It speaks the same frames as the UDP channel
(protocol.py): a request is zero-padded into a 64-byte output report, and a
reply longer than one report continues across consecutive input reports,
zero-padded after the tail marker. See docs/PROTOCOL.md, "USB HID transport
(LA448)".

`HidLink` has the four socket methods Transport uses (`sendto`, `recvfrom`,
`settimeout`, `close`), so everything above the socket - send pacing, reply
matching, quarantine, keepalive - is shared with UDP unchanged.
"""

from __future__ import annotations

from typing import Any

# 0483:5750 is STMicro's stock custom-HID ID, shared by unrelated STM32
# gadgets, so a unit is also matched on its product string (which the device
# pads with trailing spaces).
VENDOR_ID = 0x0483
PRODUCT_ID = 0x5750
PRODUCT_NAME = "AudioSystem"
REPORT_SIZE = 64

_SCHEME = "usb"


def parse_address(address: str) -> tuple[bool, str | None]:
    """Return `(is_usb, serial)` for a device address.

    Only `usb` and `usb:<serial>` are USB addresses (scheme case-insensitive);
    a hostname that merely starts with "usb" is still a network host.
    """
    scheme, sep, serial = address.strip().partition(":")
    if scheme.lower() != _SCHEME:
        return False, None
    return True, (serial.strip() or None) if sep else None


def _import_hid() -> Any:
    import hid

    return hid


def _hid() -> Any:
    try:
        return _import_hid()
    except ImportError as exc:
        raise RuntimeError("USB support needs the hidapi package: uv sync --extra usb") from exc


def _units(hid: Any) -> dict[str, Any]:
    """Attached units as `{serial: hidapi path}`."""
    return {
        str(info["serial_number"]): info["path"]
        for info in hid.enumerate(VENDOR_ID, PRODUCT_ID)
        if str(info.get("product_string") or "").strip() == PRODUCT_NAME
    }


def list_devices() -> list[str]:
    """Serial numbers of the units attached over USB."""
    return list(_units(_hid()))


class HidLink:
    """One open HID device, shaped like the UDP socket Transport expects."""

    def __init__(self, device: Any, address: str):
        self._device = device
        self.address = address
        self._timeout_ms = 200

    def settimeout(self, seconds: float) -> None:
        self._timeout_ms = int(seconds * 1000)

    def sendto(self, data: bytes, addr: object) -> int:
        """Send one frame as a zero-padded 64-byte report.

        A frame longer than one report is refused: replies continue across
        consecutive reports, but how a *request* must be split has never
        been observed, and a wrong guess could be parsed by the unit as some
        other command. Only the FIR coefficient write is that long
        (docs/PROTOCOL.md).
        """
        if len(data) > REPORT_SIZE:
            raise NotImplementedError(
                f"a {len(data)}-byte request needs more than one {REPORT_SIZE}-byte USB report, "
                "which is unverified on this hardware; no packet was sent"
            )
        # Leading 0x00: hidapi's report-ID byte for a device without report IDs.
        if self._device.write(b"\x00" + data.ljust(REPORT_SIZE, b"\x00")) < 0:
            raise OSError("USB HID write failed")
        return len(data)

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        report = self._device.read(REPORT_SIZE, self._timeout_ms)
        if not report:
            raise TimeoutError("no USB HID report received")
        return bytes(report), (self.address, 0)

    def close(self) -> None:
        self._device.close()


def open_link(address: str) -> HidLink:
    """Open the unit named by a `usb` / `usb:<serial>` address."""
    hid = _hid()
    _, serial = parse_address(address)
    found = _units(hid)
    attached = ", ".join(f"{_SCHEME}:{s}" for s in found) or "none"
    if not found:
        raise ConnectionError(
            f"no LA448 found on USB (looked for {VENDOR_ID:04x}:{PRODUCT_ID:04x}); is it plugged in and powered?"
        )
    if serial is None:
        if len(found) > 1:
            raise ConnectionError(f"more than one unit is attached over USB; pick one of: {attached}")
        serial = next(iter(found))
    elif serial not in found:
        raise ConnectionError(f"no unit {_SCHEME}:{serial} on USB; attached: {attached}")
    device = hid.device()
    try:
        device.open_path(found[serial])
    except OSError as exc:
        raise ConnectionError(
            f"could not open {_SCHEME}:{serial} ({exc}); another program may be using it "
            "(this UI or CLI, or the manufacturer's app), or this user lacks permission for the HID device"
        ) from exc
    return HidLink(device, f"{_SCHEME}:{serial}")
