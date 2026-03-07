#!/usr/bin/env python3
"""
ueboom.py — Control a UE BOOM / MEGABOOM speaker on macOS.

Two different protocols are used (same as the official UE app):

  off  — Classic Bluetooth RFCOMM/SPP (speaker must already be on & connected)
           Sends \x02\x01\xb6 to LWACP service on channel 1.
           Requires: pyobjc-framework-IOBluetooth + --speaker-mac

  on   — BLE GATT write to characteristic c6d6dc0d-07f5-47ef-9b59-630622b01fd3
           Payload: <host-mac-without-colons> + 01
           Works only when speaker is in BLE standby (after being turned off).
           Requires: host identity (--host-mac or UEBOOM_HOST_MAC) unless
                     explicit payload is provided (--payload-hex/--trusted-mac).

Examples:
  python ueboom.py scan
  python ueboom.py probe --device <corebluetooth-uuid>
    python ueboom.py on --host-mac AA:BB:CC:DD:EE:FF
    python ueboom.py off --speaker-mac AA:BB:CC:DD:EE:FF

Environment variables (optional defaults):
    UEBOOM_HOST_MAC
    UEBOOM_SPEAKER_MAC
    UEBOOM_DEVICE_ID
    UEBOOM_PAYLOAD_HEX
    UEBOOM_TRUSTED_MAC
    UEBOOM_FALLBACK_DEVICE_IDS   (comma-separated CoreBluetooth UUIDs)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

UE_POWER_CHAR_UUID = "c6d6dc0d-07f5-47ef-9b59-630622b01fd3"
UE_RFCOMM_CHANNEL = 1
UE_RFCOMM_OFF_MSG = b'\x02\x01\xb6'
DEFAULT_SCAN_SECONDS = 8.0
UE_NAME_HINTS = ("UE", "BOOM", "MEGABOOM")
# Advertisement fingerprint for UE/Logitech speakers in BLE standby:
#   - Service UUID FE9F (Google Fast Pair, used by Logitech/UE devices)
#   - Manufacturer ID 0x00E0 = 224 decimal (Logitech)
UE_BLE_SERVICE_UUID = "0000fe9f-0000-1000-8000-00805f9b34fb"
UE_BLE_MFR_ID = 224
UE_STATE_DIR = Path.home() / "Library" / "Application Support" / "ueboom"
UE_LAST_BLE_ID_CACHE = UE_STATE_DIR / "last_ble_ids.json"

ENV_HOST_MAC = "UEBOOM_HOST_MAC"
ENV_SPEAKER_MAC = "UEBOOM_SPEAKER_MAC"
ENV_DEVICE_ID = "UEBOOM_DEVICE_ID"
ENV_PAYLOAD_HEX = "UEBOOM_PAYLOAD_HEX"
ENV_TRUSTED_MAC = "UEBOOM_TRUSTED_MAC"
ENV_FALLBACK_DEVICE_IDS = "UEBOOM_FALLBACK_DEVICE_IDS"


def rfcomm_power_off(speaker_mac: str) -> int:
    """Turn off the speaker via classic BT RFCOMM using IOBluetooth (macOS only)."""
    try:
        import IOBluetooth  # pyobjc-framework-IOBluetooth
    except ImportError:
        print(
            "ERROR: pyobjc-framework-IOBluetooth is not installed.\n"
            "  → Run: pip install pyobjc-framework-IOBluetooth",
            file=sys.stderr,
        )
        return 1

    class _Delegate(IOBluetooth.NSObject):
        def rfcommChannelOpenComplete_status_(self, ch, status):
            pass
        def rfcommChannelClosed_(self, ch):
            pass

    dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(speaker_mac)
    if not dev:
        print(f"ERROR: IOBluetooth could not find device {speaker_mac}", file=sys.stderr)
        return 1

    # Establish the classic BT ACL connection if not already connected.
    if not dev.isConnected():
        conn = dev.openConnection()
        if conn != 0:
            print(
                f"ERROR: Cannot connect to speaker (error {conn:#010x}).\n"
                "  → Make sure the speaker is powered on and in range,\n"
                "    then try again.",
                file=sys.stderr,
            )
            return 1
        time.sleep(1.5)  # let the ACL link settle

    delegate = _Delegate.alloc().init()
    result, channel = dev.openRFCOMMChannelSync_withChannelID_delegate_(
        None, UE_RFCOMM_CHANNEL, delegate
    )
    if result != 0 or not channel:
        print(
            f"ERROR: Could not open RFCOMM channel (error {result:#010x}).\n"
            "  → Make sure the speaker is on, in range, and connected to your\n"
            "    Mac via Bluetooth (check System Settings → Bluetooth).",
            file=sys.stderr,
        )
        return 1

    wr = channel.writeSync_length_(UE_RFCOMM_OFF_MSG, len(UE_RFCOMM_OFF_MSG))
    time.sleep(0.3)
    channel.closeChannel()

    if wr != 0:
        print(f"ERROR: RFCOMM write failed (error {wr})", file=sys.stderr)
        return 1

    print("Power OFF sent via RFCOMM.")
    return 0


def normalize_host_mac(value: str) -> str:
    normalized = re.sub(r"[-:]", "", value).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", normalized):
        raise ValueError(
            "Invalid host MAC. Provide 12 hex chars, e.g. AA:BB:CC:DD:EE:FF"
        )
    return normalized


def build_trusted_payload_hex(value: str) -> str:
    """Return UE wake payload hex built from a trusted controller BT MAC."""
    normalized = normalize_host_mac(value)
    return normalized + "01"


def env_default(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def fallback_connectable_ids_from_env() -> tuple[str, ...]:
    raw = env_default(ENV_FALLBACK_DEVICE_IDS)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def looks_like_ue_name(name: Optional[str]) -> bool:
    if not name:
        return False
    upper = name.upper()
    return any(token in upper for token in UE_NAME_HINTS)


def looks_like_ue_advert(adv: object) -> bool:
    """Return True if advertisement data matches a UE/Logitech BLE standby fingerprint."""
    svc_uuids = [str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])]
    if UE_BLE_SERVICE_UUID in svc_uuids:
        return True
    mfr = getattr(adv, "manufacturer_data", None) or {}
    return UE_BLE_MFR_ID in mfr


def device_sort_key(item: tuple[object, object]) -> tuple[int, int]:
    device, adv = item
    name = getattr(device, "name", None) or getattr(adv, "local_name", None) or ""
    rssi = getattr(adv, "rssi", None)
    if rssi is None:
        rssi = -999
    preferred = 0 if (looks_like_ue_name(name) or looks_like_ue_advert(adv)) else 1
    return (preferred, -rssi)


async def scan_devices(scan_seconds: float) -> list[tuple[object, object]]:
    devices_and_adv = await BleakScanner.discover(timeout=scan_seconds, return_adv=True)
    items = list(devices_and_adv.values())
    items.sort(key=device_sort_key)
    return items


def print_scan_results(items: Iterable[tuple[object, object]]) -> None:
    any_rows = False
    for device, adv in items:
        any_rows = True
        name = getattr(device, "name", None) or getattr(adv, "local_name", None) or "(no name)"
        address = getattr(device, "address", None) or "(no identifier)"
        rssi = getattr(adv, "rssi", None)
        marker = "*" if (looks_like_ue_name(name) or looks_like_ue_advert(adv)) else " "
        rssi_text = str(rssi) if rssi is not None else "?"
        svc_uuids = getattr(adv, "service_uuids", None) or []
        mfr_data = getattr(adv, "manufacturer_data", None) or {}
        print(f"{marker} {name}\n    id: {address}\n    rssi: {rssi_text}", end="")
        if svc_uuids:
            print(f"\n    services: {', '.join(str(u) for u in svc_uuids)}", end="")
        if mfr_data:
            mfr_str = " ".join(f"{k}:{v.hex()}" for k, v in mfr_data.items())
            print(f"\n    mfr: {mfr_str}", end="")
        print()
    if not any_rows:
        print("No BLE devices found.")


async def probe_device(device_id: str) -> int:
    try:
        async with BleakClient(device_id, timeout=12.0) as client:
            print(f"Connected: {device_id}")
            for service in client.services:
                print(f"Service {service.uuid}")
                for char in service.characteristics:
                    props = ",".join(char.properties)
                    print(f"  Char {char.uuid} [{props}]")
        return 0
    except (BleakError, TimeoutError) as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        print(
            "If this is macOS, also check Bluetooth permissions for Terminal/iTerm/Python.",
            file=sys.stderr,
        )
        return 1


async def find_ue_ble_device(scan_seconds: float = 8.0) -> Optional[str]:
    """Auto-discover the UE speaker in BLE standby.

    Identifies the speaker by its advertisement fingerprint (FE9F service UUID
    or Logitech manufacturer ID 224). No GATT connection needed.
    Returns the CoreBluetooth UUID of the closest matching device, or None.
    """
    print(f"Scanning {scan_seconds:.0f}s for UE speaker...", flush=True)
    items = await scan_devices(scan_seconds)

    for device, adv in items:
        name = getattr(device, "name", None) or getattr(adv, "local_name", None) or ""
        device_id = getattr(device, "address", None) or str(device)
        rssi = getattr(adv, "rssi", None) or -999
        if looks_like_ue_name(name) or looks_like_ue_advert(adv):
            print(f"  Found UE device: {device_id} (rssi: {rssi})")
            return device_id

    return None


async def find_ue_ble_candidates(scan_seconds: float = 8.0, limit: int = 6) -> list[str]:
    """Return likely UE BLE CoreBluetooth UUIDs ordered by signal strength."""
    print(f"Scanning {scan_seconds:.0f}s for UE candidates...", flush=True)
    items = await scan_devices(scan_seconds)
    results: list[str] = []
    for device, adv in items:
        name = getattr(device, "name", None) or getattr(adv, "local_name", None) or ""
        if not (looks_like_ue_name(name) or looks_like_ue_advert(adv)):
            continue
        device_id = getattr(device, "address", None) or str(device)
        rssi = getattr(adv, "rssi", None)
        rssi_text = str(rssi) if rssi is not None else "?"
        print(f"  Candidate: {device_id} (rssi: {rssi_text})")
        results.append(device_id)
        if len(results) >= limit:
            break
    return results


def build_payload_candidates(host_mac: Optional[str], payload_hex: Optional[str] = None) -> list[tuple[str, bytes]]:
    """Build power-on payload variants used by different UE firmware revisions."""
    if payload_hex:
        cleaned = re.sub(r"[^0-9A-Fa-f]", "", payload_hex)
        if len(cleaned) % 2 != 0:
            raise ValueError("--payload-hex must contain an even number of hex digits")
        payload = bytes.fromhex(cleaned)
        return [("explicit", payload)]

    if not host_mac:
        raise ValueError(
            "No host MAC available. Provide --host-mac or set UEBOOM_HOST_MAC, "
            "or provide --payload-hex/--trusted-mac."
        )

    normal = bytes.fromhex(host_mac + "01")
    octets = [host_mac[i : i + 2] for i in range(0, 12, 2)]
    reversed_mac = "".join(reversed(octets))
    reversed_payload = bytes.fromhex(reversed_mac + "01")

    variants: list[tuple[str, bytes]] = [("normal", normal)]
    if reversed_payload != normal:
        variants.append(("reversed", reversed_payload))
    return variants


def _cache_key_for_speaker(speaker_mac: Optional[str]) -> str:
    if not speaker_mac:
        return "_default"
    try:
        return normalize_host_mac(speaker_mac)
    except ValueError:
        return "_default"


def load_last_ble_id(speaker_mac: Optional[str]) -> Optional[str]:
    """Load last known good CoreBluetooth UUID for this speaker from disk."""
    try:
        raw = UE_LAST_BLE_ID_CACHE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        value = data.get(_cache_key_for_speaker(speaker_mac))
        if isinstance(value, str) and value.strip():
            return value.strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return None


def save_last_ble_id(speaker_mac: Optional[str], device_id: str) -> None:
    """Persist last known good CoreBluetooth UUID for this speaker."""
    try:
        UE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if UE_LAST_BLE_ID_CACHE.exists():
            raw = UE_LAST_BLE_ID_CACHE.read_text(encoding="utf-8")
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                data = {str(k): str(v) for k, v in decoded.items()}
        data[_cache_key_for_speaker(speaker_mac)] = device_id
        UE_LAST_BLE_ID_CACHE.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Cache failures should never block power control.
        pass


async def _ble_try_power_on(device_id: str, payload: bytes) -> tuple[bool, str]:
    """Single BLE wake attempt against one candidate device/payload pair."""
    battery_char_uuid = "00002a19-0000-1000-8000-00805f9b34fb"
    try:
        async with BleakClient(device_id, timeout=12.0) as client:
            print(f"Connected: {device_id}")

            # Some UE firmware revisions need a short settle period after link-up
            # before the power characteristic becomes writable.
            await asyncio.sleep(1.5)

            char_uuids = {
                char.uuid.lower()
                for service in client.services
                for char in service.characteristics
            }
            if UE_POWER_CHAR_UUID.lower() not in char_uuids:
                return False, "power characteristic missing"

            if battery_char_uuid in char_uuids:
                try:
                    val = await client.read_gatt_char(battery_char_uuid)
                    print(f"Battery: {val[0]}%")
                except BleakError:
                    pass

            char_obj = client.services.get_characteristic(UE_POWER_CHAR_UUID)
            props = char_obj.properties if char_obj is not None else []
            use_no_response = "write-without-response" in props
            await client.write_gatt_char(
                UE_POWER_CHAR_UUID,
                payload,
                response=not use_no_response,
            )
            return True, "ok"
    except (BleakError, TimeoutError) as exc:
        detail = str(exc).strip()
        if not detail:
            detail = exc.__class__.__name__
        return False, detail


def _unpair_for_ble(speaker_mac: str) -> None:
    """Unpair the speaker so CoreBluetooth treats the BLE connection as unbonded."""
    bt = shutil.which("blueutil")
    if not bt:
        return
    subprocess.run([bt, "--unpair", speaker_mac],
                   capture_output=True, timeout=10)
    print("Unpaired from macOS (will re-pair when speaker turns on).")


def _auto_connect_speaker(
    speaker_mac: Optional[str],
    retries: int = 4,
    delay_s: float = 1.0,
    stable_seconds: int = 8,
    initial_delay_s: float = 2.5,
) -> bool:
    """Ask macOS to connect to the speaker after wake.

    Uses blueutil when available and requires the link to remain connected for
    `stable_seconds` before considering the attempt successful.
    Waits `initial_delay_s` after wake before first connect attempt so classic
    Bluetooth audio/profile services have time to come up.
    """
    if not speaker_mac:
        return False
    bt = shutil.which("blueutil")
    if not bt:
        return False

    def is_connected() -> bool:
        try:
            state = subprocess.run(
                [bt, "--is-connected", speaker_mac],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return (state.stdout or "").strip() == "1"
        except Exception:
            return False

    stable_checks = max(2, stable_seconds)

    if initial_delay_s > 0:
        print(f"Waiting {initial_delay_s:.1f}s before macOS connect attempt...")
        time.sleep(initial_delay_s)

    for attempt in range(1, max(1, retries) + 1):
        try:
            subprocess.run([bt, "--connect", speaker_mac], capture_output=True, timeout=8)
            if is_connected():
                # Verify the link doesn't immediately drop after connect.
                dropped = False
                for _ in range(stable_checks):
                    time.sleep(1.0)
                    if not is_connected():
                        dropped = True
                        print("Bluetooth link dropped during stabilization window; retrying connect...")
                        break
                if not dropped:
                    print(f"macOS connected to speaker ({speaker_mac}) and stayed stable.")
                    return True
        except Exception:
            pass

        if attempt < retries:
            time.sleep(max(0.0, delay_s))

    print(f"macOS auto-connect could not keep a stable connection for {speaker_mac}.")
    return False


async def ble_power_on(
    device_id: Optional[str],
    host_mac: Optional[str],
    speaker_mac: Optional[str] = None,
    payload_hex: Optional[str] = None,
    keep_paired: bool = True,
) -> int:
    """Wake the speaker from BLE standby via a GATT write."""
    # Unpair first so CoreBluetooth doesn't try to enforce classic-BT bonding
    # over the BLE link, which causes connection timeouts.
    if speaker_mac and not keep_paired:
        _unpair_for_ble(speaker_mac)
        await asyncio.sleep(1.5)

    try:
        payload_variants = build_payload_candidates(host_mac, payload_hex)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    candidates: list[str] = []
    if device_id:
        candidates.append(device_id)
    cached_device_id = load_last_ble_id(speaker_mac)
    if cached_device_id:
        print(f"Trying cached BLE device first: {cached_device_id}")
        candidates.append(cached_device_id)
    candidates.extend(fallback_connectable_ids_from_env())

    deduped_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cid = candidate.upper()
        if cid in seen:
            continue
        seen.add(cid)
        deduped_candidates.append(candidate)

    last_error = "no attempts made"

    async def _attempt(cands: list[str]) -> tuple[bool, str, Optional[str]]:
        local_last_error = "no attempts made"
        for round_idx in range(2):
            if round_idx > 0:
                print("Retrying wake attempts after short delay...")
                await asyncio.sleep(1.2)

            for candidate in cands:
                for label, payload in payload_variants:
                    print(
                        f"Attempt wake: device={candidate} payload={label} "
                        f"({payload.hex().upper()})"
                    )
                    ok, detail = await _ble_try_power_on(candidate, payload)
                    if ok:
                        print(
                            "Power ON sent (BLE). "
                            f"Device: {candidate} Payload: {payload.hex().upper()}"
                        )
                        return True, "ok", candidate
                    local_last_error = detail
                    print(f"  Failed: {detail}")
        return False, local_last_error, None

    if deduped_candidates:
        ok, last_error, winner = await _attempt(deduped_candidates)
        if ok and winner:
            save_last_ble_id(speaker_mac, winner)
            _auto_connect_speaker(speaker_mac)
            return 0

    # Scan only after direct/cached/fallback IDs fail.
    scanned = await find_ue_ble_candidates(scan_seconds=8.0)
    if scanned:
        combined = deduped_candidates + scanned
        scan_candidates: list[str] = []
        seen_scan: set[str] = set()
        for candidate in combined:
            cid = candidate.upper()
            if cid in seen_scan:
                continue
            seen_scan.add(cid)
            scan_candidates.append(candidate)

        ok, last_error, winner = await _attempt(scan_candidates)
        if ok and winner:
            save_last_ble_id(speaker_mac, winner)
            _auto_connect_speaker(speaker_mac)
            return 0

    # If we stayed paired and failed, one unpair+retry pass can help on some macOS stacks.
    if speaker_mac and keep_paired:
        print("Wake failed while paired; retrying once after unpair...")
        _unpair_for_ble(speaker_mac)
        await asyncio.sleep(1.5)
        retry_candidates = deduped_candidates + scanned
        retry_deduped: list[str] = []
        seen_retry: set[str] = set()
        for candidate in retry_candidates:
            cid = candidate.upper()
            if cid in seen_retry:
                continue
            seen_retry.add(cid)
            retry_deduped.append(candidate)
        if retry_deduped:
            ok, last_error, winner = await _attempt(retry_deduped)
            if ok and winner:
                save_last_ble_id(speaker_mac, winner)
                _auto_connect_speaker(speaker_mac)
                return 0

    print(f"ERROR: BLE power-on failed — {last_error}", file=sys.stderr)
    print(
        "  → Speaker advertises multiple BLE identities; if this persists, run:\n"
        "    1) off\n"
        "    2) scan\n"
        "    3) on --device <strongest UE candidate>\n"
        "  → Keep Bluetooth settings open and verify the speaker is not already bonded/connecting.",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control a UE BOOM / MEGABOOM speaker on macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ueboom.py scan\n"
            "  python ueboom.py probe --device <UUID>\n"
            "  python ueboom.py on --host-mac AA:BB:CC:DD:EE:FF\n"
            "  python ueboom.py off --speaker-mac AA:BB:CC:DD:EE:FF\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_p = subparsers.add_parser("scan", help="Scan for nearby BLE devices")
    scan_p.add_argument(
        "--scan-seconds",
        type=float,
        default=DEFAULT_SCAN_SECONDS,
        help=f"Scan duration in seconds (default: {DEFAULT_SCAN_SECONDS})",
    )

    probe_p = subparsers.add_parser("probe", help="Connect and list GATT services/characteristics")
    probe_p.add_argument("--device", required=True, help="macOS CoreBluetooth UUID")

    on_p = subparsers.add_parser(
        "on",
        help="Wake speaker from BLE standby (speaker must be off/in standby)",
    )
    on_p.add_argument(
        "--device", required=False, default=None,
        help="CoreBluetooth UUID of the speaker (from 'scan'). "
             f"If omitted, auto-scans to find it. Can also be set via {ENV_DEVICE_ID}.",
    )
    on_p.add_argument(
        "--host-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Your Mac's Bluetooth address (Option+click Bluetooth menu bar icon). "
            f"Can also be set via {ENV_HOST_MAC}."
        ),
    )
    on_p.add_argument(
        "--speaker-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Classic BT MAC of the speaker. Used for BLE ID cache keying and optional "
            f"unpair retry. Can also be set via {ENV_SPEAKER_MAC}."
        ),
    )
    on_p.add_argument(
        "--payload-hex", required=False, default=None,
        help=(
            "Explicit wake payload in hex (e.g. 6C7E67D933DE01). "
            f"If set, overrides --host-mac derived payload variants. Can also be set via {ENV_PAYLOAD_HEX}."
        ),
    )
    on_p.add_argument(
        "--trusted-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Trusted controller BT MAC used by UE wake protocol. "
            f"If set, payload becomes <trusted-mac-no-separators>01. Can also be set via {ENV_TRUSTED_MAC}."
        ),
    )
    on_p.add_argument(
        "--keep-paired",
        action="store_true",
        help="(Deprecated) Same as default behavior: do not unpair before wake.",
    )
    on_p.add_argument(
        "--unpair-before-on",
        action="store_true",
        help="Force unpair before wake attempts (normally not needed).",
    )

    off_p = subparsers.add_parser(
        "off",
        help="Turn off speaker via classic BT RFCOMM (speaker must be on)",
    )
    off_p.add_argument(
        "--speaker-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Classic BT MAC of the speaker. "
            f"Can also be set via {ENV_SPEAKER_MAC}."
        ),
    )

    cycle_p = subparsers.add_parser(
        "cycle",
        help="Deterministic off->on sequence with strict timing",
    )
    cycle_p.add_argument(
        "--speaker-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Classic BT MAC of the speaker. "
            f"Can also be set via {ENV_SPEAKER_MAC}."
        ),
    )
    cycle_p.add_argument(
        "--host-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Your Mac's Bluetooth address. "
            f"Can also be set via {ENV_HOST_MAC}."
        ),
    )
    cycle_p.add_argument(
        "--device", required=False, default=None,
        help=(
            "Optional CoreBluetooth UUID to prioritize for wake. "
            f"Can also be set via {ENV_DEVICE_ID}."
        ),
    )
    cycle_p.add_argument(
        "--payload-hex", required=False, default=None,
        help=(
            "Explicit wake payload hex (overrides host-mac derived payloads). "
            f"Can also be set via {ENV_PAYLOAD_HEX}."
        ),
    )
    cycle_p.add_argument(
        "--trusted-mac", required=False, default=None,
        metavar="AA:BB:CC:DD:EE:FF",
        help=(
            "Trusted controller BT MAC used to build wake payload <mac>01. "
            f"Can also be set via {ENV_TRUSTED_MAC}."
        ),
    )
    cycle_p.add_argument(
        "--off-wait", type=float, default=0.8,
        help="Delay in seconds between off and wake attempt (default: 0.8)",
    )
    cycle_p.add_argument(
        "--keep-paired",
        action="store_true",
        help="(Deprecated) Same as default behavior: do not unpair before wake.",
    )
    cycle_p.add_argument(
        "--unpair-before-on",
        action="store_true",
        help="Force unpair before wake attempts (normally not needed).",
    )

    return parser


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    def choose_arg_or_env(cli_value: Optional[str], env_name: str) -> Optional[str]:
        return cli_value if cli_value else env_default(env_name)

    if args.command == "scan":
        try:
            items = await scan_devices(args.scan_seconds)
            print_scan_results(items)
            return 0
        except Exception as exc:
            print(f"Scan failed: {exc}", file=sys.stderr)
            print(
                "On macOS, make sure Bluetooth is enabled and your terminal/Python "
                "process has Bluetooth permission.",
                file=sys.stderr,
            )
            return 1

    if args.command == "probe":
        return await probe_device(args.device)

    if args.command == "on":
        host_mac_raw = choose_arg_or_env(getattr(args, "host_mac", None), ENV_HOST_MAC)
        payload_hex = choose_arg_or_env(getattr(args, "payload_hex", None), ENV_PAYLOAD_HEX)
        trusted_mac = choose_arg_or_env(getattr(args, "trusted_mac", None), ENV_TRUSTED_MAC)
        speaker_mac = choose_arg_or_env(getattr(args, "speaker_mac", None), ENV_SPEAKER_MAC)
        device_id = choose_arg_or_env(getattr(args, "device", None), ENV_DEVICE_ID)

        host_mac: Optional[str] = None
        if host_mac_raw:
            try:
                host_mac = normalize_host_mac(host_mac_raw)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

        if trusted_mac:
            try:
                payload_hex = build_trusted_payload_hex(trusted_mac)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

        if not host_mac and not payload_hex:
            print(
                "ERROR: missing host identity. Provide --host-mac (or UEBOOM_HOST_MAC), "
                "or provide --payload-hex/--trusted-mac.",
                file=sys.stderr,
            )
            return 2

        return await ble_power_on(
            device_id,
            host_mac,
            speaker_mac,
            payload_hex,
            not bool(getattr(args, "unpair_before_on", False)),
        )

    if args.command == "off":
        speaker_mac = choose_arg_or_env(getattr(args, "speaker_mac", None), ENV_SPEAKER_MAC)
        if not speaker_mac:
            print(
                f"ERROR: missing speaker MAC. Provide --speaker-mac or set {ENV_SPEAKER_MAC}.",
                file=sys.stderr,
            )
            return 2
        return rfcomm_power_off(speaker_mac)

    if args.command == "cycle":
        host_mac_raw = choose_arg_or_env(getattr(args, "host_mac", None), ENV_HOST_MAC)
        payload_hex = choose_arg_or_env(getattr(args, "payload_hex", None), ENV_PAYLOAD_HEX)
        trusted_mac = choose_arg_or_env(getattr(args, "trusted_mac", None), ENV_TRUSTED_MAC)
        speaker_mac = choose_arg_or_env(getattr(args, "speaker_mac", None), ENV_SPEAKER_MAC)
        device_id = choose_arg_or_env(getattr(args, "device", None), ENV_DEVICE_ID)

        host_mac: Optional[str] = None
        if host_mac_raw:
            try:
                host_mac = normalize_host_mac(host_mac_raw)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

        if trusted_mac:
            try:
                payload_hex = build_trusted_payload_hex(trusted_mac)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

        if not speaker_mac:
            print(
                f"ERROR: missing speaker MAC. Provide --speaker-mac or set {ENV_SPEAKER_MAC}.",
                file=sys.stderr,
            )
            return 2

        if not host_mac and not payload_hex:
            print(
                "ERROR: missing host identity. Provide --host-mac (or UEBOOM_HOST_MAC), "
                "or provide --payload-hex/--trusted-mac.",
                file=sys.stderr,
            )
            return 2

        rc = rfcomm_power_off(speaker_mac)
        if rc != 0:
            return rc

        wait_s = max(0.0, float(args.off_wait))
        if wait_s > 0:
            print(f"Waiting {wait_s:.1f}s before wake attempt...")
            await asyncio.sleep(wait_s)

        return await ble_power_on(
            device_id,
            host_mac,
            speaker_mac,
            payload_hex,
            not bool(getattr(args, "unpair_before_on", False)),
        )

    parser.print_help()
    return 2


def main() -> None:
    try:
        rc = asyncio.run(async_main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
