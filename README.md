# UE MEGABOOM Control on macOS

Python CLI for powering UE BOOM / MEGABOOM speakers off and on from macOS.

This repository is script-focused and public-facing:
- `ueboom.py` is the main deliverable.
- No app bundle or packaging steps are required to use it.

## How It Works

UE speakers use two different control paths:
- `off`: classic Bluetooth RFCOMM/SPP
- `on`: BLE GATT write

This means OFF and ON are not symmetric:
- OFF works when the speaker is already on and reachable over classic BT.
- ON works when the speaker is in BLE standby (typically after OFF).

## Requirements

- macOS
- Python 3.10+
- `bleak`
- `pyobjc-framework-IOBluetooth`
- Optional: `blueutil` (improves reconnect behavior and unpair flows)

Install dependencies:

```bash
python -m pip install bleak pyobjc-framework-IOBluetooth
```

## Bluetooth Permissions (macOS)

Grant Bluetooth permission to the process you run from (Terminal, iTerm, VS Code, etc.).

If missing, BLE commands may fail with timeouts or connection errors even when the speaker is nearby.

## Quick Start

1. Find speaker and candidate BLE identities:

```bash
python ueboom.py scan
```

2. Turn speaker OFF (speaker must be on and connected):

```bash
python ueboom.py off --speaker-mac AA:BB:CC:DD:EE:FF
```

3. Turn speaker ON from standby:

```bash
python ueboom.py on --host-mac AA:BB:CC:DD:EE:FF --speaker-mac AA:BB:CC:DD:EE:FF
```

4. Optional deterministic cycle:

```bash
python ueboom.py cycle --host-mac AA:BB:CC:DD:EE:FF --speaker-mac AA:BB:CC:DD:EE:FF
```

## Practical Notes From Real Usage

### 1) Use explicit trusted controller when wake fails

If wake fails with BLE errors like:
- `CBATTErrorDomain Code=15 "Encryption is insufficient."`

try overriding trusted controller identity explicitly:

```bash
python ueboom.py on \
  --host-mac AA:BB:CC:DD:EE:FF \
  --speaker-mac 11:22:33:44:55:66 \
  --trusted-mac AA:BB:CC:DD:EE:FF
```

In practice, this often fixes stale or mismatched trusted payload issues.

### 2) BLE device IDs can rotate

CoreBluetooth UUIDs are not always stable across sessions.

`ueboom.py` mitigates this via:
- advertisement fingerprinting (FE9F service UUID and Logitech mfr ID `224`)
- scan/retry candidate strategy
- per-speaker cache of last known working BLE ID

### 3) OFF command precondition is strict

`off` sends RFCOMM payload `02 01 B6` on channel `1`.

If the speaker is not currently reachable via classic Bluetooth, OFF can fail with RFCOMM channel/open errors.

## Environment Variables

You can set defaults so commands are shorter:

- `UEBOOM_HOST_MAC`
- `UEBOOM_SPEAKER_MAC`
- `UEBOOM_DEVICE_ID`
- `UEBOOM_PAYLOAD_HEX`
- `UEBOOM_TRUSTED_MAC`
- `UEBOOM_FALLBACK_DEVICE_IDS` (comma-separated CoreBluetooth UUIDs)

Example:

```bash
export UEBOOM_HOST_MAC="AA:BB:CC:DD:EE:FF"
export UEBOOM_SPEAKER_MAC="11:22:33:44:55:66"
python ueboom.py on
python ueboom.py off
```

## Known Limitations

- Behavior can differ by UE generation/firmware.
- BLE reliability depends on timing, RF conditions, and macOS Bluetooth stack state.
- This is reverse engineered behavior, not an official UE API.

## References

- Reddit discussion that motivated PacketLogger-based reverse engineering: `https://www.reddit.com/r/shortcuts/comments/dz9zun/`
- Community gist with related UE BOOM wake notes: `https://gist.github.com/marcust/af93ff47899583f5a52f`