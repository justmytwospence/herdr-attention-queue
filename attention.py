#!/usr/bin/env python3
"""herdr-attention-queue plugin entrypoint.

herdr runs this from the plugin root (see herdr-plugin.toml):

    python3 -B attention.py startup | event | reseed | action <id>
"""

import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from attention_queue.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
