"""Guarded size fix for cuda_memtest test 10 (memory stress) at the pinned commit.

Upstream passes the allocation size to both test 10 kernels as a 32-bit int, so
allocations of 2 GiB or more are truncated modulo 2^32 and the test covers only
part of the allocation (for example 2,836 of 15,124 MiB), while the reported
bandwidth still counts the whole allocation. The per-block byte offset is int
arithmetic too. This widens both to unsigned long; nothing else changes.

Usage: python3 cuda_memtest_patch.py <source dir>
"""

import sys
from pathlib import Path

CUSTOMIZATION = "benchmark-cuda-memtest-test10-size-v1"
# (original, replacement, expected occurrences)
REPLACEMENTS = (
    ("__global__ void test10_kernel_write(char* ptr, int memsize, TYPE p1)",
     "__global__ void test10_kernel_write(char* ptr, unsigned long memsize, TYPE p1)", 1),
    ("__global__ void test10_kernel_readwrite(char* ptr, int memsize, TYPE p1, TYPE p2,",
     "__global__ void test10_kernel_readwrite(char* ptr, unsigned long memsize, TYPE p1, TYPE p2,", 1),
    ("    int avenumber = memsize/(gridDim.x*gridDim.y);\n",
     "    unsigned long avenumber = memsize/(gridDim.x*gridDim.y);\n", 2),
)
ANCHOR = "#define TYPE unsigned long\n"


def patch(source):
    text = source.read_text()
    if CUSTOMIZATION in text or text.count(ANCHOR) != 1:
        raise ValueError("cuda_memtest tests.cpp is already patched or its test 10 block changed")
    for original, replacement, count in REPLACEMENTS:
        if text.count(original) != count:
            raise ValueError(f"expected {count} occurrence(s) of: {original.strip()}")
        text = text.replace(original, replacement)
    if "int memsize" in text:
        raise ValueError("an unpatched 32-bit memsize parameter remains")
    text = text.replace(ANCHOR, f"// {CUSTOMIZATION}\n{ANCHOR}")
    with source.open("w", newline="\n") as stream:
        stream.write(text)


def main():
    patch(Path(sys.argv[1]) / "tests.cpp")
    print(CUSTOMIZATION)


if __name__ == "__main__":
    main()
