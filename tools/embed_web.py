from pathlib import Path
import argparse
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
content = '#pragma once\n#include <Arduino.h>\nstatic const char kWebPage[] PROGMEM = R"INERTIAWEB(' + (root/'web/index.html').read_text(encoding='utf-8') + ')INERTIAWEB";\n'
target = root/'src/web_page.h'
if args.check:
    if target.read_text(encoding='utf-8') != content:
        raise SystemExit('Embedded UI differs from web/index.html')
else:
    target.write_text(content, encoding='utf-8')
