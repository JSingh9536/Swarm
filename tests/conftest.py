"""Shared test setup: the suite must never probe or change the real machine.

Load awareness reads the GPU and process list and re-prioritises the Ollama server, and the usage ledger writes
under the user's home folder. Both are switched off for every test; tests that cover them call the pure functions
directly or point the ledger at a temp file.
"""

import os

os.environ["SWARM_LOAD_AWARE"] = "0"
