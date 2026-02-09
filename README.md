# pixmob2mqtt

Send PixMob-style IR codes (Flipper `.ir` files) to a Tasmota device via MQTT.

Includes a simple show player that can sequence, randomize, and pause IR cues based on a JSON file.

## Requirements

- Python 3.8+
- MQTT broker reachable from your machine and your Tasmota device
- Tasmota IR capable device (IRSend enabled)

## Install

```bash
pip install -r requirements.txt
```

## Quick Start

List available codes in a `.ir` file:

```bash
python3 pixmob2mqtt.py PixMob_main.ir --list
```

Send a single code:

```bash
python3 pixmob2mqtt.py PixMob_main.ir 192.168.1.10 TURQ --topic tasmota_771F55
```

With MQTT auth:

```bash
python3 pixmob2mqtt.py PixMob_main.ir 192.168.1.10 TURQ --topic tasmota_771F55 --user mqtt --password secret
```

Using the full color set:

```bash
python3 pixmob2mqtt.py pixmob_all_colors.ir 192.168.1.10 TURQ_3 --topic tasmota_771F55
```

## Show Player

The show player reads a JSON file and plays cues through MQTT.

Dry-run to see what would be sent:

```bash
python3 show_player.py PixMob_main.ir 192.168.1.10 sample_show.json --topic tasmota_771F55 --dry-run
```

Play a show:

```bash
python3 show_player.py PixMob_main.ir 192.168.1.10 sample_show.json --topic tasmota_771F55
```

### Show File Format

`sample_show.json` demonstrates the structure. Supported actions:

- `send`: send a single code (with optional repeats and gaps)
- `sequence`: send a list of codes for durations at a given rate
- `random`: randomly pick from a pool for a duration
- `pause`: wait for a number of seconds

Minimal example:

```json
{
  "default_gap_s": 0.1,
  "steps": [
    {"action": "send", "code": "RED", "repeat": 3, "gap_s": 0.2},
    {"action": "pause", "seconds": 1.0},
    {"action": "random", "pool": ["RED", "GRN"], "total_s": 5, "rate_hz": 3}
  ]
}
```

### Demo Shows

The repo includes a few demo shows:

- `demo_show_warmup.json` (use with `PixMob_main.ir`)
- `demo_show_fade_cycle.json` (use with `PixMob_main.ir`)
- `demo_show_all_colors.json` (use with `pixmob_all_colors.ir`)

Examples:

```bash
python3 show_player.py PixMob_main.ir 192.168.1.10 demo_show_warmup.json --topic tasmota_771F55 --dry-run
python3 show_player.py PixMob_main.ir 192.168.1.10 demo_show_fade_cycle.json --topic tasmota_771F55
python3 show_player.py pixmob_all_colors.ir 192.168.1.10 demo_show_all_colors.json --topic tasmota_771F55
```

## How It Works

`pixmob2mqtt.py` parses the `.ir` file and publishes an `IRSend` command to:

```
cmnd/<topic>/IRSend
```

The payload is formatted as Tasmota raw: `<frequency_khz>,<pulse_us>,<pulse_us>,...`.

## Files

- `pixmob2mqtt.py`: send a single code to MQTT/Tasmota
- `show_player.py`: play a JSON show file
- `PixMob_main.ir`: main PixMob code set
- `pixmob_all_colors.ir`: extended color set
- `sample_show.json`: example show
- `demo_show_warmup.json`: demo show (main set)
- `demo_show_fade_cycle.json`: demo show (main set, fade codes)
- `demo_show_all_colors.json`: demo show (all colors)

## Notes

- The `broker` argument is the MQTT broker hostname/IP (not the Tasmota device IP unless it is also your broker).
- Ensure your Tasmota topic matches the device topic (e.g., `tasmota_771F55`).
- Successfully tested with Pixmob X4 Gen 3.1 bands and an S06 IR controller running Tasmota.

## Credits

IR code files are sourced from:

- `flipper-pixmob-ir-codes` by Daniel Weidman: https://github.com/danielweidman/flipper-pixmob-ir-codes
- `pixmob-ir-reverse-engineering` by Daniel Weidman: https://github.com/danielweidman/pixmob-ir-reverse-engineering
