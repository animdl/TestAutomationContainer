"""Guarded latency customization for the pinned PTS FIO launcher."""

import hashlib
import os
from pathlib import Path


CUSTOMIZATION = "benchmark-fio-latency-v1"
ORIGINAL = "clat_percentiles=0\ndisable_lat=1\ndisable_clat=1\ndisable_slat=1\n"
LATENCY = "clat_percentiles=1\npercentile_list=50:95:99\ndisable_lat=0\ndisable_clat=0\ndisable_slat=0\n"


def configure_launcher(launcher):
    text = launcher.read_text()
    if text.count(ORIGINAL) != 1 or CUSTOMIZATION in text:
        raise ValueError("FIO launcher does not contain exactly one expected latency block")
    text = text.replace(ORIGINAL, LATENCY)
    first, rest = text.split("\n", 1)
    with launcher.open("w", newline="\n") as stream:
        stream.write(first + "\n# " + CUSTOMIZATION + "\n" + rest)


def verify_launcher(launcher):
    data = launcher.read_bytes()
    text = data.decode()
    if text.count(LATENCY) != 1 or text.count("# " + CUSTOMIZATION + "\n") != 1:
        raise ValueError("FIO launcher latency customization is missing or changed")
    return f"{CUSTOMIZATION} sha256={hashlib.sha256(data).hexdigest()}"


def main():
    manifest = Path("/usr/local/share/benchmark/pts-profiles")
    profiles = [p for p in manifest.read_text().split() if p.startswith("pts/fio-")]
    if profiles != ["pts/fio-1.15.0"]:
        raise ValueError("latency customization requires exactly pts/fio-1.15.0")
    root = Path(os.environ.get("PTS_USER_PATH_OVERRIDE", "/var/lib/phoronix-test-suite"))
    launcher = root / "installed-tests" / profiles[0] / "fio-run"
    configure_launcher(launcher)
    print(verify_launcher(launcher))


if __name__ == "__main__":
    main()
