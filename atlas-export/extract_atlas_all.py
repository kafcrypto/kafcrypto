import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone

from extract_atlas import fetch_metadata, first_nonempty, flatten_links, parse_amount

OUT = Path('atlas-export/out')
ATLAS_BASE = 'https://atlas.optimism.io'


def load_json(name, default=None):
    p = OUT / name
    if not p.exists():
        return [] if default is None else default
    return json.loads(p.read_text(encoding='utf-8'))


def write_json(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def csv_cell(v):
    if v is None:
        return ''
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    return v


def write_csv(name, rows, fields):
    with (OUT / name).open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for row in rows:
            w.writerow({k: csv_cell(row.get(k)) for k in fields})


def latest_projects(decoded):
    good = [
        d for d in decoded
        if d.get('project_id') and not d.get('decode_error') and not d.get('revoked')
    ]
    good.sort(key=lambda x: int(x.get('attestation_time') or 0), reverse=True)
    latest = {}
    snapshot_count = {}
    for d in good:
        key = d['project_id'].lower()
        snapshot_count[key] = snapshot_count.get(key, 0) + 1
        latest.setdefault(key, d)
    return latest, snapshot_count


def reward_index(rows):
    idx = {}
    for r in rows:
        pid = (r.get('project_id') or '').lower()
        if not pid:
            continue
        idx.setdefault(pid, []).append(r)
    return idx


def normalize(pid, att, meta_result, rewards, snapshot_count):
    meta = meta_result.get('data') if meta_result and meta_result.get('ok') else {}
    if not isinstance(meta, dict):
        meta = {}
    links = flatten_links(meta)

    website = first_nonempty(meta.get('website'), (links.get('website') or [None])[0])
    twitter = first_nonempty(
        (links.get('twitter') or [None])[0],
        (links.get('x') or [None])[0],
        meta.get('twitter'), meta.get('x')
    )
    githubs = list(links.get('github') or [])
    if isinstance(meta.get('github'), str):
        githubs.append(meta['github'])

    description = first_nonempty(meta.get('description'))
    if not description and rewards:
        description = first_nonempty(*[r.get('description') for r in rewards])

    reward_total = sum(parse_amount(r.get('reward_display') or '') or 0 for r in rewards)
    reward_displays = [r.get('reward_display') for r in rewards if r.get('reward_display')]

    return {
        'atlas_project_id': pid,
        'name': first_nonempty(meta.get('name'), meta.get('title'), att.get('name')),
        'description': description,
        'category': first_nonempty(meta.get('category'), att.get('category')),
        'organization': first_nonempty(meta.get('organization'), meta.get('organizationName')),
        'website': website,
        'twitter_x': twitter,
        'github_repositories': githubs,
        'team_contributors': first_nonempty(meta.get('team'), meta.get('teamMembers'), meta.get('contributors'), []),
        'chains': first_nonempty(meta.get('chains'), meta.get('networks'), []),
        'contracts': first_nonempty(meta.get('contracts'), meta.get('addresses'), []),
        'farcaster_id': att.get('farcaster_id'),
        'parent_project_id': att.get('parent_project_id'),
        'metadata_type': att.get('metadata_type'),
        'metadata_url': att.get('metadata_url'),
        'metadata_resolved_url': meta_result.get('resolved_url') if meta_result else None,
        'metadata_fetch_ok': bool(meta_result and meta_result.get('ok')),
        'latest_attestation_id': att.get('attestation_id'),
        'latest_attestation_time': att.get('attestation_time'),
        'snapshot_count': snapshot_count,
        'rewarded': bool(rewards),
        'reward_record_count': len(rewards),
        'reward_display_values': reward_displays,
        'total_reward_op_display_parsed': reward_total,
        'atlas_url': f'{ATLAS_BASE}/project/{pid}',
        'metadata_raw': meta,
    }


def main():
    decoded = load_json('atlas_eas_snapshots_decoded.json')
    rewards_all = load_json('atlas_all_rewards_raw.json')
    latest, counts = latest_projects(decoded)
    rewards = reward_index(rewards_all)

    print(f'[all] snapshots: {len(decoded)}')
    print(f'[all] unique Atlas project IDs: {len(latest)}')
    print(f'[all] rewarded unique project IDs: {len(rewards)}')

    metadata_results = {}
    items = list(latest.items())
    with ThreadPoolExecutor(max_workers=24) as ex:
        futs = {
            ex.submit(fetch_metadata, att.get('metadata_url') or ''): pid
            for pid, att in items
        }
        total = len(futs)
        for i, fut in enumerate(as_completed(futs), 1):
            pid = futs[fut]
            try:
                metadata_results[pid] = fut.result()
            except Exception as e:
                metadata_results[pid] = {'ok': False, 'resolved_url': None, 'data': None, 'errors': [repr(e)]}
            if i % 100 == 0 or i == total:
                print(f'[all-meta] {i}/{total}')

    projects = []
    for pid, att in latest.items():
        projects.append(normalize(
            pid,
            att,
            metadata_results.get(pid, {'ok': False, 'data': None}),
            rewards.get(pid, []),
            counts.get(pid, 0),
        ))

    projects.sort(key=lambda r: ((r.get('name') or '').lower(), r['atlas_project_id']))
    write_json('atlas_all_projects.json', projects)
    write_csv('atlas_all_projects.csv', projects, [
        'atlas_project_id','name','description','category','organization','website','twitter_x',
        'github_repositories','team_contributors','chains','contracts','farcaster_id','parent_project_id',
        'metadata_type','metadata_url','metadata_resolved_url','metadata_fetch_ok','latest_attestation_id',
        'latest_attestation_time','snapshot_count','rewarded','reward_record_count','reward_display_values',
        'total_reward_op_display_parsed','atlas_url'
    ])

    failures = [
        {
            'atlas_project_id': p['atlas_project_id'],
            'name': p.get('name'),
            'metadata_url': p.get('metadata_url'),
            'atlas_url': p.get('atlas_url'),
        }
        for p in projects if not p.get('metadata_fetch_ok')
    ]
    write_csv('atlas_all_metadata_failures.csv', failures, ['atlas_project_id','name','metadata_url','atlas_url'])

    audit = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'eas_snapshots': len(decoded),
        'unique_atlas_projects': len(projects),
        'rewarded_unique_projects': sum(1 for p in projects if p['rewarded']),
        'non_rewarded_unique_projects': sum(1 for p in projects if not p['rewarded']),
        'metadata_fetch_ok': sum(1 for p in projects if p['metadata_fetch_ok']),
        'metadata_fetch_failed': sum(1 for p in projects if not p['metadata_fetch_ok']),
        'projects_with_description': sum(1 for p in projects if p.get('description')),
        'projects_with_website': sum(1 for p in projects if p.get('website')),
        'projects_with_github': sum(1 for p in projects if p.get('github_repositories')),
    }
    write_json('atlas_all_audit.json', audit)
    print(json.dumps(audit, indent=2))


if __name__ == '__main__':
    main()
