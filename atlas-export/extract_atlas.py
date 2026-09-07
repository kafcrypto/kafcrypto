import csv
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from eth_abi import decode
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ATLAS_BASE = "https://atlas.optimism.io"
RESULTS_URL = f"{ATLAS_BASE}/round/results"
EAS_GRAPHQL = "https://optimism.easscan.org/graphql"
PROJECT_METADATA_SCHEMA = "0xe035e3fe27a64c8d7291ae54c6e85676addcbc2d179224fe7fc1f7f05a8c6eac"
ROUND_LABELS = {
    "4": "Round 4: Onchain Builders",
    "5": "Round 5: OP Stack",
    "6": "Round 6: Governance",
    "7": "Retro Funding: Dev Tooling",
    "8": "Retro Funding: Onchain Builders",
}
OUT = Path(os.environ.get("ATLAS_EXPORT_DIR", "atlas-export/out"))
OUT.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; OP-Atlas-Archive/1.0; +https://github.com/kafcrypto/kafcrypto)",
    "Accept": "application/json,text/plain,*/*",
})


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_amount(text: str):
    if not text:
        return None
    s = text.strip().replace(",", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMB])?$", s, re.I)
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return value * mult


def scrape_round(page, round_id: str):
    url = f"{RESULTS_URL}?rounds={round_id}"
    print(f"[atlas] round {round_id}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)

    try:
        page.wait_for_selector('a[href^="/project/"]', timeout=45_000)
    except PlaywrightTimeoutError:
        print(f"[atlas] round {round_id}: no rows appeared")
        return []

    stable_rounds = 0
    previous = -1
    for i in range(250):
        count = page.locator('a[href^="/project/"]').count()
        if count == previous:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous = count

        button = page.get_by_role("button", name="Show more")
        if button.count() == 0:
            break
        try:
            button.first.click(timeout=15_000)
            page.wait_for_timeout(700)
            try:
                page.wait_for_function(
                    "prev => document.querySelectorAll('a[href^=\"/project/\"]').length > prev",
                    arg=count,
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[atlas] round {round_id}: show-more stopped: {e}")
            break
        if stable_rounds >= 3:
            break

    rows = page.locator('a[href^="/project/"]')
    records = []
    for i in range(rows.count()):
        a = rows.nth(i)
        href = a.get_attribute("href") or ""
        project_id = href.rstrip("/").split("/")[-1]
        if not project_id.startswith("0x"):
            continue
        try:
            name = (a.locator("h5").first.inner_text(timeout=2000) or "").strip()
        except Exception:
            name = ""
        try:
            description = (a.locator("p").first.inner_text(timeout=2000) or "").strip()
        except Exception:
            description = ""
        amount_text = ""
        try:
            img = a.locator('img[alt="Optimism"]').first
            if img.count():
                amount_text = (img.locator("xpath=..").locator("span").last.inner_text(timeout=2000) or "").strip()
        except Exception:
            pass
        records.append({
            "project_id": project_id,
            "project_name": name,
            "description_from_results": description,
            "round_id": round_id,
            "round_label": ROUND_LABELS.get(round_id, round_id),
            "reward_display": amount_text,
            "reward_op": parse_amount(amount_text),
            "atlas_url": f"{ATLAS_BASE}/project/{project_id}",
            "source_url": url,
        })

    dedup = {}
    for r in records:
        dedup[(r["project_id"], r["round_id"])] = r
    records = list(dedup.values())
    print(f"[atlas] round {round_id}: {len(records)} reward rows")
    return records


def scrape_all_rewards():
    all_rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        for rid in ROUND_LABELS:
            all_rows.extend(scrape_round(page, rid))
        browser.close()
    return all_rows


def eas_query(skip: int, take: int = 100):
    fields_variants = ["id time data revoked", "id time data"]
    last_error = None
    for fields in fields_variants:
        q = f'''query {{
          attestations(
            where: {{schemaId: {{equals: "{PROJECT_METADATA_SCHEMA}"}}}},
            take: {take},
            skip: {skip},
            orderBy: [{{time: desc}}]
          ) {{ {fields} }}
        }}'''
        for attempt in range(6):
            try:
                r = SESSION.post(EAS_GRAPHQL, json={"query": q}, timeout=60)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                payload = r.json()
                if payload.get("errors"):
                    last_error = payload["errors"]
                    break
                return payload.get("data", {}).get("attestations", [])
            except Exception as e:
                last_error = repr(e)
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"EAS query failed at skip={skip}: {last_error}")


def fetch_all_eas_attestations():
    all_rows = []
    take = 100
    skip = 0
    while True:
        rows = eas_query(skip, take)
        all_rows.extend(rows)
        print(f"[eas] fetched {len(all_rows)} attestations")
        if len(rows) < take:
            break
        skip += take
    return all_rows


def hex32(v):
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    return str(v)


def decode_metadata_attestation(att):
    raw = att.get("data") or ""
    if raw.startswith("0x"):
        raw = raw[2:]
    try:
        values = decode(
            ["bytes32", "uint256", "string", "string", "bytes32", "uint8", "string"],
            bytes.fromhex(raw),
        )
        project_ref_uid, farcaster_id, name, category, parent_ref, metadata_type, metadata_url = values
        return {
            "attestation_id": att.get("id"),
            "attestation_time": int(att.get("time") or 0),
            "revoked": att.get("revoked", False),
            "project_id": hex32(project_ref_uid),
            "farcaster_id": int(farcaster_id),
            "name": name,
            "category": category,
            "parent_project_id": hex32(parent_ref),
            "metadata_type": int(metadata_type),
            "metadata_url": metadata_url,
        }
    except Exception as e:
        return {
            "attestation_id": att.get("id"),
            "attestation_time": int(att.get("time") or 0),
            "decode_error": repr(e),
            "raw_data": att.get("data"),
        }


def candidate_urls(url: str):
    if not url:
        return []
    if url.startswith("ipfs://"):
        tail = url[len("ipfs://"):].lstrip("/")
        return [
            f"https://ipfs.io/ipfs/{tail}",
            f"https://gateway.pinata.cloud/ipfs/{tail}",
            f"https://dweb.link/ipfs/{tail}",
        ]
    if url.startswith("ar://"):
        return ["https://arweave.net/" + url[len("ar://"):].lstrip("/")]
    if url.startswith("http://") or url.startswith("https://"):
        return [url]
    return [url]


def fetch_metadata(url: str):
    errors = []
    for candidate in candidate_urls(url):
        for attempt in range(3):
            try:
                r = SESSION.get(candidate, timeout=30)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if "json" in ctype.lower():
                    data = r.json()
                else:
                    text = r.text.strip()
                    data = json.loads(text)
                return {"ok": True, "resolved_url": candidate, "data": data}
            except Exception as e:
                errors.append(f"{candidate}: {repr(e)}")
                time.sleep(0.5 * (attempt + 1))
    return {"ok": False, "resolved_url": None, "data": None, "errors": errors}


def flatten_links(meta):
    out = {}
    links = meta.get("links") if isinstance(meta, dict) else None
    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "").lower().strip()
            u = item.get("url")
            if t and u:
                out.setdefault(t, []).append(u)
    return out


