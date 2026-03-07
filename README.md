# UE MEGABOOM Control on macOS

This repo documents a practical reverse-engineering workflow for powering a UE speaker off and on from macOS using Python.

It includes:
- `ueboom.py`: public, reusable CLI with no personal device IDs hardcoded.

## What We Found

### 1) OFF and ON use different radios/protocols
- `off` is classic Bluetooth (RFCOMM / SPP style message).
- `on` is BLE (GATT write to a specific characteristic).

That means OFF and ON are not symmetric operations:
- OFF only works when the speaker is already on and reachable over classic BT.
- ON works when the speaker is in BLE standby and advertising.

### 2) OFF signal format (classic BT)
- RFCOMM channel: `1`
- Payload bytes: `02 01 B6`

In `ueboom.py`, this is sent through macOS `IOBluetooth` (`pyobjc-framework-IOBluetooth`).

### 3) ON signal format (BLE)
- Characteristic UUID: `c6d6dc0d-07f5-47ef-9b59-630622b01fd3`
- Observed wake payload shape: `<12 hex chars><01>`

Default generated payload is based on a controller identity (`host_mac`) and appends `01`.

### 4) About the "encrypted command"
From packet-log analysis, the wake payload is best treated as an authenticated/trusted-controller token rather than plain text control bytes.

Practical interpretation:
- It behaves like a compact identity-based command marker.
- The trailing `01` acts as an operation marker for wake in the observed captures.
- Different firmware/device states may accept slightly different payload variants (normal or byte-reversed MAC derivation).

This repo does not claim to have fully broken UE's internal crypto scheme. Instead, it implements a robust, reproducible wake strategy from empirical captures.

### 5) BLE identity is not always stable
On macOS, the CoreBluetooth device UUID used for BLE may vary across sessions/states.

Mitigations implemented:
- Candidate scanning using UE/Logitech advertisement fingerprint:
  - FE9F service UUID (Fast Pair family)
  - Logitech manufacturer ID `224`
- Last-known BLE ID cache per speaker MAC
- Optional fallback UUID list via env var

## CLI Usage

```bash
python ueboom.py scan
python ueboom.py probe --device <corebluetooth-uuid>
python ueboom.py off --speaker-mac AA:BB:CC:DD:EE:FF
python ueboom.py on --host-mac AA:BB:CC:DD:EE:FF --speaker-mac AA:BB:CC:DD:EE:FF
python ueboom.py cycle --host-mac AA:BB:CC:DD:EE:FF --speaker-mac AA:BB:CC:DD:EE:FF
```

## Environment Variables

`ueboom.py` supports env defaults so commands can be shorter and reusable:

- `UEBOOM_HOST_MAC`
- `UEBOOM_SPEAKER_MAC`
- `UEBOOM_DEVICE_ID`
- `UEBOOM_PAYLOAD_HEX`
- `UEBOOM_TRUSTED_MAC`
- `UEBOOM_FALLBACK_DEVICE_IDS` (comma-separated UUIDs)

Example:

```bash
export UEBOOM_HOST_MAC="AA:BB:CC:DD:EE:FF"
export UEBOOM_SPEAKER_MAC="11:22:33:44:55:66"
python ueboom.py on
python ueboom.py off
```

## Requirements

- macOS
- Python 3.10+
- `bleak`
- `pyobjc-framework-IOBluetooth`
- Optional but recommended: `blueutil` for post-wake auto-connect handling

## Limitations

- Firmware behavior can vary by speaker generation/version.
- BLE wake reliability depends on timing, RF conditions, and macOS BT stack state.
- This is reverse engineered behavior, not an official UE API.

## Device Compatibility Note

This script and workflow were developed and validated on a 2015 UE MEGABOOM.

Newer UE BOOM/MEGABOOM generations may use different firmware behavior, BLE identifiers, or wake/control payload handling, so some commands or reliability characteristics may differ.

## References

The following materials were meaningful during implementation and troubleshooting:

- Reddit discussion that motivated/confirmed reverse-engineering approach (PacketLogger diffing, BLE wake write, and `<BT_MAC> + 01` insight): `https://www.reddit.com/r/shortcuts/comments/dz9zun/