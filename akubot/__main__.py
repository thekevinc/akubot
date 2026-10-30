from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .config import ConfigurationError, Persona, Settings, local_model_url
from .importer import discover, prepare, read_reviewed
from .storage import Store
from .channel_policy import ChannelPolicy


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="akubot: private, local Discord personality bot")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Connect the configured bot to Discord")
    commands.add_parser("check", help="Check the local model and personality database")
    inspect = commands.add_parser("inspect-export", help="List channel IDs in an unzipped official Discord export")
    inspect.add_argument("directory", type=Path)
    prep = commands.add_parser("prepare-export", help="Create redacted candidates for your manual review")
    prep.add_argument("directory", type=Path)
    prep.add_argument("--channel-id", action="append", required=True)
    prep.add_argument("--owner-id", required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--channel-policy", type=Path, default=Path("/config/channel-policy.json"))
    load = commands.add_parser("load-examples", help="Load only manually reviewed, shareable examples")
    load.add_argument("file", type=Path)
    load.add_argument("--channel-policy", type=Path, default=Path("/config/channel-policy.json"))
    load.add_argument("--reviewed", action="store_true", required=True,
                      help="Confirm every example is safe for everyone who can invoke the bot")
    commands.add_parser("clear-examples", help="Remove all stored style examples")
    return result


async def check_model() -> int:
    import aiohttp
    from .model import ModelError, Ollama

    Persona.load(Path(os.getenv("PERSONA_FILE", "config/persona.json")))
    model_name = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    url = local_model_url(os.getenv("OLLAMA_URL", "http://ollama:11434"))
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            ready = await Ollama(session, url, model_name).available()
        except ModelError:
            print("The local model service is not reachable. Start the configured Ollama service first.")
            return 1
    if not ready:
        print("The configured model is not downloaded. Run the model-download setup step first.")
        return 1
    store = Store(Path(os.getenv("DATA_DIR", "data")) / "aku.sqlite3")
    try:
        print(f"Local model ready. Reviewed examples: {store.count()}.")
        if not store.count():
            print("akubot can reply, but it has not been personalized with your message examples yet.")
    finally:
        store.close()
    return 0


async def run_bot() -> int:
    import discord
    from .bot import AkuClient

    settings = Settings.from_env()
    token = settings.read_token()
    persona = Persona.load(settings.persona_file)
    store = Store(settings.data_dir / "aku.sqlite3")
    try:
        async with AkuClient(settings, store, persona) as client:
            await client.start(token)
    except discord.LoginFailure:
        print("Discord rejected the bot token. Reset it in the Developer Portal and rerun scripts/setup.py.")
        return 1
    except discord.Forbidden:
        print("Discord denied access. Confirm the server IDs and install the bot using the setup invitation link.")
        return 1
    finally:
        store.close()
    return 0


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Library errors can include response bodies or event context.
    logging.getLogger("discord").setLevel(logging.CRITICAL)
    args = parser().parse_args()
    try:
        if args.command == "run":
            return asyncio.run(run_bot())
        if args.command == "check":
            return asyncio.run(check_model())
        if args.command == "inspect-export":
            for channel in discover(args.directory):
                print(channel.channel_id)
            print("Only channel IDs are shown. Use the export's index.json locally to identify channels.")
        elif args.command == "prepare-export":
            kept, skipped = prepare(args.directory, args.output, set(args.channel_id), args.owner_id, ChannelPolicy.load(args.channel_policy))
            print(f"Prepared {kept} candidates; skipped {skipped} records. Nothing was added to the bot.")
            print("Manually remove private facts, names, quotes, and anything unsuitable for every enabled server.")
            print("Save the approved lines in a separate JSONL file before using load-examples --reviewed.")
        else:
            texts = read_reviewed(args.file, ChannelPolicy.load(args.channel_policy)) if args.command == "load-examples" else None
            store = Store(Path(os.getenv("DATA_DIR", "data")) / "aku.sqlite3")
            try:
                if texts is not None:
                    print(f"Added {store.add_examples(texts)} reviewed examples. Total: {store.count()}.")
                else:
                    store.clear_examples()
                    print("All stored style examples were removed. Backups must be removed separately.")
            finally:
                store.close()
        return 0
    except ConfigurationError as error:
        print(str(error))
        return 1
    except (ValueError, OSError):
        # Never print input records, file contents, credentials, or raw errors.
        print("Setup could not complete. Check the configuration, input format, selected IDs, and file permissions.")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("akubot stopped unexpectedly. No credentials or message content were logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