def first_nonempty(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def normalize_project(project_id, latest_att, meta_result, reward_rows):
    meta = meta_result.get("data") if meta_result and meta_result.get("ok") else {}
    if not isinstance(meta, dict):
        meta = {}
    links = flatten_links(meta)
    website = first_nonempty(meta.get("website"), (links.get("website") or [None])[0])
    twitter = first_nonempty(
        (links.get("twitter") or [None])[0],
        (links.get("x") or [None])[0],
        meta.get("twitter"),
        meta.get("x"),
    )
    githubs = links.get("github") or []
    if isinstance(meta.get("github"), str):
        githubs.append(meta["github"])
    teams = first_nonempty(meta.get("team"), meta.get("teamMembers"), meta.get("contributors"), [])
    contracts = first_nonempty(meta.get("contracts"), meta.get("addresses"), [])
    chains = first_nonempty(meta.get("chains"), meta.get("networks"), [])

    total_reward = sum((r.get("reward_op") or 0) for r in reward_rows)
    rounds = sorted({r["round_id"] for r in reward_rows}, key=lambda x: int(x))
    result_name = first_nonempty(*[r.get("project_name") for r in reward_rows])
    result_desc = first_nonempty(*[r.get("description_from_results") for r in reward_rows])

    return {
        "atlas_project_id": project_id,
        "name": first_nonempty(meta.get("name"), meta.get("title"), latest_att.get("name"), result_name),
        "description": first_nonempty(meta.get("description"), result_desc),
        "category": first_nonempty(meta.get("category"), latest_att.get("category")),
        "website": website,
        "twitter_x": twitter,
        "github_repositories": githubs,
        "team_contributors": teams,
        "chains": chains,
        "contracts": contracts,
        "organization": first_nonempty(meta.get("organization"), meta.get("organizationName")),
        "farcaster_id": latest_att.get("farcaster_id"),
        "atlas_url": f"{ATLAS_BASE}/project/{project_id}",
        "rewarded_round_ids": rounds,
        "rewarded_round_count": len(rounds),
        "total_reward_op_display_parsed": total_reward,
        "latest_attestation_id": latest_att.get("attestation_id"),
        "latest_attestation_time": latest_att.get("attestation_time"),
        "metadata_url": latest_att.get("metadata_url"),
        "metadata_resolved_url": meta_result.get("resolved_url") if meta_result else None,
        "metadata_fetch_ok": bool(meta_result and meta_result.get("ok")),
        "metadata_raw": meta,
    }


def to_csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def write_csv(path: Path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: to_csv_cell(row.get(k)) for k in fields})


