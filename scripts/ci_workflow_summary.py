"""Publish measured GitHub job/step durations without reading secrets or job logs."""
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def seconds(start, end):
    if not start or not end:
        return None
    return max(0, round((datetime.fromisoformat(end.replace("Z", "+00:00"))
                         - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))


def duration(value):
    return "—" if value is None else f"{value // 60}:{value % 60:02d}"


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    headers = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    jobs = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
        with urlopen(Request(url, headers=headers), timeout=30) as response:
            batch = json.load(response)["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    lines = ["## Release timings", "", "Measured wall-clock durations; parallel jobs overlap.", "",
             "| Job | Result | Duration |", "| --- | --- | ---: |"]
    for job in jobs:
        if job["name"] == "timing-summary":
            continue
        name = job["name"].replace("|", "\\|")
        elapsed = seconds(job.get("started_at"), job.get("completed_at"))
        lines.append(f"| {name} | {job.get('conclusion') or job['status']} | {duration(elapsed)} |")
        for step in job.get("steps", []):
            if step["name"].startswith(("Build & push", "Deploy to")):
                elapsed = seconds(step.get("started_at"), step.get("completed_at"))
                # The deployment action name can contain the configured host; use a stable label.
                label = "Deploy to production" if step["name"].startswith("Deploy to") else step["name"]
                lines.append(f"| ↳ {label} | {step.get('conclusion') or step['status']} | {duration(elapsed)} |")
    lines += ["", "The deploy log records `DEPLOY_TIMING` for pull, migrations, restart, readiness and cleanup.",
              "Each migration also reports its own duration. A build-only run leaves production unchanged.", ""]
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write("\n".join(lines))


if __name__ == "__main__":
    main()
