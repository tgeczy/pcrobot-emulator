"""Assemble the distributable .nvda-addon from the three ingredients.

The repository ships only this project's own code. To build the add-on:

1. Engine files (c) NIKOL -> addon/synthDrivers/_pcrobot_engine/bin/
   (SBDD.EXE, ROBOTVOX.EXE, ROBOTVOX.OV, ROBOTVOX.CF, AHA.RAW)
   from https://archive.org/details/pcrobot-archive
2. DOSBox-X -> addon/synthDrivers/_pcrobot_engine/dosbox/
   (dosbox-x.exe and its COPYING.txt) from https://dosbox-x.com/
3. Run:  python tools/build_addon.py
   -> pcrobot-<version>.nvda-addon in the repository root.
"""
import configparser
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, 'addon')

ENGINE = ('SBDD.EXE', 'ROBOTVOX.EXE', 'ROBOTVOX.OV', 'ROBOTVOX.CF',
          'AHA.RAW', 'SPEAK.COM')
DOSBOX = ('dosbox-x.exe', 'COPYING.txt')

missing = []
for name in ENGINE:
    p = os.path.join(ADDON, 'synthDrivers', '_pcrobot_engine', 'bin', name)
    if not os.path.isfile(p):
        missing.append(p)
for name in DOSBOX:
    p = os.path.join(ADDON, 'synthDrivers', '_pcrobot_engine', 'dosbox', name)
    if not os.path.isfile(p):
        missing.append(p)
if missing:
    print('Missing ingredients (see tools/build_addon.py docstring):')
    for p in missing:
        print('  ' + os.path.relpath(p, ROOT))
    sys.exit(1)

cp = configparser.ConfigParser()
with open(os.path.join(ADDON, 'manifest.ini'), encoding='utf-8') as f:
    cp.read_string('[addon]\n' + f.read())
version = cp['addon']['version'].strip().strip('"')

out = os.path.join(ROOT, 'pcrobot-%s.nvda-addon' % version)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for dirpath, dirnames, filenames in os.walk(ADDON):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for name in filenames:
            full = os.path.join(dirpath, name)
            z.write(full, os.path.relpath(full, ADDON))
print('built %s (%.1f MB)' % (out, os.path.getsize(out) / 1e6))
