# Local writing-style training

This directory contains reusable training infrastructure for WSL. It runs a small LoRA experiment on exact outgoing-message strings from an official Discord archive. Source code belongs in this repository. Archives, sampled datasets, metadata, adapters, merged weights, conversion tools, reports, and model caches belong in private WSL storage, normally `~/.local/share/akubot/training`.

The owner explicitly authorized this trial to include all other server channels without verifying NSFW metadata. Secret remains excluded by numeric server ID and name. DMs are excluded. This exception applies to this training pipeline only; the regular reviewed-example importer retains its stricter NSFW policy.

## What the model learns

The dataset reader parses each message record and extracts its `Contents` string verbatim. URLs, whitespace, emojis, mentions, and any code or JSON actually posted inside a message remain in that string. Empty messages and duplicate strings are omitted. The surrounding JSON payload, attachment field, IDs, timestamps, and server/channel records are not model targets. Message ID, timestamp, and source server/channel are retained separately in private provenance.

Each training example uses a fixed generic request for one casual message, with the original message as the assistant response. Only response tokens contribute to the training loss. This is a style experiment, not reconstructed question/answer training. The archive lacks other people's messages. It cannot teach the model which reply the owner gave to a particular question.

Messages are kept intact in the dataset. The trainer currently uses a 512-token window, including its instruction prefix, so longer messages are truncated during tokenization. Validation is split by channel/day, with global deduplication, to reduce leakage between nearby messages. The quick trial uses 1,536 training and 96 validation examples. A lower validation loss measures prediction of held-out writing, not conversational fidelity.

## Reproduce a quick trial

Run from the repository root in WSL. Download steps fetch public code/model files without mounting private training data. The trainer and converters run with no network, no published ports, no Discord token, a non-root user, and bounded resources. Docker build sends only the training scripts and dependency list.

```sh
python3 training/download_base.py --destination ~/.local/share/akubot/training/base

docker compose --env-file .env -f training/compose.yaml build train

python3 training/data.py \
  --archive /mnt/c/Users/theke/Downloads/discord-messages.zip \
  --output ~/.local/share/akubot/training/style-trial-v2/dataset \
  --policy ~/.local/share/akubot/config/channel-policy.json \
  --include-all-other-servers-for-trial --sample-size 1536

mkdir -m 700 -p ~/.local/share/akubot/training/style-trial-v2/output

AKU_TRIAL_NAME=style-trial-v2 docker compose --env-file .env \
  -f training/compose.yaml run --rm train --steps 160

python3 training/convert_models.py \
  --private-dir ~/.local/share/akubot --trial style-trial-v2

python3 training/install_model.py \
  --private-dir ~/.local/share/akubot --trial style-trial-v2 \
  --name aku-style-v2 --base-name aku-style-base-v2
```

Use a fresh trial/output/model name for each run. Existing datasets and adapters are not overwritten. Source server exclusions come from the private channel policy; nothing in this repository contains the owner's server IDs or message contents.

The initial trial uses the pinned `HuggingFaceTB/SmolLM2-1.7B-Instruct` revision in `download_base.py`, BF16 LoRA rank 8, learning rate 0.0001 with warmup/decay, batch size 4, accumulation 2, and seed 42. Merged weights are converted to Q8 GGUF for Ollama. The baseline and trained model use the same base weights, template, and quantization for comparison.

The importer starts its own temporary offline Ollama process to write model files to the private cache. It does not restart the existing bot or model service. Installation registers the exact base and its compatible LoRA build in the private `data/model-catalog.json`. After installing, use Tuning's Base model and LoRA style selectors. Choose None to compare with the original base. The style menu lists only installed styles compatible with the selected base; incompatible and missing styles are hidden. Use Overview's local preview for tests. Keep the trained model private: it was trained on unreviewed personal text and may reproduce parts of it.

The dashboard no longer exposes the SQL message library. LoRA training does not add messages to that separate example database. Training status and scalar metrics are saved to each run's `output/metrics.jsonl`, alongside `training-report.json`; no prompts, message bodies, or generated replies are logged.

## Resumable full Llama training

The full run uses the existing pinned Llama 3.1 8B Instruct source in `base-llama31`, not the SmolLM2 model or the already trained sample adapter. It starts a fresh rank-8 LoRA (alpha 16, dropout 0.05) with a conservative learning rate of 0.00002, 3% warmup, and decay. Batch size 2 with four accumulated batches gives eight messages per optimizer step. One pass over 413,542 training messages takes 51,693 steps. Another 22,238 messages remain held out. Allow roughly 18–24 hours for training on the measured RTX 5090, plus tokenization, checkpoint writes, validation, and export. Actual speed depends on message lengths and other GPU use.

