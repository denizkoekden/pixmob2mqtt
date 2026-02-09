#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
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
    codes: Dict[str, IRCode] = {}
    current_name: Optional[str] = None
    current_freq: Optional[int] = None
    current_duty: Optional[float] = None
    current_data: List[int] = []
    in_data = False

    def flush():
        nonlocal current_name, current_freq, current_duty, current_data, in_data
        if current_name and current_freq and current_data:
            codes[current_name] = IRCode(current_name, current_freq, current_duty, current_data[:])
        current_name = None
        current_freq = None
        current_duty = None
        current_data = []
        in_data = False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip().startswith("#"):
                in_data = False
                continue
            m = IR_NAME_RE.match(line)
            if m:
                flush()
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
                nums = re.findall(r"\d+", m.group(1))
                current_data.extend(int(n) for n in nums)
                in_data = True
                continue
            if in_data:
                nums = re.findall(r"\d+", line)
                if nums:
                    current_data.extend(int(n) for n in nums)
                else:
                    in_data = False

    flush()
    return codes

def tasmota_payload(code: IRCode) -> str:
    freq_khz = int(round(code.frequency_hz / 1000.0))
    return f"{freq_khz}," + ",".join(str(x) for x in code.data_us)

def connect_mqtt(broker: str, port: int, user: Optional[str], password: Optional[str], client_id: str):
    client = mqtt.Client(client_id=client_id, clean_session=True)
    if user is not None:
        client.username_pw_set(user, password=password)
    rc = client.connect(broker, port, keepalive=30)
    if rc != 0:
        raise RuntimeError(f"MQTT connect failed rc={rc}")
    client.loop_start()
    return client

def publish_irs(client: mqtt.Client, tasmota_topic: str, payload: str, qos: int = 0):
    topic = f"cmnd/{tasmota_topic}/IRSend"
    info = client.publish(topic, payload=payload, qos=qos, retain=False)
    info.wait_for_publish(timeout=5)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed rc={info.rc}")

def sleep_precise(seconds: float):
    # good enough for show cues (tens of ms precision on typical Linux)
    end = time.perf_counter() + seconds
    while True:
        now = time.perf_counter()
        if now >= end:
            return
        time.sleep(min(0.01, end - now))

def run_show(codes: Dict[str, IRCode], show: dict, client: mqtt.Client, tasmota_topic: str, dry_run: bool):
    default_gap = float(show.get("default_gap_s", 0.0))

    for i, step in enumerate(show.get("steps", []), start=1):
        action = step.get("action", "send")

        if action == "send":
            name = step["code"]
            if name not in codes:
                raise KeyError(f"Step {i}: code '{name}' not found in .ir file")
            payload = tasmota_payload(codes[name])
            repeat = int(step.get("repeat", 1))
            gap_s = float(step.get("gap_s", default_gap))
            hold_s = float(step.get("hold_s", 0.0))  # time after finishing repeats

            for r in range(repeat):
                if dry_run:
                    print(f"[{i}] SEND {name} ({r+1}/{repeat})")
                else:
                    publish_irs(client, tasmota_topic, payload)
                if r < repeat - 1 and gap_s > 0:
                    sleep_precise(gap_s)

            if hold_s > 0:
                sleep_precise(hold_s)

        elif action == "sequence":
            # play a list of codes in order, each for duration_s (sent as repeated cues)
            seq = step["sequence"]  # list of {code, duration_s, rate_hz}
            for item in seq:
                name = item["code"]
                duration_s = float(item.get("duration_s", 1.0))
                rate_hz = float(item.get("rate_hz", 2.0))  # how often to re-send during this duration
                if name not in codes:
                    raise KeyError(f"Step {i}: code '{name}' not found in .ir file")
                payload = tasmota_payload(codes[name])

                period = 1.0 / max(rate_hz, 0.1)
                t_end = time.perf_counter() + duration_s
                n = 0
                while time.perf_counter() < t_end:
                    n += 1
                    if dry_run:
                        print(f"[{i}] SEQ {name} #{n}")
                    else:
                        publish_irs(client, tasmota_topic, payload)
                    sleep_precise(period)

        elif action == "random":
            # random color roulette from a list for total_s, at rate_hz
            pool = step["pool"]  # list of code names
            total_s = float(step.get("total_s", 10.0))
            rate_hz = float(step.get("rate_hz", 3.0))
            period = 1.0 / max(rate_hz, 0.1)

            for name in pool:
                if name not in codes:
                    raise KeyError(f"Step {i}: code '{name}' not found in .ir file")
            t_end = time.perf_counter() + total_s
            n = 0
            while time.perf_counter() < t_end:
                n += 1
                name = random.choice(pool)
                payload = tasmota_payload(codes[name])
                if dry_run:
                    print(f"[{i}] RAND {name} #{n}")
                else:
                    publish_irs(client, tasmota_topic, payload)
                sleep_precise(period)

        elif action == "pause":
            secs = float(step.get("seconds", 1.0))
            if dry_run:
                print(f"[{i}] PAUSE {secs}s")
            sleep_precise(secs)

        else:
            raise ValueError(f"Step {i}: unknown action '{action}'")

def main():
    ap = argparse.ArgumentParser(description="Play a PixMob show via Tasmota IRSend over MQTT")
    ap.add_argument("irfile", help="Path to .ir file")
    ap.add_argument("broker", help="MQTT broker IP/host")
    ap.add_argument("showfile", help="Path to show JSON file")
    ap.add_argument("--topic", required=True, help="Tasmota topic, e.g. tasmota_771F55")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--client-id", default="pixmob-show")
    ap.add_argument("--dry-run", action="store_true", help="Print steps, do not send MQTT")
    args = ap.parse_args()

    codes = parse_ir_file(args.irfile)

    with open(args.showfile, "r", encoding="utf-8") as f:
        show = json.load(f)

    if args.dry_run:
        client = None
        run_show(codes, show, client, args.topic, dry_run=True)
        return 0

    client = connect_mqtt(args.broker, args.port, args.user, args.password, args.client_id)
    try:
        run_show(codes, show, client, args.topic, dry_run=False)
    finally:
        client.loop_stop()
        client.disconnect()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
