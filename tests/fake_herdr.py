#!/usr/bin/env python3
"""Stand-in for the herdr CLI in tests: appends its arguments to $FAKE_HERDR_LOG."""

import json
import os
import sys

with open(os.environ["FAKE_HERDR_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("FAKE_HERDR_EXIT", "0")))
