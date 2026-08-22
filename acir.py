#!/usr/bin/env python3
"""
AC IR capture/demodulation helper for ATK-Logic edge-timestamp CSV exports.

No third-party dependencies.

Typical usage:
    python acir.py plan
    python acir.py capture
    python acir.py status
    python acir.py analyse
    python acir.py decode some_capture.csv
    python acir.py reprocess

By default, configuration is loaded from ./acir_config.json. Override it with:
    python acir.py --config /path/to/config.json capture
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def state_id(state: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())
    text = re.sub(r"-+", "-", text).strip("-._")
    return (text or "capture")[:max_len]


def state_label(state: dict[str, Any]) -> str:
    preferred = ["power", "mode", "temperature_c", "fan", "swing"]
    pieces: list[str] = []
    used: set[str] = set()
    for key in preferred:
        if key in state:
            value = state[key]
            if key == "temperature_c":
                pieces.append(f"{value}C")
            else:
                pieces.append(f"{key}-{value}")
            used.add(key)
    for key in sorted(state):
        if key not in used:
            pieces.append(f"{key}-{state[key]}")
    return "__".join(slug(str(x), 32) for x in pieces)


def bytes_hex(values: Iterable[int]) -> str:
    return " ".join(f"{x:02X}" for x in values)


def resolve_path(config_path: Path, value: str) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = config_path.parent / p
    return p.resolve()


def atomic_json_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def median_or_none(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def relpath_or_abs(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Configuration and capture plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedState:
    state: dict[str, Any]
    sid: str
    required_count: int
    experiments: tuple[str, ...]


@dataclass
class AppConfig:
    config_path: Path
    raw: dict[str, Any]
    inbox_file: Path
    dataset_dir: Path
    channel: str | None
    poll_interval_s: float
    stable_for_s: float
    demod: dict[str, Any]
    baseline: dict[str, Any]
    experiments: list[dict[str, Any]]


def load_config(path: Path) -> AppConfig:
    path = path.expanduser().resolve()
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if "inbox_file" not in raw or "dataset_dir" not in raw:
        raise ValueError("config must contain 'inbox_file' and 'dataset_dir'")
    remote = raw.get("remote", {})
    baseline = remote.get("baseline")
    experiments = remote.get("experiments")
    if not isinstance(baseline, dict) or not baseline:
        raise ValueError("config remote.baseline must be a non-empty object")
    if not isinstance(experiments, list) or not experiments:
        raise ValueError("config remote.experiments must be a non-empty array")

    demod_defaults = {
        "burst_split_us": 100.0,
        "frame_gap_us": 5000.0,
        "leader_min_us": 1500.0,
        "one_threshold_us": 800.0,
        "min_burst_us": 100.0,
    }
    demod_defaults.update(raw.get("demod", {}))

    return AppConfig(
        config_path=path,
        raw=raw,
        inbox_file=resolve_path(path, raw["inbox_file"]),
        dataset_dir=resolve_path(path, raw["dataset_dir"]),
        channel=raw.get("channel"),
        poll_interval_s=float(raw.get("poll_interval_s", 0.25)),
        stable_for_s=float(raw.get("stable_for_s", 0.75)),
        demod=demod_defaults,
        baseline=dict(baseline),
        experiments=list(experiments),
    )


def experiment_states(cfg: AppConfig, exp: dict[str, Any]) -> list[dict[str, Any]]:
    if exp.get("enabled", True) is False:
        return []

    base = dict(cfg.baseline)
    if "base_overrides" in exp:
        base.update(exp["base_overrides"])

    if "field" in exp:
        field = str(exp["field"])
        values = exp.get("values", [])
        if not isinstance(values, list) or not values:
            raise ValueError(f"experiment {exp.get('name')!r}: field sweep requires non-empty values")
        result = []
        for value in values:
            state = dict(base)
            state[field] = value
            result.append(state)
        return result

    if "states" in exp:
        result = []
        states = exp["states"]
        if not isinstance(states, list) or not states:
            raise ValueError(f"experiment {exp.get('name')!r}: states must be a non-empty array")
        for override in states:
            if not isinstance(override, dict):
                raise ValueError(f"experiment {exp.get('name')!r}: each states item must be an object")
            state = dict(base)
            state.update(override)
            result.append(state)
        return result

    state = dict(base)
    state.update(exp.get("overrides", {}))
    return [state]


def build_plan(cfg: AppConfig) -> list[PlannedState]:
    # Same physical remote state can appear in multiple experiments. We capture it
    # only as many times as the largest repeat requirement, and attach all of the
    # experiment names to it. This lets one capture satisfy multiple sweeps.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for index, exp in enumerate(cfg.experiments):
        name = str(exp.get("name") or f"experiment_{index + 1}")
        repeats = int(exp.get("repeats", 1))
        if repeats < 1:
            raise ValueError(f"experiment {name!r}: repeats must be >= 1")
        for state in experiment_states(cfg, exp):
            sid = state_id(state)
            if sid not in merged:
                merged[sid] = {
                    "state": state,
                    "required_count": repeats,
                    "experiments": [name],
                }
                order.append(sid)
            else:
                merged[sid]["required_count"] = max(merged[sid]["required_count"], repeats)
                if name not in merged[sid]["experiments"]:
                    merged[sid]["experiments"].append(name)

    return [
        PlannedState(
            state=merged[sid]["state"],
            sid=sid,
            required_count=merged[sid]["required_count"],
            experiments=tuple(merged[sid]["experiments"]),
        )
        for sid in order
    ]


# ---------------------------------------------------------------------------
# ATK CSV parsing and IR demodulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    time_s: float
    level: int


@dataclass
class Burst:
    start_s: float
    end_s: float
    edges: list[Edge]

    @property
    def duration_us(self) -> float:
        return (self.end_s - self.start_s) * 1_000_000.0


class DecodeError(RuntimeError):
    pass


def parse_atk_csv(path: Path, channel: str | None = None) -> tuple[list[Edge], dict[str, str]]:
    comments: dict[str, str] = {}
    data_lines: list[str] = []

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for raw_line in f:
            if raw_line.startswith(";"):
                text = raw_line[1:].strip()
                if ":" in text:
                    key, value = text.split(":", 1)
                    comments[key.strip()] = value.strip()
                continue
            if raw_line.strip():
                data_lines.append(raw_line)

    if not data_lines:
        raise DecodeError("CSV contains no tabular data")

    reader = csv.DictReader(data_lines, skipinitialspace=True)
    if not reader.fieldnames:
        raise DecodeError("CSV has no header")
    fields = [f.strip() for f in reader.fieldnames]

    time_field = next((f for f in fields if f.lower() in {"time(s)", "time", "time (s)"}), None)
    if time_field is None:
        time_field = next((f for f in fields if "time" in f.lower() and "system" not in f.lower()), None)
    if time_field is None:
        raise DecodeError(f"cannot find time column; columns are {fields}")

    if channel is not None:
        channel_field = next((f for f in fields if f == channel), None)
        if channel_field is None:
            raise DecodeError(f"configured channel {channel!r} not found; columns are {fields}")
    else:
        candidates = [f for f in fields if f != time_field and "systemtime" not in f.lower()]
        if not candidates:
            raise DecodeError("cannot find a logic channel column")
        channel_field = candidates[-1]

    edges: list[Edge] = []
    # DictReader keeps the original field spellings. Build a stripped-key row.
    for row in reader:
        clean = {(k or "").strip(): v for k, v in row.items()}
        try:
            t = float(str(clean[time_field]).strip())
            level = int(float(str(clean[channel_field]).strip()))
        except (KeyError, TypeError, ValueError):
            continue
        if level not in (0, 1):
            continue
        edges.append(Edge(t, level))

    if len(edges) < 4:
        raise DecodeError(f"only {len(edges)} usable edge rows found")

    # ATK exports a t=0 sample representing the current idle level. For this
    # remote the real transmission starts at the first falling edge. Dropping
    # the leading idle sample gives accurate leader-mark timing.
    while len(edges) >= 2 and edges[0].level == edges[1].level:
        edges.pop(0)
    if len(edges) >= 2 and edges[0].level == 1 and edges[1].level == 0:
        edges = edges[1:]

    comments["Detected channel"] = channel_field
    return edges, comments


def split_bursts(edges: list[Edge], burst_split_us: float, min_burst_us: float) -> list[Burst]:
    if not edges:
        return []
    groups: list[list[Edge]] = []
    start = 0
    for i in range(len(edges) - 1):
        gap_us = (edges[i + 1].time_s - edges[i].time_s) * 1_000_000.0
        if gap_us > burst_split_us:
            groups.append(edges[start : i + 1])
            start = i + 1
    groups.append(edges[start:])

    bursts: list[Burst] = []
    for group in groups:
        if len(group) < 2:
            continue
        burst = Burst(group[0].time_s, group[-1].time_s, group)
        # ATK sometimes emits one final level sample at the end of the capture.
        if burst.duration_us >= min_burst_us:
            bursts.append(burst)
    return bursts


def estimate_carrier_hz(bursts: list[Burst]) -> float | None:
    # Real carrier half-cycles in these captures form two strong clusters around
    # ~12 us and ~14 us. Threshold chatter adds much shorter intervals. Find the
    # two dominant 0.1-us bins between 8 and 20 us, sufficiently separated, and
    # add them to estimate a full carrier period.
    hist: Counter[float] = Counter()
    for burst in bursts:
        e = burst.edges
        for i in range(len(e) - 1):
            dt_us = (e[i + 1].time_s - e[i].time_s) * 1_000_000.0
            if 8.0 <= dt_us <= 20.0:
                hist[round(dt_us, 1)] += 1
    if not hist:
        return None

    peaks = hist.most_common(20)
    first = peaks[0][0]
    second = None
    for value, _count in peaks[1:]:
        if abs(value - first) >= 1.0:
            second = value
            break
    if second is None:
        return None
    period_us = first + second
    if period_us <= 0:
        return None
    return 1_000_000.0 / period_us


def bits_to_bytes(bits: list[int], lsb_first: bool) -> list[int]:
    out: list[int] = []
    for i in range(0, len(bits), 8):
        chunk = bits[i : i + 8]
        if len(chunk) != 8:
            break
        if lsb_first:
            value = sum(bit << j for j, bit in enumerate(chunk))
        else:
            value = 0
            for bit in chunk:
                value = (value << 1) | bit
        out.append(value)
    return out


def demodulate(path: Path, cfg: AppConfig) -> dict[str, Any]:
    edges, comments = parse_atk_csv(path, cfg.channel)
    d = cfg.demod
    bursts = split_bursts(
        edges,
        burst_split_us=float(d["burst_split_us"]),
        min_burst_us=float(d["min_burst_us"]),
    )
    if len(bursts) < 3:
        raise DecodeError(f"found only {len(bursts)} carrier bursts")

    leader_min = float(d["leader_min_us"])
    frame_gap = float(d["frame_gap_us"])
    one_threshold = float(d["one_threshold_us"])

    leader_indices = [i for i, b in enumerate(bursts) if b.duration_us >= leader_min]
    if not leader_indices:
        raise DecodeError("no leader burst found")

    frames: list[dict[str, Any]] = []
    used_until = -1
    for leader_index in leader_indices:
        if leader_index <= used_until:
            continue
        if leader_index + 1 >= len(bursts):
            continue

        leader = bursts[leader_index]
        leader_space_us = (bursts[leader_index + 1].start_s - leader.end_s) * 1_000_000.0
        bits: list[int] = []
        spaces_us: list[float] = []
        marks_us: list[float] = []
        j = leader_index + 1

        while j + 1 < len(bursts):
            # If we encounter another leader after an inter-frame gap, stop.
            if j != leader_index + 1 and bursts[j].duration_us >= leader_min:
                break
            space_us = (bursts[j + 1].start_s - bursts[j].end_s) * 1_000_000.0
            if space_us >= frame_gap:
                break
            marks_us.append(bursts[j].duration_us)
            spaces_us.append(space_us)
            bits.append(1 if space_us >= one_threshold else 0)
            j += 1

        # j is the trailer mark when the following gap ends the frame.
        trailer_us = bursts[j].duration_us if j < len(bursts) else None
        used_until = max(used_until, j)

        zero_spaces = [s for bit, s in zip(bits, spaces_us) if bit == 0]
        one_spaces = [s for bit, s in zip(bits, spaces_us) if bit == 1]
        lsb = bits_to_bytes(bits, lsb_first=True)
        msb = bits_to_bytes(bits, lsb_first=False)

        frames.append(
            {
                "leader_mark_us": round(leader.duration_us, 1),
                "leader_space_us": round(leader_space_us, 1),
                "bit_count": len(bits),
                "byte_count": len(bits) // 8,
                "trailing_bits": len(bits) % 8,
                "bits": "".join(str(x) for x in bits),
                "bytes_lsb": lsb,
                "bytes_lsb_hex": bytes_hex(lsb),
                "bytes_msb": msb,
                "bytes_msb_hex": bytes_hex(msb),
                "median_mark_us": round(median_or_none(marks_us) or 0.0, 1),
                "median_zero_space_us": round(median_or_none(zero_spaces) or 0.0, 1),
                "median_one_space_us": round(median_or_none(one_spaces) or 0.0, 1),
                "trailer_mark_us": round(trailer_us, 1) if trailer_us is not None else None,
            }
        )

    if not frames:
        raise DecodeError("leader found, but no decodable frame followed it")

    carrier_hz = estimate_carrier_hz(bursts)
    return {
        "source_format": "ATK-Logic edge CSV",
        "csv_metadata": comments,
        "edge_count": len(edges),
        "burst_count": len(bursts),
        "carrier_hz_estimate": round(carrier_hz, 1) if carrier_hz else None,
        "demod_parameters": {
            "burst_split_us": float(d["burst_split_us"]),
            "frame_gap_us": frame_gap,
            "leader_min_us": leader_min,
            "one_threshold_us": one_threshold,
            "min_burst_us": float(d["min_burst_us"]),
        },
        "frames": frames,
    }


def primary_frame(decoded: dict[str, Any]) -> dict[str, Any]:
    frames = decoded.get("frames", [])
    if not frames:
        raise DecodeError("decoded capture has no frames")
    return max(frames, key=lambda f: int(f.get("bit_count", 0)))


def print_decode(decoded: dict[str, Any]) -> None:
    carrier = decoded.get("carrier_hz_estimate")
    if carrier:
        print(f"Carrier estimate : {carrier / 1000.0:.2f} kHz")
    print(f"Edges / bursts   : {decoded.get('edge_count')} / {decoded.get('burst_count')}")
    for i, frame in enumerate(decoded.get("frames", []), 1):
        print(f"Frame {i}          : {frame['bit_count']} bits / {frame['byte_count']} full bytes")
        print(f"  leader          : {frame['leader_mark_us']:.1f} us mark, {frame['leader_space_us']:.1f} us space")
        print(
            "  data timings    : "
            f"mark~{frame['median_mark_us']:.1f} us, "
            f"0~{frame['median_zero_space_us']:.1f} us, "
            f"1~{frame['median_one_space_us']:.1f} us"
        )
        print(f"  LSB-first bytes : {frame['bytes_lsb_hex']}")
        if frame.get("trailing_bits"):
            print(f"  trailing bits   : {frame['trailing_bits']}")


# ---------------------------------------------------------------------------
# Persistent dataset
# ---------------------------------------------------------------------------


def capture_root(cfg: AppConfig) -> Path:
    return cfg.dataset_dir / "captures"


def iter_metadata(cfg: AppConfig) -> Iterable[tuple[Path, dict[str, Any]]]:
    root = capture_root(cfg)
    if not root.exists():
        return
    for path in sorted(root.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("schema_version") == SCHEMA_VERSION and "state" in data:
                yield path, data
        except (OSError, json.JSONDecodeError):
            continue


def successful_captures(cfg: AppConfig) -> list[tuple[Path, dict[str, Any]]]:
    return [(p, m) for p, m in iter_metadata(cfg) if m.get("status") == "ok"]


def successful_by_state(cfg: AppConfig) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    result: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for path, meta in successful_captures(cfg):
        sid = meta.get("state_id") or state_id(meta["state"])
        result[sid].append((path, meta))
    for items in result.values():
        items.sort(key=lambda item: item[1].get("captured_at", ""))
    return result


def all_hashes(cfg: AppConfig) -> set[str]:
    return {m.get("source_sha256") for _, m in iter_metadata(cfg) if m.get("source_sha256")}


def next_pending(cfg: AppConfig) -> tuple[PlannedState, int] | None:
    done = successful_by_state(cfg)
    for planned in build_plan(cfg):
        have = len(done.get(planned.sid, []))
        if have < planned.required_count:
            return planned, have + 1
    return None


def archive_capture(
    cfg: AppConfig,
    source: Path,
    planned: PlannedState,
    sample_index: int,
    decoded: dict[str, Any] | None,
    error: str | None,
) -> tuple[Path, Path, dict[str, Any]]:
    digest = sha256_file(source)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    state_dir = capture_root(cfg) / f"{planned.sid}__{state_label(planned.state)}"
    state_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sample-{sample_index:02d}__{timestamp}__{digest[:8]}"
    csv_dest = state_dir / f"{stem}.csv"
    json_dest = state_dir / f"{stem}.json"

    # Preserve exactly what ATK wrote. shutil.move handles cross-filesystem moves.
    shutil.move(str(source), str(csv_dest))

    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if decoded is not None else "decode_error",
        "captured_at": now_iso(),
        "state_id": planned.sid,
        "state": planned.state,
        "sample_index": sample_index,
        "experiments": list(planned.experiments),
        "source_sha256": digest,
        "raw_csv": csv_dest.name,
        "decode": decoded,
    }
    if error:
        meta["error"] = error
    atomic_json_write(json_dest, meta)
    return csv_dest, json_dest, meta


def wait_until_stable(path: Path, stable_for_s: float, poll_s: float) -> bool:
    deadline_same = time.monotonic() + stable_for_s
    previous: tuple[int, int] | None = None
    while path.exists():
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            time.sleep(poll_s)
            continue
        if current != previous:
            previous = current
            deadline_same = time.monotonic() + stable_for_s
        elif time.monotonic() >= deadline_same:
            return True
        time.sleep(poll_s)
    return False


def print_requested_state(planned: PlannedState, sample_index: int) -> None:
    print("\n" + "=" * 68)
    print(f"CAPTURE {sample_index}/{planned.required_count} for this state")
    if planned.experiments:
        print(f"Used by: {', '.join(planned.experiments)}")
    print("\nSet the remote EXACTLY to:")
    width = max(len(k) for k in planned.state)
    for key, value in planned.state.items():
        label = "TEMPERATURE" if key == "temperature_c" else key.replace("_", " ").upper()
        display = f"{value} °C" if key == "temperature_c" else str(value).upper()
        print(f"  {label:<{width + 3}} {display}")
    print()


def cmd_capture(cfg: AppConfig, _args: argparse.Namespace) -> int:
    cfg.inbox_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.dataset_dir.mkdir(parents=True, exist_ok=True)

    # Any file already sitting in the inbox when we start is deliberately not
    # consumed: we cannot know which requested state it represents. Capture only
    # a fresh save/overwrite after the prompt below.
    start_hash = sha256_file(cfg.inbox_file) if cfg.inbox_file.exists() else None
    seen_hashes = all_hashes(cfg)
    if start_hash:
        seen_hashes.add(start_hash)

    print(f"Watching ATK export path: {cfg.inbox_file}")
    print(f"Dataset directory       : {cfg.dataset_dir}")
    print("Save/overwrite that exact file after each requested state. Ctrl+C stops safely.")

    try:
        while True:
            pending = next_pending(cfg)
            if pending is None:
                print("\nCapture plan complete.")
                print("Run: python acir.py analyse")
                return 0
            planned, sample_index = pending
            print_requested_state(planned, sample_index)
            print(f"Waiting for a NEW save of: {cfg.inbox_file.name}")

            while True:
                if not cfg.inbox_file.exists():
                    time.sleep(cfg.poll_interval_s)
                    continue
                if not wait_until_stable(cfg.inbox_file, cfg.stable_for_s, cfg.poll_interval_s):
                    continue
                try:
                    digest = sha256_file(cfg.inbox_file)
                except OSError:
                    time.sleep(cfg.poll_interval_s)
                    continue
                if digest in seen_hashes:
                    time.sleep(cfg.poll_interval_s)
                    continue
                seen_hashes.add(digest)
                break

            print("Detected completed ATK export; demodulating...")
            try:
                decoded = demodulate(cfg.inbox_file, cfg)
                csv_dest, _json_dest, _meta = archive_capture(
                    cfg, cfg.inbox_file, planned, sample_index, decoded, None
                )
                frame = primary_frame(decoded)
                carrier = decoded.get("carrier_hz_estimate")
                print(f"  OK: {frame['bit_count']} bits -> {frame['bytes_lsb_hex']}")
                if carrier:
                    print(f"  carrier estimate: {carrier / 1000.0:.2f} kHz")
                print(f"  archived raw CSV: {csv_dest}")
            except Exception as exc:
                # Even bad captures are preserved with the requested setting so
                # nothing silently disappears. The failed sample does not count
                # as complete, therefore the same requested state is shown again.
                error = f"{type(exc).__name__}: {exc}"
                try:
                    csv_dest, _json_dest, _meta = archive_capture(
                        cfg, cfg.inbox_file, planned, sample_index, None, error
                    )
                    print(f"  DECODE FAILED: {error}")
                    print(f"  raw CSV still archived: {csv_dest}")
                    print("  This state will be requested again.")
                except Exception as archive_exc:
                    print(f"  DECODE FAILED: {error}", file=sys.stderr)
                    print(f"  ALSO FAILED TO ARCHIVE INPUT: {archive_exc}", file=sys.stderr)
                    return 2
    except KeyboardInterrupt:
        print("\nStopped. Existing archived captures remain valid; rerun capture to resume.")
        return 130


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def packet_from_meta(meta: dict[str, Any]) -> list[int] | None:
    decoded = meta.get("decode")
    if not decoded:
        return None
    try:
        frame = primary_frame(decoded)
    except DecodeError:
        return None
    values = frame.get("bytes_lsb")
    return list(values) if isinstance(values, list) else None


def bits_from_packet(packet: list[int]) -> list[int]:
    out: list[int] = []
    for b in packet:
        out.extend((b >> i) & 1 for i in range(8))
    return out


def differing_byte_indices(a: list[int], b: list[int]) -> list[int]:
    n = max(len(a), len(b))
    return [i for i in range(n) if i >= len(a) or i >= len(b) or a[i] != b[i]]


def infer_numeric_relations(samples: list[tuple[float, list[int]]]) -> list[str]:
    if len(samples) < 3:
        return []
    lengths = {len(p) for _, p in samples}
    if len(lengths) != 1:
        return []
    n = next(iter(lengths))
    relations: list[str] = []

    def constant_offset(parts: list[tuple[float, int]]) -> float | None:
        offsets = [part - x for x, part in parts]
        if max(offsets) - min(offsets) < 1e-9:
            return offsets[0]
        return None

    for i in range(n):
        whole = [(x, p[i]) for x, p in samples]
        high = [(x, p[i] >> 4) for x, p in samples]
        low = [(x, p[i] & 0x0F) for x, p in samples]
        for label, parts in ((f"byte[{i}]", whole), (f"byte[{i}].high_nibble", high), (f"byte[{i}].low_nibble", low)):
            if len({v for _, v in parts}) < 2:
                continue
            offset = constant_offset(parts)
            if offset is not None and float(offset).is_integer():
                off = int(offset)
                if off == 0:
                    relations.append(f"{label} = value")
                elif off > 0:
                    relations.append(f"{label} = value + {off}")
                else:
                    relations.append(f"{label} = value - {abs(off)}")
    return relations


def zero_sum_suffix_candidates(packets: list[list[int]]) -> list[int]:
    if len(packets) < 2 or len({len(p) for p in packets}) != 1:
        return []
    n = len(packets[0])
    if len({tuple(p) for p in packets}) < 2:
        return []
    result = []
    for start in range(n - 1):
        if all((sum(p[start:]) & 0xFF) == 0 for p in packets):
            result.append(start)
    return result


def analyse_data(cfg: AppConfig) -> dict[str, Any]:
    captures = successful_captures(cfg)
    by_state = successful_by_state(cfg)
    plan = build_plan(cfg)

    capture_rows: list[dict[str, Any]] = []
    packets: list[list[int]] = []
    for path, meta in captures:
        packet = packet_from_meta(meta)
        if packet is not None:
            packets.append(packet)
        capture_rows.append(
            {
                "metadata": relpath_or_abs(path, cfg.dataset_dir),
                "captured_at": meta.get("captured_at"),
                "state_id": meta.get("state_id"),
                "state": meta.get("state"),
                "packet_lsb_hex": bytes_hex(packet) if packet is not None else None,
                "byte_count": len(packet) if packet is not None else None,
            }
        )

    length_groups: dict[int, list[list[int]]] = defaultdict(list)
    for packet in packets:
        length_groups[len(packet)].append(packet)

    frame_lengths: dict[str, Any] = {}
    for length, group in sorted(length_groups.items()):
        varying: list[int] = []
        constants: dict[str, str] = {}
        if group:
            for i in range(length):
                vals = {p[i] for p in group}
                if len(vals) == 1:
                    constants[str(i)] = f"{next(iter(vals)):02X}"
                else:
                    varying.append(i)
        frame_lengths[str(length)] = {
            "capture_count": len(group),
            "unique_packet_count": len({tuple(p) for p in group}),
            "constant_bytes": constants,
            "varying_byte_indices": varying,
            "zero_sum_suffix_starts": zero_sum_suffix_candidates(group),
        }

    experiments_out: list[dict[str, Any]] = []
    for index, exp in enumerate(cfg.experiments):
        if exp.get("enabled", True) is False:
            continue
        name = str(exp.get("name") or f"experiment_{index + 1}")
        states = experiment_states(cfg, exp)
        field = exp.get("field")
        entries: list[dict[str, Any]] = []
        reference_packet: list[int] | None = None
        numeric_samples: list[tuple[float, list[int]]] = []

        # Prefer the exact configured baseline as the diff reference if this
        # experiment contains it, otherwise use its first successfully captured state.
        base_sid = state_id(cfg.baseline)
        if base_sid in by_state:
            reference_packet = packet_from_meta(by_state[base_sid][0][1])

        for state in states:
            sid = state_id(state)
            captures_for_state = by_state.get(sid, [])
            packet = packet_from_meta(captures_for_state[0][1]) if captures_for_state else None
            if reference_packet is None and packet is not None:
                reference_packet = packet
            value = state.get(field) if field else None
            entry = {
                "state_id": sid,
                "state": state,
                "captured_samples": len(captures_for_state),
                "packet_lsb_hex": bytes_hex(packet) if packet is not None else None,
                "byte_count": len(packet) if packet is not None else None,
                "diff_byte_indices_vs_reference": (
                    differing_byte_indices(reference_packet, packet)
                    if reference_packet is not None and packet is not None
                    else None
                ),
            }
            if field:
                entry["field"] = field
                entry["value"] = value
                if isinstance(value, (int, float)) and packet is not None:
                    numeric_samples.append((float(value), packet))
            entries.append(entry)

        # Bit positions that vary within this experiment, for packets of one length.
        present_packets = [
            packet_from_meta(by_state[state_id(s)][0][1])
            for s in states
            if state_id(s) in by_state
        ]
        present_packets = [p for p in present_packets if p is not None]
        varying_bits: list[int] = []
        if present_packets and len({len(p) for p in present_packets}) == 1:
            bit_rows = [bits_from_packet(p) for p in present_packets]
            for bit_index in range(len(bit_rows[0])):
                if len({row[bit_index] for row in bit_rows}) > 1:
                    varying_bits.append(bit_index)

        experiments_out.append(
            {
                "name": name,
                "field": field,
                "entries": entries,
                "varying_bit_indices_lsb": varying_bits,
                "numeric_relations": infer_numeric_relations(numeric_samples) if field else [],
            }
        )

    completed_states = {
        sid: len(items) for sid, items in by_state.items()
    }
    plan_status = [
        {
            "state_id": p.sid,
            "state": p.state,
            "required_count": p.required_count,
            "captured_count": completed_states.get(p.sid, 0),
            "experiments": list(p.experiments),
        }
        for p in plan
    ]

    return {
        "generated_at": now_iso(),
        "successful_capture_count": len(captures),
        "plan": plan_status,
        "frame_lengths": frame_lengths,
        "experiments": experiments_out,
        "captures": capture_rows,
    }


def print_analysis(analysis: dict[str, Any]) -> None:
    print(f"Successful captures: {analysis['successful_capture_count']}")
    print("\nFrame lengths:")
    for length, info in analysis["frame_lengths"].items():
        print(
            f"  {length:>3} bytes: {info['capture_count']} captures, "
            f"{info['unique_packet_count']} unique packets"
        )
        if info["varying_byte_indices"]:
            print(f"      varying bytes: {info['varying_byte_indices']}")
        if info["zero_sum_suffix_starts"]:
            starts = ", ".join(str(x) for x in info["zero_sum_suffix_starts"])
            print(f"      checksum clue: sum(bytes[start:]) == 0 mod 256 for start={starts}")

    for exp in analysis["experiments"]:
        print(f"\n[{exp['name']}]")
        entries = exp["entries"]
        for entry in entries:
            if exp.get("field"):
                label = f"{exp['field']}={entry.get('value')}"
            else:
                label = state_label(entry["state"])
            if entry["packet_lsb_hex"] is None:
                print(f"  {label:<28} MISSING")
            else:
                diff = entry.get("diff_byte_indices_vs_reference")
                diff_text = f"  diff bytes {diff}" if diff else ""
                print(f"  {label:<28} {entry['packet_lsb_hex']}{diff_text}")
        if exp.get("varying_bit_indices_lsb"):
            print(f"  varying LSB-first bit indices: {exp['varying_bit_indices_lsb']}")
        for relation in exp.get("numeric_relations", []):
            print(f"  inferred: {relation}")


def cmd_analyse(cfg: AppConfig, _args: argparse.Namespace) -> int:
    analysis = analyse_data(cfg)
    output = cfg.dataset_dir / "analysis.json"
    atomic_json_write(output, analysis)
    print_analysis(analysis)
    print(f"\nMachine-readable analysis: {output}")
    return 0


# ---------------------------------------------------------------------------
# Other commands
# ---------------------------------------------------------------------------


def cmd_status(cfg: AppConfig, _args: argparse.Namespace) -> int:
    plan = build_plan(cfg)
    done = successful_by_state(cfg)
    total = sum(p.required_count for p in plan)
    complete = sum(min(len(done.get(p.sid, [])), p.required_count) for p in plan)
    print(f"Capture progress: {complete}/{total}")
    for p in plan:
        have = len(done.get(p.sid, []))
        mark = "OK" if have >= p.required_count else "--"
        print(f"[{mark}] {have}/{p.required_count}  {state_label(p.state)}")
    return 0


def cmd_plan(cfg: AppConfig, _args: argparse.Namespace) -> int:
    plan = build_plan(cfg)
    print(f"Unique physical states: {len(plan)}")
    print(f"Total required captures: {sum(p.required_count for p in plan)}")
    for i, p in enumerate(plan, 1):
        suffix = f" x{p.required_count}" if p.required_count > 1 else ""
        print(f"{i:02d}. {state_label(p.state)}{suffix}")
        print(f"    experiments: {', '.join(p.experiments)}")
    return 0


def cmd_decode(cfg: AppConfig, args: argparse.Namespace) -> int:
    path = Path(args.csv).expanduser().resolve()
    decoded = demodulate(path, cfg)
    print_decode(decoded)
    if args.json:
        atomic_json_write(Path(args.json).expanduser().resolve(), decoded)
    return 0


def cmd_reprocess(cfg: AppConfig, _args: argparse.Namespace) -> int:
    count = 0
    failed = 0
    for meta_path, meta in list(iter_metadata(cfg)):
        raw_name = meta.get("raw_csv")
        if not raw_name:
            continue
        raw_path = meta_path.parent / raw_name
        if not raw_path.exists():
            print(f"MISSING RAW: {raw_path}")
            failed += 1
            continue
        try:
            decoded = demodulate(raw_path, cfg)
            meta["decode"] = decoded
            meta["status"] = "ok"
            meta.pop("error", None)
            meta["reprocessed_at"] = now_iso()
            atomic_json_write(meta_path, meta)
            count += 1
        except Exception as exc:
            meta["decode"] = None
            meta["status"] = "decode_error"
            meta["error"] = f"{type(exc).__name__}: {exc}"
            meta["reprocessed_at"] = now_iso()
            atomic_json_write(meta_path, meta)
            failed += 1
    print(f"Reprocessed: {count} OK, {failed} failed")
    return 0 if failed == 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guided AC IR capture + ATK edge CSV demodulator")
    parser.add_argument(
        "--config",
        default="acir_config.json",
        help="config JSON path (default: ./acir_config.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capture", help="guided capture loop; watches the configured ATK export file")
    sub.add_parser("status", help="show capture-plan progress from the persistent dataset")
    sub.add_parser("plan", help="show the deduplicated capture plan")
    sub.add_parser("analyse", help="compare all captured states and write analysis.json")
    sub.add_parser("reprocess", help="redemodulate every archived raw CSV using current demod settings")

    p_decode = sub.add_parser("decode", help="demodulate one ATK CSV without adding it to the dataset")
    p_decode.add_argument("csv")
    p_decode.add_argument("--json", help="optional path to write decoded JSON")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        cfg = load_config(Path(args.config))
        dispatch = {
            "capture": cmd_capture,
            "status": cmd_status,
            "plan": cmd_plan,
            "analyse": cmd_analyse,
            "reprocess": cmd_reprocess,
            "decode": cmd_decode,
        }
        return dispatch[args.command](cfg, args)
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc.filename}", file=sys.stderr)
        return 2
    except (ValueError, DecodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