This run stays separate from the quick trials. Its private dataset, checkpoints, adapters, merged weights, and reports live in `~/.local/share/akubot/training/llama-full-v1/`. The owner-authorized server exclusions come from the private channel policy. DMs remain excluded. Original message strings remain intact in the files. The model still sees only the 512-token training window and a generic style request, not reconstructed conversations.

Prepare and build once. These commands do not train or change running services:

```sh
cd ~/workspace/akubot
python3 training/run_full.py prepare
python3 training/run_full.py build
```

Preparation copies the existing full dataset, verifies every row's server scope, checks deduplication across both splits, and fingerprints the data and pinned base files. It creates the dashboard's Ready state. If rebuilding on a new machine, first build the pinned training foundation with `docker compose --env-file .env -f training/compose.yaml build train`.

When ready, launch it in the background:

```sh
python3 training/run_full.py start
```

The dedicated `akubot-llama-full-training` container has no network, published ports, Discord credentials, Docker socket, or access to the bot's database. Only its private output and a narrow control mailbox are writable. It runs as your WSL user with resource limits. Closing the terminal, dashboard, or Codex does not stop it. WSL, Windows, and Docker must remain running and awake. It does not restart, unload, or reconfigure existing services. Keep roughly 19 GB of GPU memory free, and avoid loading other large models while training. An allocation failure preserves existing checkpoints for recovery.

Enable the small local launcher once to use dashboard Start/Stop:

```sh
python3 training/launcher.py --install
```

This installs an Akubot user service in `~/.config/systemd/user/akubot-training-launcher.service`. Enabling the helper does not start training. The service runs in WSL as your user, with read-only home/system files and write access limited to its private receipt folder and training mailbox. It opens no HTTP listener and accepts no executable, arguments, paths, images, or container names from the dashboard. Its only launch action invokes the fixed prepared job. Docker operations remain in the host helper, outside the dashboard container. Start requests have a short expiry, durable acknowledgments, and replay protection. A helper restart never automatically replays an acknowledged start.

Open the dashboard's **Training** tab for Start/Stop, step count, estimated remaining time, training and held-out loss, a recent loss curve, and the latest checkpoint. **Start training** launches the job or recovers its latest checkpoint. **Stop training** requests a safe checkpoint and worker exit. It never force-kills or deletes checkpoints. Wait for Stopped before shutting down Docker. Stop also works while paused. **Pause training** is a request: the worker completes the current optimizer step, saves, releases model and optimizer GPU memory, then shows Paused. During tokenization it pauses without allocating the model. During validation it cancels that validation pass, saves, and pauses. Final model export is a short non-pausable phase. Resume loads the latest checkpoint and continues. The bot's Pause replies button is independent of Pause training.

The controls can also be used from WSL:

```sh
python3 training/run_full.py status
python3 training/run_full.py pause
python3 training/run_full.py stop
python3 training/run_full.py resume
```

If the computer, Docker, or worker stops, choose Start training again, or use the `resume` command. This launches a new detached worker and restores the latest complete, verified checkpoint. Start is enabled only while the local launcher is online, Docker is available, and the prepared job is stopped. The helper verifies the fixed container's ownership label before recovery. A different container with that name is left untouched. The dashboard never receives the Docker socket. Helper and worker heartbeats expose offline status; WSL fallback commands remain available. There are no automatic training restart loops. The installer uses a user service when available. If WSL has no working user-service connection, it falls back to a detached background helper that survives closing the terminal. After WSL shuts down, rerun `python3 training/launcher.py --install` if the launcher is offline. Installing again while it is online leaves the worker unchanged.

Checkpoints save every 250 optimizer steps or five minutes, whichever comes first at a safe step boundary. They include the LoRA weights, optimizer state, learning-rate plan, completed step, exact shuffled message order and cursor, and Python/PyTorch/CUDA random states. Writes use temporary directories and a final checksum marker followed by an atomic rename. Partial writes are ignored; a corrupted newest checkpoint falls back to an earlier verified one. The latest three checkpoints and the best periodic validation checkpoint are retained. A changed dataset, base file, or job configuration is rejected for resume. A sudden shutdown can lose work after the last complete checkpoint. Tokenization has a separate atomic cache, so a completed preprocessing pass is reusable.

Held-out validation uses 256 messages every 2,000 steps and the full validation set after the final step. The best periodic checkpoint is retained for comparison, but the default final export uses the final one-epoch checkpoint. A lower validation loss measures writing prediction, not how convincingly the bot answers real questions. No automatic model selection or Ollama import happens. The final adapter is exported at strength 1.0 so it can be compared or remerged later. Export failures can be retried with `resume` without repeating completed training.

