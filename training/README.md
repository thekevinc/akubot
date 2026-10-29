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

The importer starts its own temporary offline Ollama process to write model files to the private cache. It does not restart the existing bot or model service. Installation registers the exact base and its compatible LoRA build in the private `data/model-catalog.json`. After installing, use Tuning's Base model and LoRA style selectors. Choose None to compare with the original base. Styles trained for other bases are disabled. Use Overview's local preview for tests. Keep the trained model private: it was trained on unreviewed personal text and may reproduce parts of it.

The dashboard no longer exposes the SQL message library. LoRA training does not add messages to that separate example database. Training status and scalar metrics are saved to each run's `output/metrics.jsonl`, alongside `training-report.json`; no prompts, message bodies, or generated replies are logged.

## Full dataset, later

Adding `--all-messages` to `training/data.py` prepares all unique non-empty messages outside the excluded sources, rather than sampling. The prepared full dataset lives in the private `style-full-v1` directory. It contains 413,542 training and 22,238 held-out examples. No full training job has been started.

To train one pass later, rebuild the trainer after code changes, ensure the new output directory exists, and run:

```sh
AKU_TRIAL_NAME=style-full-v1 docker compose --env-file .env \
  -f training/compose.yaml run --rm train \
  --epochs 1 --batch-size 16 --accumulation 1
```

The explicit command overrides the quick trial's 160-step default. Benchmark the larger batch before scheduling the full run. Never publish model artifacts or datasets to GitHub.

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
