"""
One-time setup for the website performance optimizer.
No browser or Lighthouse required — measurement is pure static file analysis.

Run once before train.py:
    python prepare.py
"""

import os, sys, subprocess
from pathlib import Path

WEBSITE_REPO = "https://github.com/03shraddha/personal-website.git"
WEBSITE_DIR  = os.environ.get("WEBSITE_DIR", "/content/personal-website")


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

    step(1, "Install Python dependencies")
    run("pip install anthropic -q")

    step(2, f"Clone / update personal-website → {WEBSITE_DIR}")
    if Path(WEBSITE_DIR).exists():
        print("  Directory exists — pulling latest.")
        run(f"git -C {WEBSITE_DIR} pull --ff-only")
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        url   = WEBSITE_REPO.replace("https://", f"https://{token}@") if token else WEBSITE_REPO
        run(f"git clone {url} {WEBSITE_DIR}")

    step(3, "Configure git identity (needed for committing improvements)")
    run(f'git -C {WEBSITE_DIR} config user.email "optimizer@autoresearch"')
    run(f'git -C {WEBSITE_DIR} config user.name  "Autoresearch Optimizer"')

    step(4, "Smoke-test: measure baseline load_score")
    sys.path.insert(0, str(Path(__file__).parent))
    os.environ.setdefault("WEBSITE_DIR", WEBSITE_DIR)
    from train import measure
    score, metrics = measure()
    print(f"\n  Baseline load_score : {score:.1f}  (lower is better)")
    for k, v in metrics.items():
        print(f"    {k:<25} {v}")

    print("\nDone! Ready to train.")
    print("Next step: python train.py")


if __name__ == "__main__":
    main()
