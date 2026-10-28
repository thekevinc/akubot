"""Interactive setup. Run locally; never paste a bot token into a chat."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import shutil
from urllib.parse import urlencode


PROJECT = Path(__file__).resolve().parent.parent


def default_private_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Akubot"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "akubot"


def validate_private_dir(path: Path, project: Path = PROJECT) -> Path:
    path = path.expanduser().resolve()
    project = project.resolve()
    if path == project or project in path.parents or any(part.lower().startswith("onedrive") for part in path.parts):
        raise ValueError("Choose private storage outside the repository and OneDrive.")
    if any(char in path.as_posix() for char in "\r\n'\x00"):
        raise ValueError("Choose a folder path without quotes or line breaks.")
    return path


def prompt_ids(label: str, multiple: bool = False, optional: bool = False) -> str:
    while True:
        value = input(label).strip()
        if optional and not value:
            return ""
        parts = [part.strip() for part in value.split(",")]
        if all(part.isdecimal() and int(part) > 0 for part in parts) and (multiple or len(parts) == 1):
            return ",".join(dict.fromkeys(parts))
        print("Enter a numeric Discord ID" + (" or comma-separated IDs." if multiple else "."))


def initialize(private: Path, owner: str, guilds: str, channels: str, token: str, project: Path = PROJECT) -> None:
    private = validate_private_dir(private, project)
    for folder in (private, *(private / name for name in ("secrets", "data", "models", "imports", "review", "config"))):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    token_file = private / "secrets" / "discord_token.txt"
    if token:
        if not 30 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise ValueError("The token format does not look right. Copy the bot token from the Developer Portal.")
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token)
        token_file.chmod(0o600)
    elif not token_file.is_file():
        raise ValueError("A bot token is required for first-time setup.")
    persona = private / "config" / "persona.json"
    if not persona.exists():
        shutil.copyfile(project / "config" / "persona.json", persona)
        persona.chmod(0o600)
    uid = os.getuid() if hasattr(os, "getuid") and os.getuid() != 0 else 10001
    gid = os.getgid() if hasattr(os, "getgid") and os.getgid() != 0 else 10001
    config = (
        "# Local machine configuration. No bot token is stored here.\n"
        f"AKU_PRIVATE_DIR='{private.as_posix()}'\nAKU_UID={uid}\nAKU_GID={gid}\n"
        f"DISCORD_OWNER_ID={owner}\nALLOWED_GUILD_IDS={guilds}\nALLOWED_CHANNEL_IDS={channels}\n"
        "OLLAMA_MODEL=llama3.2:3b\nOLLAMA_IMAGE=ollama/ollama:0.40.12@sha256:20d1bbfbabe1c97ba4b16301f08a20d8484bcf77047742a3cb71c613d8e12d0b\n"
    )
    env = project / ".env"
    descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(config)
    env.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create local Akubot settings without storing secrets in the repository")
    parser.add_argument("--private-dir", type=Path, default=default_private_dir())
    args = parser.parse_args()
    try:
        os.umask(0o077)
        private = validate_private_dir(args.private_dir)
        print("Create a bot application at https://discord.com/developers/applications first.")
        print("Enable Developer Mode in Discord to copy your own user ID and your server IDs.")
        print(f"Private storage: {private}")
        application = prompt_ids("Application ID (Developer Portal > General Information): ")
        owner = prompt_ids("Your personal Discord user ID: ")
        guilds = prompt_ids("Server IDs to enable (comma separated): ", multiple=True)
        channels = prompt_ids("Optional channel IDs (empty allows visible channels in those servers): ", multiple=True, optional=True)
        keep = (private / "secrets" / "discord_token.txt").is_file()
        token = getpass.getpass("Bot token (hidden" + ("; empty keeps the existing token): " if keep else "): ")).strip()
        initialize(private, owner, guilds, channels, token)
        query = urlencode({"client_id": application, "scope": "bot applications.commands", "permissions": "274877975552"})
        print("Settings saved. Install the bot in each approved server using:")
        print("https://discord.com/oauth2/authorize?" + query)
        print("Next: docker compose run --rm model-download")
        print("Then: docker compose up -d --build")
        print("See README.md for permissions, personalization, and the setup check.")
        return 0
    except (ValueError, OSError) as error:
        if isinstance(error, ValueError):
            print(str(error))
        else:
            print("Setup could not write the local files. Check the selected folder's permissions.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

