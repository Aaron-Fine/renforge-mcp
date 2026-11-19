from __future__ import annotations

from typing import Final

PROTOCOL_NAME: Final = "renforge-editor"
PROTOCOL_VERSION: Final = 1

AUTH_FRAME_MAX_BYTES: Final = 4 * 1024
COMMAND_FRAME_MAX_BYTES: Final = 1024 * 1024
MAX_STRING_BYTES: Final = 4 * 1024
MAX_PATH_BYTES: Final = 1024
MAX_INTENTS: Final = 256
MAX_DIAGNOSTICS_BYTES: Final = 64 * 1024

TRANSACTION_DIRNAME: Final = "editor-transactions"
# The project's own RenForge folder. RenForge state lives in the state folder;
# this one only holds the owner's policy.json, and editor exchange files while
# a save crosses filesystems (PROJECT_EXCHANGE_DIRNAME inside it).
RENFORGE_DIRNAME: Final = ".renforge"
PROJECT_EXCHANGE_DIRNAME: Final = "editor-exchange"

COMMIT_STATES: Final = {
    "staged",
    "publishing",
    "published",
    "committed",
    "rolled_back",
    "rollback_conflict",
    "failed",
}

