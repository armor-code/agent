#!/bin/sh
# Pass all arguments to the Python script and redirect output python
/usr/src/venv/bin/python3 -W ignore worker.py "$@" > /dev/null 2> /tmp/armorcode/console.log
