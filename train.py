"""
Website Performance Optimizer — main loop.
Analogous to karpathy/autoresearch train.py, but instead of minimising val_bpb
on a language model, this maximises the Lighthouse performance score of a
static website by having Claude autonomously edit HTML / CSS / JS.

Metric  : Lighthouse Performance Score  (0–100, higher is better)
Edits   : index.html, styles.css, script.js  in WEBSITE_DIR
Keeps   : a change only if it improves score by > MIN_GAIN
Reverts : everything else
Logs    : optimization_log.json  (full history)

Run with: python train.py
"""

import os, json, re, time, threading, subprocess
import http.server, socketserver
from pathlib import Path
from datetime import datetime
import anthropic

# ──────────────────────────────────────────────
# CONFIG  (edit these if you need to)
# ──────────────────────────────────────────────
WEBSITE_DIR    = Path(os.environ.get("WEBSITE_DIR", "/content/personal-website"))
PORT           = 8787
LH_RUNS        = 3          # Lighthouse passes averaged per measurement
MIN_GAIN       = 0.5        # minimum score improvement to keep a change
PUSH_CHANGES   = True       # git-push improvements to GitHub (needs GITHUB_TOKEN)
MAX_EXPERIMENTS = 100       # stop after this many experiments
MODEL          = "claude-opus-4-7"
LOG_FILE       = Path(__file__).parent / "optimization_log.json"

# Only these files may be modified by the agent
EDITABLE       = ["index.html", "styles.css", "script.js"]

# Lighthouse audits to capture in the log
LH_AUDITS      = [
    "first-contentful-paint",
    "largest-contentful-paint",
    "total-blocking-time",
    "cumulative-layout-shift",
    "speed-index",
]


# ──────────────────────────────────────────────
# LOCAL HTTP SERVER
# ──────────────────────────────────────────────
class _Silent(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_): pass

def _serve(directory: Path):
    os.chdir(directory)
    with socketserver.TCPServer(("", PORT), _Silent) as httpd:
        httpd.serve_forever()

def start_server():
    threading.Thread(target=_serve, args=(WEBSITE_DIR,), daemon=True).start()
    time.sleep(1.5)
    print(f"  Serving {WEBSITE_DIR.name} at http://localhost:{PORT}")


# ──────────────────────────────────────────────
# MEASUREMENT
# ──────────────────────────────────────────────
def run_lighthouse() -> tuple[float, dict]:
    """Run Lighthouse once. Returns (score_0_to_100, audit_metrics)."""
    result = subprocess.run(
        [
            "lighthouse",
            f"http://localhost:{PORT}/index.html",
            "--output=json",
            "--quiet",
            "--only-categories=performance",
            "--chrome-flags=--headless --no-sandbox --disable-gpu --disable-dev-shm-usage",
        ],
        capture_output=True, text=True, timeout=120,
    )
    data    = json.loads(result.stdout)
    score   = data["categories"]["performance"]["score"] * 100
    metrics = {k: data["audits"][k]["numericValue"] for k in LH_AUDITS}
    return score, metrics

def measure() -> tuple[float, dict]:
    """Average LH_RUNS Lighthouse passes. Returns (avg_score, avg_metrics)."""
    scores, acc = [], {}
    for i in range(LH_RUNS):
        try:
            s, m = run_lighthouse()
            scores.append(s)
            for k, v in m.items():
                acc.setdefault(k, []).append(v)
            print(f"    run {i+1}/{LH_RUNS}: {s:.1f}")
        except Exception as e:
            print(f"    run {i+1}/{LH_RUNS}: failed ({e})")

    if not scores:
        raise RuntimeError("All Lighthouse runs failed.")

    avg_score   = sum(scores) / len(scores)
    avg_metrics = {k: sum(v) / len(v) for k, v in acc.items()}
    return avg_score, avg_metrics


# ──────────────────────────────────────────────
# FILE I/O
# ──────────────────────────────────────────────
def read_files() -> dict[str, str]:
    return {f: (WEBSITE_DIR / f).read_text() for f in EDITABLE if (WEBSITE_DIR / f).exists()}

def backup_files() -> dict[str, str]:
    return read_files()

def restore_files(snapshot: dict[str, str]):
    for name, body in snapshot.items():
        (WEBSITE_DIR / name).write_text(body)

def apply_changes(response: str) -> list[str]:
    """Parse <FILE name="…">…</FILE> blocks and write to WEBSITE_DIR."""
    changed = []
    for m in re.finditer(r'<FILE name="([^"]+)">(.*?)</FILE>', response, re.DOTALL):
        fname, body = m.group(1).strip(), m.group(2).strip()
        if fname in EDITABLE:
            (WEBSITE_DIR / fname).write_text(body)
            changed.append(fname)
    return changed

