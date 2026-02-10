#!/usr/bin/env python3
"""
pixmob2mqtt.py

Usage examples:
  # Send TURQ_3 from all.ir to Tasmota via MQTT broker 192.168.178.25
  python3 pixmob2mqtt.py all.ir 192.168.178.25 TURQ_3 --topic tasmota_771F55

  # List all available code names in a file
  python3 pixmob2mqtt.py all.ir --list

  # With MQTT auth
  python3 pixmob2mqtt.py all.ir 192.168.178.25 TURQ_3 --topic tasmota_771F55 --user mqtt --password secret

  # Beat-reactive mic mode (Ctrl+C to stop)
  python3 pixmob2mqtt.py all.ir 192.168.178.25 --topic tasmota_771F55 --mic --mic-code TURQ
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Missing dependency: paho-mqtt\nInstall with: pip install paho-mqtt", file=sys.stderr)
    sys.exit(2)

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None


@dataclass
class IRCode:
    name: str
    frequency_hz: int
    duty_cycle: Optional[float]
    data_us: List[int]


IR_NAME_RE = re.compile(r'^\s*name:\s*(.+?)\s*$')
IR_FREQ_RE = re.compile(r'^\s*frequency:\s*(\d+)\s*$')
IR_DUTY_RE = re.compile(r'^\s*duty_cycle:\s*([0-9]*\.?[0-9]+)\s*$')
IR_DATA_RE = re.compile(r'^\s*data:\s*(.*)\s*$')


def parse_ir_file(path: str) -> Dict[str, IRCode]:
    """
    Parse a Flipper-style .ir file.
    Supports long/multi-line data blocks (if wrapped).
    Returns dict keyed by code name.
    """
    codes: Dict[str, IRCode] = {}

    current_name: Optional[str] = None
    current_freq: Optional[int] = None
    current_duty: Optional[float] = None
    current_data: List[int] = []
    in_data_continuation = False

    def flush_current():
        nonlocal current_name, current_freq, current_duty, current_data, in_data_continuation
        if current_name and current_freq and current_data:
            codes[current_name] = IRCode(
                name=current_name,
                frequency_hz=current_freq,
                duty_cycle=current_duty,
                data_us=current_data[:],
            )
        # reset
        current_name = None
        current_freq = None
        current_duty = None
        current_data = []
        in_data_continuation = False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Comment line resets data continuation
            if line.strip().startswith("#"):
                in_data_continuation = False
                continue

            m = IR_NAME_RE.match(line)
            if m:
                # new entry starts -> flush old
                flush_current()
                current_name = m.group(1).strip()
                continue

            m = IR_FREQ_RE.match(line)
            if m:
                current_freq = int(m.group(1))
                continue

            m = IR_DUTY_RE.match(line)
            if m:
                try:
                    current_duty = float(m.group(1))
                except ValueError:
                    current_duty = None
                continue

            m = IR_DATA_RE.match(line)
            if m:
                # data may be long; grab ints from rest of line
                tail = m.group(1)
                nums = re.findall(r'\d+', tail)
                current_data.extend(int(n) for n in nums)
                in_data_continuation = True
                continue

            # If data wrapped onto next lines, keep collecting integers
            if in_data_continuation:
                nums = re.findall(r'\d+', line)
                if nums:
                    current_data.extend(int(n) for n in nums)
                else:
                    in_data_continuation = False

    flush_current()
    return codes


def tasmota_payload(code: IRCode) -> str:
    # Tasmota raw expects kHz integer as first arg (e.g., 38)
    freq_khz = int(round(code.frequency_hz / 1000.0))
    data_csv = ",".join(str(x) for x in code.data_us)
    return f"{freq_khz},{data_csv}"


def mqtt_publish(
    broker: str,
    port: int,
    topic: str,
    payload: str,
    user: Optional[str],
    password: Optional[str],
    client_id: str,
    qos: int = 0,
    retain: bool = False,
    timeout_s: int = 5,
) -> None:
    client = mqtt.Client(client_id=client_id, clean_session=True)

    if user is not None:
        client.username_pw_set(user, password=password)

    rc = client.connect(broker, port, keepalive=30)
    if rc != 0:
        raise RuntimeError(f"MQTT connect failed rc={rc}")

    client.loop_start()
    info = client.publish(topic, payload=payload, qos=qos, retain=retain)
    info.wait_for_publish(timeout=timeout_s)
    client.loop_stop()
    client.disconnect()

    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed rc={info.rc}")

def _resolve_input_device(device_spec: Optional[str]) -> Optional[int]:
    if device_spec is None:
        return None
    try:
        return int(device_spec)
    except ValueError:
        pass

    if sd is None:
        raise RuntimeError("sounddevice not available")

    device_spec_lower = device_spec.lower()
    for i, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0 and device_spec_lower in dev["name"].lower():
            return i
    raise ValueError(f"No input device matching '{device_spec}'.")

def run_mic_mode(
    codes: Dict[str, IRCode],
    broker: str,
    port: int,
    topic: str,
    code_name: str,
    user: Optional[str],
    password: Optional[str],
    client_id: str,
    qos: int,
    retain: bool,
    dry_run: bool,
    device_spec: Optional[str],
    samplerate: int,
    blocksize: int,
    threshold_ratio: float,
    floor: float,
    min_interval_s: float,
):
    if sd is None or np is None:
        raise RuntimeError("Mic mode requires sounddevice and numpy. Install with: pip install sounddevice numpy")

    if code_name not in codes:
        raise KeyError(f"Code '{code_name}' not found in .ir file")

    payload = tasmota_payload(codes[code_name])
    device = _resolve_input_device(device_spec)
    ema = None
    last_trigger = 0.0

    if dry_run:
        client = None
    else:
        client = mqtt.Client(client_id=client_id, clean_session=True)
        if user is not None:
            client.username_pw_set(user, password=password)
        rc = client.connect(broker, port, keepalive=30)
        if rc != 0:
            raise RuntimeError(f"MQTT connect failed rc={rc}")
        client.loop_start()

    print("Mic mode active. Press Ctrl+C to stop.")

    try:
        with sd.InputStream(
            device=device,
            channels=1,
            samplerate=samplerate,
            blocksize=blocksize,
            dtype="float32",
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    continue

                rms = float(np.sqrt(np.mean(np.square(data))))
                if ema is None:
                    ema = rms
                else:
                    ema = (ema * 0.9) + (rms * 0.1)

                now = time.perf_counter()
                trigger_level = max(ema * threshold_ratio, floor)
                if rms >= trigger_level and (now - last_trigger) >= min_interval_s:
                    last_trigger = now
                    if dry_run:
                        print(f"[MIC] BEAT rms={rms:.4f} ema={ema:.4f}")
                    else:
                        info = client.publish(topic, payload=payload, qos=qos, retain=retain)
                        info.wait_for_publish(timeout=5)
                        if info.rc != mqtt.MQTT_ERR_SUCCESS:
                            raise RuntimeError(f"MQTT publish failed rc={info.rc}")
    finally:
        if not dry_run and client is not None:
            client.loop_stop()
            client.disconnect()


def main():
    ap = argparse.ArgumentParser(description="Send PixMob .ir codes to Tasmota via MQTT")
    ap.add_argument("irfile", nargs="?", help="Path to .ir file")
    ap.add_argument("broker", nargs="?", help="MQTT broker hostname/IP (NOT the Tasmota device IP unless it is the broker)")
    ap.add_argument("code", nargs="?", help="Code name to send (e.g., TURQ_3). In --mic mode, this overrides --mic-code.")
    ap.add_argument("--topic", help="Tasmota topic (e.g., tasmota_771F55). Publishes to cmnd/<topic>/IRSend")
    ap.add_argument("--port", type=int, default=1883, help="MQTT port (default 1883)")
    ap.add_argument("--user", help="MQTT username")
    ap.add_argument("--password", help="MQTT password")
    ap.add_argument("--client-id", default="pixmob2mqtt", help="MQTT client id")
    ap.add_argument("--qos", type=int, default=0, choices=[0, 1, 2], help="MQTT QoS")
    ap.add_argument("--retain", action="store_true", help="Publish retained message")
    ap.add_argument("--list", action="store_true", help="List available code names in the file and exit")
    ap.add_argument("--mic", action="store_true", help="Beat-reactive mic mode")
    ap.add_argument("--mic-code", default="WHITISH", help="Code name to send on each beat")
    ap.add_argument("--mic-device", help="Input device index or substring match")
    ap.add_argument("--mic-samplerate", type=int, default=44100)
    ap.add_argument("--mic-block", type=int, default=1024, help="Audio block size")
    ap.add_argument("--mic-threshold", type=float, default=1.8, help="Trigger ratio vs rolling RMS")
    ap.add_argument("--mic-floor", type=float, default=0.02, help="Minimum RMS to trigger")
    ap.add_argument("--mic-min-interval", type=float, default=0.15, help="Minimum seconds between triggers")
    ap.add_argument("--dry-run", action="store_true", help="Print beats, do not send MQTT (mic mode)")

    args = ap.parse_args()

    if not args.irfile:
        ap.print_help()
        return 1

    codes = parse_ir_file(args.irfile)

    if args.list:
        for name in sorted(codes.keys()):
            print(name)
        return 0

    if not args.broker:
        ap.error("broker is required unless --list is used")

    if not args.topic:
        ap.error("--topic is required (example: --topic tasmota_771F55)")

    if args.mic:
        code_name = args.code or args.mic_code
        run_mic_mode(
            codes=codes,
            broker=args.broker,
            port=args.port,
            topic=f"cmnd/{args.topic}/IRSend",
            code_name=code_name,
            user=args.user,
            password=args.password,
            client_id=args.client_id,
            qos=args.qos,
            retain=args.retain,
            dry_run=args.dry_run,
            device_spec=args.mic_device,
            samplerate=args.mic_samplerate,
            blocksize=args.mic_block,
            threshold_ratio=args.mic_threshold,
            floor=args.mic_floor,
            min_interval_s=args.mic_min_interval,
        )
        return 0

    if not args.code:
        ap.error("code is required unless --list or --mic is used")

    if args.code not in codes:
        close = [n for n in codes.keys() if n.lower() == args.code.lower()]
        if close:
            hint = close[0]
            ap.error(f"Code '{args.code}' not found. Did you mean '{hint}' (case mismatch)?")
        ap.error(f"Code '{args.code}' not found. Use --list to see available names.")

    code = codes[args.code]
    payload = tasmota_payload(code)
    mqtt_topic = f"cmnd/{args.topic}/IRSend"

    mqtt_publish(
        broker=args.broker,
        port=args.port,
        topic=mqtt_topic,
        payload=payload,
        user=args.user,
        password=args.password,
        client_id=args.client_id,
        qos=args.qos,
        retain=args.retain,
    )

    print(f"Sent '{args.code}' to {mqtt_topic} via broker {args.broker}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