After training completes, convert and register the new model separately, then select it in Tuning:

```sh
python3 training/convert_models.py --private-dir ~/.local/share/akubot \
  --trial llama-full-v1 --only-trained \
  --base-dir ~/.local/share/akubot/training/base-llama31

python3 training/install_model.py --private-dir ~/.local/share/akubot \
  --trial llama-full-v1 --name aku-style-llama-full-v1 \
  --base-dir ~/.local/share/akubot/training/base-llama31 \
  --base-name llama3.1:8b --existing-base --label 'Aku full archive for Llama'
```

The mailbox is `~/.local/share/akubot/data/training-control/llama-full-v1/`. The dashboard reads only sanitized scalar metrics and writes only the four fixed training commands there. The host helper keeps its durable request receipt under `training/launcher`, outside the dashboard mount. It does not receive dataset or checkpoint mounts. Training message bodies, prompts, and replies are never written to container logs. Keep private datasets and trained models out of GitHub.

The end-to-end recovery test uses a tiny, randomly initialized model and synthetic tokens. It checks exact adapter equality against uninterrupted training after forced termination, pause/resume, and dashboard Stop/restart, corrupt and incomplete checkpoint handling, fingerprint mismatch rejection, and retention bounds:

```sh
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:size=256m,mode=1777 \
  --memory 2g --cpus 2 --pids-limit 128 --log-driver none \
  --mount type=bind,src="$PWD/training/check_recovery.py",dst=/trainer/check_recovery.py,readonly \
  --entrypoint python akubot-full-trainer:local /trainer/check_recovery.py
```

## Checks

```sh
python3 -m unittest discover -s training -p 'test_*.py' -q
```

## Rebuild at a different LoRA strength

Strength is a multiplier on the learned weight update before it is merged into the base model. A value of 1.5 exports `base + 1.5 * learned_update`. The original adapter checkpoint is retained at its trained scale. Strength does not supply missing context or improve the base model's reasoning. Each exported strength is registered as its own compatible style choice; Ollama does not apply a live multiplier to these merged models.

The stronger SmolLM2 build reuses the original random-sample adapter. A separate Llama 3.1 8B adapter is trained on exactly the same 1,536 training and 96 validation messages. These runs retain all earlier data exclusions and the owner's authorization to include other server channels. No recent-only sample or full-dataset training run was started.

Public base model files can be downloaded separately into private WSL storage:

```sh
python3 training/download_base.py \
  --destination ~/.local/share/akubot/training/base-llama31 \
  --repository unsloth/Meta-Llama-3.1-8B-Instruct \
  --revision a2856192dd7c25b842431f39c179a6c2c2f627d1
```

`AKU_TRAIN_BASE=base-llama31` selects that base directory for the offline training container. Set up a fresh run with a copy of the selected dataset and an empty output directory before running:

```sh
AKU_TRAIN_BASE=base-llama31 AKU_TRIAL_NAME=style-llama-strong-v1 \
docker compose --env-file .env -f training/compose.yaml run --rm train \
  --steps 160 --batch-size 2 --accumulation 4 \
  --learning-rate 0.00005 --merge-strength 1.5
```

The trainer loads weights directly onto the GPU to keep host-memory use bounded. Reports record the exact base repository/revision and distinguish ordinary adapter validation loss from validation loss at the exported strength. A `--merge-strength` value from 0 to 2 is supported.

To reuse an adapter without training again, run the same isolated service with `--entrypoint python`, mount the original adapter read-only at `/adapter`, and use `/trainer/rebuild.py --strength 1.5`. This writes a new `merged` directory and report in the selected fresh output run.

For another base, pass its directory explicitly to the converter and installer. The base identity in the training report must match the selected directory:

```sh
python3 training/convert_models.py --private-dir ~/.local/share/akubot \
  --trial style-llama-strong-v1 \
  --base-dir ~/.local/share/akubot/training/base-llama31 --only-trained

python3 training/install_model.py --private-dir ~/.local/share/akubot \
  --trial style-llama-strong-v1 --name aku-style-llama-strong-v1 \
  --base-dir ~/.local/share/akubot/training/base-llama31 \
  --base-name llama3.1:8b --existing-base \
  --label 'Aku style 1 for Llama (1.5×)'
```

`--existing-base` retains an already installed baseline rather than converting it again. The existing baseline may have a different quantization from the new Q8 trained build. Both legacy and current Ollama manifest layouts are checked before installing. The installer selects the proper native message template for SmolLM2 or Llama 3.1. Source registration appears after refreshing Tuning; no bot restart is needed.
