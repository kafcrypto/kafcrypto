import csv
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ATLAS_BASE = "https://atlas.optimism.io"
RESULTS_URL = f"{ATLAS_BASE}/round/results"
OUT = Path("atlas-export/out")


def scrape_all():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_selector('a[href^="/project/"]', timeout=45_000)
        for _ in range(300):
            prev = page.locator('a[href^="/project/"]').count()
            button = page.get_by_role("button", name="Show more")
            if button.count() == 0:
                break
            try:
                button.first.click(timeout=15_000)
                try:
                    page.wait_for_function(
                        "prev => document.querySelectorAll('a[href^=\"/project/\"]').length > prev",
                        arg=prev,
                        timeout=15_000,
                    )
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(1500)
            except Exception:
                break
        rows = []
        links = page.locator('a[href^="/project/"]')
        for i in range(links.count()):
            a = links.nth(i)
            href = a.get_attribute("href") or ""
            pid = href.rstrip("/").split("/")[-1]
            if not pid.startswith("0x"):
                continue
            try:
                name = (a.locator("h5").first.inner_text(timeout=2000) or "").strip()
            except Exception:
                name = ""
            try:
                desc = (a.locator("p").first.inner_text(timeout=2000) or "").strip()
            except Exception:
                desc = ""
            amount = ""
            try:
                img = a.locator('img[alt="Optimism"]').first
                if img.count():
                    amount = (img.locator("xpath=..").locator("span").last.inner_text(timeout=2000) or "").strip()
            except Exception:
                pass
            rows.append({"project_id": pid, "project_name": name, "description": desc, "reward_display": amount, "atlas_url": f"{ATLAS_BASE}/project/{pid}"})
        browser.close()
    return rows


def main():
    rows = scrape_all()
    (OUT / "atlas_all_rewards_raw.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "atlas_all_rewards.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["project_id", "project_name", "description", "reward_display", "atlas_url"])
        w.writeheader(); w.writerows(rows)

    filtered = json.loads((OUT / "atlas_rewards_raw.json").read_text(encoding="utf-8"))
    filtered_pairs = {(r["project_id"], r.get("reward_display", ""), r.get("project_name", "")) for r in filtered}
    filtered_ids = {r["project_id"] for r in filtered}
    all_ids = {r["project_id"] for r in rows}

    unmatched_rows = [r for r in rows if (r["project_id"], r.get("reward_display", ""), r.get("project_name", "")) not in filtered_pairs]
    missing_ids = sorted(all_ids - filtered_ids)

    audit_path = OUT / "atlas_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit.update({
        "all_rounds_ui_rows": len(rows),
        "all_rounds_ui_unique_projects": len(all_ids),
        "all_rounds_rows_not_matched_to_round_4_8": len(unmatched_rows),
        "all_rounds_unique_project_ids_missing_from_round_4_8": len(missing_ids),
        "all_rounds_missing_project_ids": missing_ids,
    })
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    with (OUT / "atlas_all_rounds_unmatched.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["project_id", "project_name", "description", "reward_display", "atlas_url"])
        w.writeheader(); w.writerows(unmatched_rows)

    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
