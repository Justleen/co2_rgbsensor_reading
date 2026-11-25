#!/usr/bin/env python3
"""Generate co2watch/page.h (PROGMEM string) from co2watch/page.html.

The firmware and the emulator serve the same page.html. Run this after editing
the page; flash-ota.sh runs it automatically.
"""
from pathlib import Path

d = Path(__file__).resolve().parent / "co2watch"
html = (d / "page.html").read_text().rstrip("\n")
if ")HTML\"" in html:
    raise SystemExit("page.html must not contain )HTML\"")
out = (
    "// GENERATED from page.html by firmware/gen_page.py. Do not edit.\n"
    "#pragma once\n"
    f'static const char PAGE[] PROGMEM = R"HTML({html})HTML";\n'
)
target = d / "page.h"
if not target.exists() or target.read_text() != out:
    target.write_text(out)
    print(f"wrote {target}")