def file_sizes() -> dict[str, int]:
    return {f: (WEBSITE_DIR / f).stat().st_size for f in EDITABLE if (WEBSITE_DIR / f).exists()}


# ──────────────────────────────────────────────
# GIT  (push improvements back to GitHub)
# ──────────────────────────────────────────────
def git_push(exp: int, score_before: float, score_after: float):
    delta = score_after - score_before
    msg   = f"perf: experiment {exp:03d}  {score_before:.1f} → {score_after:.1f}  (+{delta:.1f} pts)"
    cmds  = [
        f"git -C {WEBSITE_DIR} add {' '.join(EDITABLE)}",
        f'git -C {WEBSITE_DIR} commit -m "{msg}"',
        f"git -C {WEBSITE_DIR} push",
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [git warn] {r.stderr.strip()[:120]}")
            break


# ──────────────────────────────────────────────
# AGENT
# ──────────────────────────────────────────────
def call_agent(best_score: float, history: list, files: dict[str, str]) -> str:
    program  = (Path(__file__).parent / "program.md").read_text()
    recent   = json.dumps(history[-6:], indent=2) if history else "none yet"
    files_block = "\n\n".join(f"=== {n} ===\n{b}" for n, b in files.items())

    prompt = f"""{program}

## Current snapshot
Performance score : {best_score:.1f} / 100   (higher is better)
File sizes (bytes): {json.dumps(file_sizes())}

## Recent experiment history  (last 6)
{recent}

## Current file contents
{files_block}

## Your task
Study the history. Choose ONE untried, high-impact optimisation.
Output COMPLETE modified file(s) in XML tags, then one sentence of explanation.
"""
    resp = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ──────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────
def main():
    print("=== Website Performance Optimizer ===\n")
    print(f"Website : {WEBSITE_DIR}")
    print(f"Model   : {MODEL}")
    print(f"Push    : {'yes (git push on improvement)' if PUSH_CHANGES else 'no'}\n")

    start_server()

    # Baseline
    print("Measuring baseline …")
    baseline_score, baseline_metrics = measure()
    print(f"\nBaseline: {baseline_score:.1f} / 100")
    for k, v in baseline_metrics.items():
        print(f"  {k:<40} {v:>8.0f} ms" if "shift" not in k else f"  {k:<40} {v:>8.4f}")

    log = {
        "started":     datetime.now().isoformat(),
        "website":     str(WEBSITE_DIR),
        "baseline":    {"score": round(baseline_score, 2), "metrics": {k: round(v, 2) for k, v in baseline_metrics.items()}},
        "experiments": [],
    }

    history    = []
    best_score = baseline_score
    exp        = 0

    print(f"\nStarting optimisation loop …  (max {MAX_EXPERIMENTS} experiments)\n")

    while exp < MAX_EXPERIMENTS:
        exp += 1
        print(f"── Experiment {exp:03d}  │  best so far: {best_score:.1f} ──")

        snapshot = backup_files()

        # Ask the agent for one optimisation
        print("  Agent thinking …")
        t0       = time.time()
        response = call_agent(best_score, history, read_files())
        elapsed  = time.time() - t0
        print(f"  Agent responded in {elapsed:.1f}s")

        changed = apply_changes(response)
        if not changed:
            print("  No file changes in response — skipping.\n")
            continue
        print(f"  Changed: {changed}")

        # Measure new score
        print("  Measuring …")
        try:
            new_score, new_metrics = measure()
        except RuntimeError as e:
            print(f"  Measurement failed: {e} — reverting.")
            restore_files(snapshot)
            continue

        delta    = new_score - best_score
        improved = delta >= MIN_GAIN

        entry = {
            "experiment":    exp,
            "timestamp":     datetime.now().isoformat(),
            "files_changed": changed,
            "score_before":  round(best_score, 2),
            "score_after":   round(new_score,  2),
            "delta":         round(delta,       2),
            "kept":          improved,
            "metrics":       {k: round(v, 2) for k, v in new_metrics.items()},
        }

        if improved:
            best_score = new_score
            print(f"  IMPROVED  {delta:+.1f}  →  {best_score:.1f} / 100  ✓")
            if PUSH_CHANGES:
                git_push(exp, entry["score_before"], best_score)
                print("  Pushed to GitHub.")
        else:
            print(f"  No improvement  ({delta:+.1f}) — reverting.")
            restore_files(snapshot)

        history.append(entry)
        log["experiments"].append(entry)
        LOG_FILE.write_text(json.dumps(log, indent=2))
        print()

    print(f"Done! Ran {MAX_EXPERIMENTS} experiments. Best score: {best_score:.1f} / 100")
    print(f"Full log saved to {LOG_FILE}")


if __name__ == "__main__":
    main()
