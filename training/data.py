"""Prepare an explicitly authorized, archive-only writing-style trial."""
from __future__ import annotations
import argparse, hashlib, json, os, random, sys, zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def clean_message(value):
    # Preserve the complete message string, per the owner's trial preference.
    # Only empty/non-text records are omitted. Source JSON fields stay separate.
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def prepare(archive: Path, destination: Path, excluded_guilds: set[str], *, allow_unknown: bool, size=1536, seed=42, all_messages=False):
    if not allow_unknown:
        raise ValueError('This archive-only trial requires explicit authorization for unknown NSFW status.')
    if destination.exists():
        raise ValueError('Choose a new trial directory; existing runs are never overwritten.')
    rng = random.Random(seed)
    counts = Counter()
    pools = {'train': [], 'validation': []}
    seen = set()
    channel_guilds = {}
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names = {item.filename for item in entries}
        if len(names) != len(entries) or sum(item.file_size for item in entries) > 1_000_000_000:
            raise ValueError('Unexpected archive limits.')
        for item in entries:
            path = PurePosixPath(item.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in item.filename:
                raise ValueError('Unsafe archive entry.')
        for item in entries:
            path = PurePosixPath(item.filename)
            if path.name != 'channel.json':
                continue
            if item.file_size > 2_000_000 or len(path.parts) != 3 or path.parts[0].lower() != 'messages':
                raise ValueError('Unexpected channel metadata.')
            channel = json.loads(package.read(item))
            guild = channel.get('guild')
            if not isinstance(guild, dict):
                counts['excluded_dm_channels'] += 1
                continue
            gid, cid = str(guild.get('id', '')), str(channel.get('id', ''))
            if not gid.isdecimal() or not cid.isdecimal():
                raise ValueError('Invalid channel identity.')
            if gid in excluded_guilds or str(guild.get('name', '')).strip().casefold() == 'secret':
                counts['excluded_secret_channels'] += 1
                continue
            filename = str(path.parent / 'messages.json')
            if filename not in names or package.getinfo(filename).file_size > 50_000_000:
                raise ValueError('Missing or oversized message file.')
            rows = json.loads(package.read(filename))
            if not isinstance(rows, list):
                raise ValueError('Unexpected message format.')
            counts['included_channels'] += 1
            channel_guilds[cid] = gid
            for row in rows:
                counts['source_messages'] += 1
                if not isinstance(row, dict):
                    counts['filtered_messages'] += 1
                    continue
                text = clean_message(row.get('Contents'))
                if not text:
                    counts['filtered_messages'] += 1
                    continue
                digest = hashlib.sha256(text.casefold().encode()).hexdigest()
                if digest in seen:
                    counts['duplicate_messages'] += 1
                    continue
                seen.add(digest)
                # Nearby messages from one channel/day stay in the same split.
                timestamp = str(row.get('Timestamp', ''))
                group = hashlib.sha256((cid + ':' + timestamp[:10]).encode()).hexdigest()
                split = 'validation' if int(group[:8], 16) % 20 == 0 else 'train'
                pools[split].append({'text': text, 'source': {'guild_id': gid, 'channel_id': cid,
                                    'message_id': str(row.get('ID', '')), 'timestamp': timestamp}})
    selected = {}
    for split, limit in [('train', size), ('validation', min(96, max(16, size // 16)))]:
        if all_messages:
            selected[split] = pools[split]
            continue
        rng.shuffle(pools[split])
        # Bound any one channel's contribution to keep the sample varied.
        per_channel = Counter()
        channel_count = min(5, max(1, len({row['source']['channel_id'] for row in pools[split]})))
        channel_cap = max(8, limit // channel_count)
        chosen = []
        for row in pools[split]:
            cid = row['source']['channel_id']
            if per_channel[cid] >= channel_cap:
                continue
            chosen.append(row)
            per_channel[cid] += 1
            if len(chosen) >= limit:
                break
        selected[split] = chosen
    if len(selected['train']) < 128 or len(selected['validation']) < 8:
        raise ValueError('Not enough clean data for this trial.')
    destination.mkdir(mode=0o700, parents=True)
    for split, rows in selected.items():
        target = destination / f'{split}.jsonl'
        with target.open('x', encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        target.chmod(0o600)
        counts[split + '_examples'] = len(rows)
    report = {'version': 1, 'seed': seed, 'selection': 'all unique non-empty messages' if all_messages else 'bounded sample', 'counts': dict(counts), 'excluded_guild_ids': sorted(excluded_guilds),
              'unknown_nsfw_status_authorized_for_trial': True, 'scope': 'All other server channels included per owner instruction; Secret and DMs excluded', 'targets': 'Exact Contents strings, including URLs and formatting; no wrapper JSON metadata',
              'context': 'No conversation reconstruction or invented historical prompts',
              'input_format': 'official Discord outgoing-message archive', 'source_archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    (destination / 'dataset-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--include-all-other-servers-for-trial', '--allow-unknown-nsfw-for-trial', dest='allow_unknown_nsfw_for_trial', action='store_true')
    parser.add_argument('--sample-size', type=int, default=1536)
    parser.add_argument('--all-messages', action='store_true')
    args = parser.parse_args()
    try:
        if not 128 <= args.sample_size <= 20000:
            raise ValueError('Sample size must be between 128 and 20000.')
        policy = json.loads(args.policy.read_text())
        excluded = set(policy['excluded_guild_ids'])
        if not excluded or not all(isinstance(value, str) and value.isdecimal() for value in excluded):
            raise ValueError('A valid server exclusion policy is required.')
        report = prepare(args.archive, args.output, excluded, allow_unknown=args.allow_unknown_nsfw_for_trial, size=args.sample_size, all_messages=args.all_messages)
        print(json.dumps(report['counts']))
    except Exception:
        print('Dataset preparation failed. No message bodies or raw exceptions were printed.', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