def main():
    started = datetime.now(timezone.utc).isoformat()
    print(f"[start] {started}")

    rewards = scrape_all_rewards()
    write_json(OUT / "atlas_rewards_raw.json", rewards)
    write_csv(
        OUT / "atlas_funding.csv",
        rewards,
        [
            "project_id", "project_name", "round_id", "round_label", "reward_display",
            "reward_op", "atlas_url", "description_from_results", "source_url"
        ],
    )

    rewarded_ids = sorted({r["project_id"] for r in rewards})
    print(f"[atlas] unique rewarded project IDs: {len(rewarded_ids)}")

    eas_raw = fetch_all_eas_attestations()
    write_json(OUT / "atlas_eas_snapshots_raw.json", eas_raw)
    decoded = [decode_metadata_attestation(a) for a in eas_raw]
    write_json(OUT / "atlas_eas_snapshots_decoded.json", decoded)

    good = [d for d in decoded if d.get("project_id") and not d.get("decode_error") and not d.get("revoked")]
    good.sort(key=lambda x: x.get("attestation_time", 0), reverse=True)
    latest = {}
    for d in good:
        latest.setdefault(d["project_id"].lower(), d)
    print(f"[eas] unique projects with decoded metadata snapshots: {len(latest)}")

    metadata_results = {}
    latest_rewarded = {}
    for pid in rewarded_ids:
        att = latest.get(pid.lower())
        if att:
            latest_rewarded[pid] = att

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {
            ex.submit(fetch_metadata, att.get("metadata_url") or ""): pid
            for pid, att in latest_rewarded.items()
        }
        for idx, fut in enumerate(as_completed(futs), start=1):
            pid = futs[fut]
            try:
                metadata_results[pid] = fut.result()
            except Exception as e:
                metadata_results[pid] = {"ok": False, "data": None, "resolved_url": None, "errors": [repr(e)]}
            if idx % 50 == 0:
                print(f"[meta] resolved {idx}/{len(futs)}")

    reward_by_project = {}
    for r in rewards:
        reward_by_project.setdefault(r["project_id"], []).append(r)

    projects = []
    gaps = []
    for pid in rewarded_ids:
        att = latest.get(pid.lower())
        if not att:
            gaps.append({"project_id": pid, "gap": "no_eas_metadata_snapshot", "atlas_url": f"{ATLAS_BASE}/project/{pid}"})
            att = {"project_id": pid}
        meta_result = metadata_results.get(pid) or {"ok": False, "data": None, "resolved_url": None}
        if att.get("metadata_url") and not meta_result.get("ok"):
            gaps.append({
                "project_id": pid,
                "gap": "metadata_url_unresolved",
                "metadata_url": att.get("metadata_url"),
                "atlas_url": f"{ATLAS_BASE}/project/{pid}",
            })
        projects.append(normalize_project(pid, att, meta_result, reward_by_project.get(pid, [])))

    projects.sort(key=lambda x: ((x.get("name") or "").lower(), x["atlas_project_id"]))
    write_json(OUT / "atlas_projects.json", projects)
    write_csv(
        OUT / "atlas_projects.csv",
        projects,
        [
            "atlas_project_id", "name", "description", "category", "organization", "website",
            "twitter_x", "github_repositories", "team_contributors", "chains", "contracts",
            "farcaster_id", "atlas_url", "rewarded_round_ids", "rewarded_round_count",
            "total_reward_op_display_parsed", "latest_attestation_id", "latest_attestation_time",
            "metadata_url", "metadata_resolved_url", "metadata_fetch_ok"
        ],
    )
    write_csv(OUT / "atlas_gaps.csv", gaps, ["project_id", "gap", "metadata_url", "atlas_url"])

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reward_rows": len(rewards),
        "unique_rewarded_projects": len(rewarded_ids),
        "eas_metadata_attestations": len(eas_raw),
        "unique_projects_in_eas_metadata": len(latest),
        "rewarded_projects_with_eas_snapshot": sum(1 for pid in rewarded_ids if pid.lower() in latest),
        "rewarded_projects_missing_eas_snapshot": sum(1 for pid in rewarded_ids if pid.lower() not in latest),
        "rewarded_projects_metadata_fetch_ok": sum(1 for pid in rewarded_ids if metadata_results.get(pid, {}).get("ok")),
        "gap_rows": len(gaps),
        "round_counts": {
            rid: sum(1 for r in rewards if r["round_id"] == rid)
            for rid in ROUND_LABELS
        },
        "notes": [
            "Reward amounts are parsed from the public OP Atlas UI display, which may be rounded for large values.",
            "Raw EAS attestation data is preserved separately so no metadata history is discarded.",
            "The canonical project master is deduplicated by Atlas project ID.",
        ],
    }
    write_json(OUT / "atlas_audit.json", audit)
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
