#!/usr/bin/env python3
"""Deploy only the separately managed SPS service to an exact main commit.

Run through the existing server deployment account, never through the Hub app.
--check is read-only. --apply builds before downtime, then makes a stopped-volume
backup and replaces only Compose project sps / service sps. No carrier writes,
Hub restarts, Caddy changes, data restoration, or volume deletion are performed.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


PROJECT = "sps"
SERVICE = "sps"
CHECKOUT = Path("/opt/sps")
CONFIG_FILES = [CHECKOUT / "compose.yaml", CHECKOUT / "compose.hub.yaml"]
IMAGE_TAG = "sps-sps:latest"
PUBLIC = "https://hub.biketrek.sk/sps/"
PRIVATE_STATE = Path.home() / ".local" / "state" / "sps-deploy"
REVISION_LABEL = "org.opencontainers.image.revision"
INSPECT_FORMAT = '''{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"image_ref":{{json .Config.Image}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}},"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}},"oom_killed":{{json .State.OOMKilled}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'''
MANIFEST_CODE = r'''
import hashlib,json,re
from pathlib import Path
root=Path('/app/web/dist')
index=(root/'index.html').read_bytes()
assets=re.findall(rb'(?:src|href)="\./(assets/[A-Za-z0-9_.-]+)"',index)
assert assets, 'No compiled frontend assets'
print(json.dumps({'index':hashlib.sha256(index).hexdigest(),'assets':{p.decode():hashlib.sha256((root/p.decode()).read_bytes()).hexdigest() for p in assets}}))
'''
BACKUP_CODE = "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/source',arcname='data',recursive=True); t.close()"


class DeploymentError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def phase(message):
    print("SPS_DEPLOY " + message, flush=True)


def run(arguments, *, timeout=60, cwd=CHECKOUT, stdout=None, stdin=None):
    """Capture subprocess output; errors never echo commands, keys or env data."""
    try:
        result = subprocess.run(arguments, cwd=cwd, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError("Command unavailable or timed out: " + arguments[0]) from exc
    require(result.returncode == 0, f"Command failed: {arguments[0]} (exit {result.returncode}). Output withheld to protect server configuration.")
    return (result.stdout or b"").decode("utf-8").strip()


def git(*arguments, timeout=60):
    return run(["git", "-C", str(CHECKOUT), *arguments], timeout=timeout)


def compose(*arguments, timeout=120):
    command = ["docker", "compose", "--project-name", PROJECT]
    for path in CONFIG_FILES:
        command.extend(["-f", str(path)])
    return run([*command, *arguments], timeout=timeout)


def container():
    ids = run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=sps",
               "--filter", "label=com.docker.compose.service=sps"]).splitlines()
    result = []
    for container_id in ids:
        data = json.loads(run(["docker", "inspect", "--format", INSPECT_FORMAT, container_id]))
        if str(data["labels"].get("com.docker.compose.oneoff", "false")).lower() != "true":
            result.append(data)
    require(len(result) == 1, "Expected exactly one existing SPS service container.")
    return result[0]


def data_volume(info):
    mounts = [m for m in info["mounts"] if m["Destination"] == "/app/data"]
    require(len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0].get("RW"), "SPS /app/data must be one persistent writable Docker volume.")
    name = mounts[0].get("Name", "")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", name), "Unexpected data volume identity.")
    return name


def validate_container(info, volume=None):
    labels = info["labels"]
    require(labels.get("com.docker.compose.project") == PROJECT and labels.get("com.docker.compose.service") == SERVICE,
            "Container does not belong to the SPS project/service.")
    require(labels.get("com.docker.compose.project.working_dir") == str(CHECKOUT), "Unexpected SPS Compose working directory.")
    require(labels.get("com.docker.compose.project.config_files", "").split(",") == [str(p) for p in CONFIG_FILES],
            "Unexpected Compose files; inspect the existing deployment before continuing.")
    require(info["image_ref"] in {"sps-sps", IMAGE_TAG}, "Unexpected SPS image tag; do not replace an unrelated image.")
    require("sps-proxy" in info["networks"] and "sps-receiver" in (info["networks"]["sps-proxy"].get("Aliases") or []),
            "SPS must retain its existing proxy network and alias.")
    actual = data_volume(info)
    if volume is not None:
        require(actual == volume, "The data volume identity changed; deployment must stop.")
    return actual


def origin_is_sps(origin):
    if origin == "git@github.com:mpriesol/sps.git":
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in {"https", "ssh"} and parsed.hostname == "github.com" and parsed.path.rstrip("/") in {"/mpriesol/sps", "/mpriesol/sps.git"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch(url, limit=8_000_000, timeout=5):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        require(response.status == 200, "Public SPS endpoint did not return HTTP 200.")
        data = response.read(limit + 1)
        require(len(data) <= limit, "Unexpectedly large public SPS response.")
        return data


def env_fingerprint():
    path = CHECKOUT / "sps.env"
    require(path.is_file() and not path.is_symlink(), "Expected the existing regular sps.env file.")
    return hashlib.sha256(path.read_bytes()).digest()


def preflight(revision):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Use the exact full lowercase 40-character SPS main commit.")
    require(CHECKOUT.is_dir() and not CHECKOUT.is_symlink(), "Expected the existing /opt/sps directory.")
    info = container()
    volume = validate_container(info)
    require(info["running"] and info["health"] == "healthy", "Existing SPS must be running and healthy before replacement.")
    run(["docker", "network", "inspect", "sps-proxy", "--format", "{{.Name}}"])
    volume_labels = json.loads(run(["docker", "volume", "inspect", volume, "--format", "{{json .Labels}} "])) or {}
    require(volume_labels.get("com.docker.compose.project") == PROJECT and volume_labels.get("com.docker.compose.volume") == "courier_passwords",
            "Data volume does not have the expected SPS ownership labels.")
    require(git("rev-parse", "--show-toplevel") == str(CHECKOUT), "Unexpected Git checkout root.")
    require(origin_is_sps(git("remote", "get-url", "origin")), "Git origin must be the existing mpriesol/sps repository.")
    require(git("symbolic-ref", "--short", "HEAD") == "main", "Existing server checkout must be on main.")
    require(not git("status", "--porcelain", "--untracked-files=no"), "Tracked server checkout has local changes; refusing to replace them.")
    remote = git("ls-remote", "--heads", "origin", "refs/heads/main", timeout=60).split()
    require(remote == [revision, "refs/heads/main"], "Requested commit is not the current SPS origin/main.")
    compose("config", "--quiet")
    require(json.loads(fetch(PUBLIC + "healthz", limit=4096)).get("status") == "ok", "Public SPS health check failed.")
    return {"container": info, "volume": volume, "head": git("rev-parse", "HEAD"), "env": env_fingerprint()}


def isolated_image_command(image, code, *mounts):
    command = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--entrypoint", "python"]
    for mount in mounts:
        command.extend(["--mount", mount])
    return [*command, image, "-c", code]


def manifest(image):
    return json.loads(run(isolated_image_command(image, MANIFEST_CODE), timeout=30))


def backup_volume(old_image, volume, directory):
    temporary = directory / "data.tar.gz.partial"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        run(isolated_image_command(old_image, BACKUP_CODE, f"type=volume,src={volume},dst=/source,readonly"), stdout=output, timeout=240)
        output.flush()
        os.fsync(output.fileno())
    # Read every archived byte while stopped; don't expose names or contents.
    with tarfile.open(temporary, "r:gz") as archive:
        for member in archive:
            if member.isfile():
                source = archive.extractfile(member)
                while source.read(1024 * 1024):
                    pass
    temporary.replace(directory / "data.tar.gz")


def wait_ready(image, volume, expected_manifest, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            info = container()
            validate_container(info, volume)
            require(info["image"] == image and info["running"] and info["health"] == "healthy", "SPS image is not healthy yet.")
            require(json.loads(fetch(PUBLIC + "healthz", limit=4096)).get("status") == "ok", "SPS health failed.")
            require(hashlib.sha256(fetch(PUBLIC)).hexdigest() == expected_manifest["index"], "Public SPS HTML does not match the running image.")
            for asset, digest in expected_manifest["assets"].items():
                require(hashlib.sha256(fetch(PUBLIC + asset)).hexdigest() == digest, "Public SPS asset does not match the running image.")
            return
        except (DeploymentError, OSError, ValueError):
            time.sleep(3)
    raise DeploymentError("SPS image, volume, public health and frontend verification did not all pass within the readiness window.")


def apply(revision, before, directory):
    old_image = before["container"]["image"]
    old_manifest = manifest(old_image)
    rollback_tag = "sps-sps:rollback-" + directory.name
    service_may_be_stopped = False
    run(["docker", "image", "tag", old_image, rollback_tag])
    try:
        phase("fetching and verifying the exact merged SPS main commit")
        git("fetch", "--no-tags", "origin", "main", timeout=120)
        require(git("rev-parse", "FETCH_HEAD") == revision, "SPS main moved; refusing to deploy a different commit.")
        git("merge-base", "--is-ancestor", before["head"], revision)
        # This application release does not change production Compose semantics.
        for path in CONFIG_FILES:
            require(git("diff", before["head"], revision, "--", path.name) == "", "Compose configuration changed; this helper requires a separate review.")
        git("merge", "--ff-only", revision)
        require(git("rev-parse", "HEAD") == revision and env_fingerprint() == before["env"], "Checkout or server environment changed unexpectedly.")
        phase("building the new SPS image while the existing service stays running")
        # Only bytes from the reviewed commit enter the build. Untracked server
        # files (including sps.env) cannot leak into the context or change code.
        tracked = git("ls-tree", "-r", "--name-only", revision).splitlines()
        require("sps.env" not in tracked and not any(p.startswith("data/") for p in tracked), "The release unexpectedly tracks private server data.")
        source_archive = directory / "source.tar"
        git("archive", "--format=tar", "--output", str(source_archive), revision)
        with source_archive.open("rb") as source:
            run(["docker", "build", "--label", f"{REVISION_LABEL}={revision}", "--tag", IMAGE_TAG, "-"], stdin=source, timeout=900)
        new_image = run(["docker", "image", "inspect", IMAGE_TAG, "--format", "{{.Id}}"])
        label = run(["docker", "image", "inspect", new_image, "--format", '{{index .Config.Labels "org.opencontainers.image.revision"}}'])
        require(label == revision, "Built image does not identify the requested commit.")
        new_manifest = manifest(new_image)
        current = container()
        require(current["id"] == before["container"]["id"] and current["image"] == old_image and current["running"], "SPS changed during the build; refusing to stop a different deployment.")
        validate_container(current, before["volume"])
        phase("gracefully stopping SPS for a consistent private data-volume backup")
        service_may_be_stopped = True
        run(["docker", "stop", "--time", "60", current["id"]], timeout=75)
        stopped = container()
        require(not stopped["running"] and not stopped["oom_killed"] and stopped["exit_code"] in {0, 143}, "SPS did not stop gracefully; do not proceed with the upgrade.")
        backup_volume(old_image, before["volume"], directory)
        phase("backup verified; starting only the new SPS service")
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "--timeout", "60", SERVICE, timeout=120)
        wait_ready(new_image, before["volume"], new_manifest)
        require(env_fingerprint() == before["env"], "Existing sps.env changed during deployment.")
        phase("verified exact image revision, unchanged data volume, public health and new frontend assets")
        print(json.dumps({"deployed": True, "revision": revision, "image": new_image, "service": SERVICE, "backup": "private server backup retained"}), flush=True)
    except BaseException:
        phase("deployment failed; retaining data and restoring the previous SPS image")
        try:
            run(["docker", "image", "tag", old_image, IMAGE_TAG])
            if service_may_be_stopped:
                compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", "--timeout", "60", SERVICE, timeout=120)
                wait_ready(old_image, before["volume"], old_manifest)
                phase("previous SPS image restored; all current data retained without restoring an older backup")
            else:
                phase("existing SPS container was not stopped")
        except BaseException:
            phase("ROLLBACK FAILED; SPS requires operator attention; current data and private backup are retained")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("revision", help="Exact full 40-character commit currently on mpriesol/sps main")
    args = parser.parse_args()
    phase("read-only SPS preflight")
    before = preflight(args.revision)
    phase("preflight passed: existing SPS checkout, environment, volume, proxy network and public health verified")
    if args.check:
        return
    require(not PRIVATE_STATE.is_symlink(), "Private deployment state must not be a symlink.")
    PRIVATE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PRIVATE_STATE, 0o700)
    lock_fd = os.open(PRIVATE_STATE / "deployment.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("Another SPS deployment is already active.") from exc
        before = preflight(args.revision)
        directory = PRIVATE_STATE / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.revision[:12])
        directory.mkdir(mode=0o700)
        def interrupted(signum, frame):
            raise DeploymentError("Deployment interrupted; attempting the previous image recovery.")
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, interrupted)
        apply(args.revision, before, directory)


if __name__ == "__main__":
    try:
        main()
    except (DeploymentError, OSError, ValueError) as exc:
        # Only controlled messages are printed. Never dump subprocess output.
        message = str(exc) if isinstance(exc, DeploymentError) else "Local deployment prerequisite failed; server configuration output withheld."
        print("SPS_DEPLOY_FAILED " + message, file=sys.stderr, flush=True)
        sys.exit(1)
