"""
One-time setup for the website performance optimizer.
Analogous to karpathy/autoresearch prepare.py (data prep + runtime check).

Run once on Colab before train.py:
    python prepare.py
"""

import os, sys, subprocess
from pathlib import Path

WEBSITE_REPO  = "https://github.com/03shraddha/personal-website.git"
WEBSITE_DIR   = os.environ.get("WEBSITE_DIR", "/content/personal-website")


def run(cmd: str, check: bool = True) -> int:
    print(f"  $ {cmd}")
    r = subprocess.run(cmd, shell=True, check=False)
    if check and r.returncode != 0:
        print(f"  ERROR: command failed (exit {r.returncode})")
        sys.exit(r.returncode)
    return r.returncode


def step(n: int, title: str):
    print(f"\n[{n}] {title}")


def main():
    print("=== Website Performance Optimizer — Setup ===")

    step(1, "Install Chromium (needed by Lighthouse)")
    run("apt-get install -y chromium-browser > /dev/null 2>&1", check=False)
    # fallback name on some Colab images
    if run("which chromium-browser > /dev/null 2>&1", check=False) != 0:
        run("apt-get install -y chromium > /dev/null 2>&1", check=False)

    step(2, "Install Lighthouse (Node.js CLI)")
    run("npm install -g lighthouse > /dev/null 2>&1")
    rc = run("lighthouse --version", check=False)
    if rc != 0:
        print("  ERROR: lighthouse not found after install — check Node.js.")
        sys.exit(1)

    step(3, "Install Python dependencies")
    run("pip install anthropic -q")

    step(4, f"Clone / update personal-website → {WEBSITE_DIR}")
    if Path(WEBSITE_DIR).exists():
        print(f"  Directory exists — pulling latest.")
        run(f"git -C {WEBSITE_DIR} pull --ff-only")
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            url = WEBSITE_REPO.replace("https://", f"https://{token}@")
        else:
            url = WEBSITE_REPO
        run(f"git clone {url} {WEBSITE_DIR}")

    step(5, "Configure git identity (needed for committing improvements)")
    run(f'git -C {WEBSITE_DIR} config user.email "optimizer@autoresearch"')
    run(f'git -C {WEBSITE_DIR} config user.name  "Autoresearch Optimizer"')

    step(6, "Smoke-test: start server + run one Lighthouse pass")
    import threading, http.server, socketserver, time, json

    class _Silent(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass

    os.chdir(WEBSITE_DIR)
    with socketserver.TCPServer(("", 8787), _Silent) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(1)
        result = subprocess.run(
            ["lighthouse", "http://localhost:8787/index.html",
             "--output=json", "--quiet", "--only-categories=performance",
             "--chrome-flags=--headless --no-sandbox --disable-gpu --disable-dev-shm-usage"],
            capture_output=True, text=True, timeout=120,
        )
        httpd.shutdown()

    try:
        data  = json.loads(result.stdout)
        score = data["categories"]["performance"]["score"] * 100
        print(f"\n  Baseline Lighthouse score: {score:.1f} / 100")
    except Exception:
        print("  WARNING: Could not parse Lighthouse output.")
        print("  stdout:", result.stdout[:300])
        print("  stderr:", result.stderr[:300])
        print("  Setup may still work — try running train.py anyway.")

    print("\nDone! Ready to train.")
    print("Next step: python train.py")


if __name__ == "__main__":
    main()
