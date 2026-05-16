"""
autoresearch — website page-load edition.

karpathy/autoresearch runs a tight loop:
  measure val_bpb  →  agent edits model code  →  keep if better, revert if not  →  repeat

This file runs the same loop, but the target is page load time instead of language
model perplexity.  The metric is load_score (lower is better, directly analogous to
val_bpb): a static-analysis proxy for real-world load performance, computable from
raw HTML/CSS/JS without a browser.

  load_score = total_asset_kb                          (transfer cost)
             + 50 × render-blocking scripts in <head>  (blocks first paint)
             + 10 × CDN origins missing a preconnect   (extra DNS + TLS round-trips)
             +  5 × images missing width/height         (causes layout shift / CLS)

Each iteration, Claude proposes one targeted edit.  If load_score drops by ≥ MIN_GAIN
the change is kept and git-pushed; otherwise the files are restored and the loop
continues.  100 experiments cap the run.

Edits   : index.html, styles.css, script.js  (in WEBSITE_DIR)
Keeps   : change only if load_score drops by > MIN_GAIN
Reverts : everything else
Logs    : optimization_log.json

Run with: python train.py
"""

import os, json, re, time
from pathlib import Path
from datetime import datetime
import anthropic

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
WEBSITE_DIR     = Path(os.environ.get("WEBSITE_DIR", "/content/personal-website"))
MIN_GAIN        = 5.0        # minimum load_score drop to keep a change
MAX_EXPERIMENTS = 100        # stop after this many experiments
PUSH_CHANGES    = True       # git-push improvements to GitHub (needs GITHUB_TOKEN)
MODEL           = "claude-opus-4-7"
LOG_FILE        = Path(__file__).parent / "optimization_log.json"

# Only these files may be modified by the agent
EDITABLE        = ["index.html", "styles.css", "script.js"]


# ──────────────────────────────────────────────
# MEASUREMENT  (pure static analysis — no browser needed)
# ──────────────────────────────────────────────
def measure() -> tuple[float, dict]:
    """
    Compute load_score from static file analysis.
    Lower is better — analogous to val_bpb.

    Score = asset_kb
          + 50 * blocking_scripts   (render-blocking <script> in <head>)
          + 10 * missing_preconnects (CDN origins with no preconnect hint)
          +  5 * cls_risk_images    (images without explicit width+height)
    """
    html_text = (WEBSITE_DIR / "index.html").read_text(encoding="utf-8")
    css_text  = (WEBSITE_DIR / "styles.css").read_text(encoding="utf-8")
    js_text   = (WEBSITE_DIR / "script.js").read_text(encoding="utf-8")

    html_kb  = len(html_text.encode()) / 1024
    css_kb   = len(css_text.encode())  / 1024
    js_kb    = len(js_text.encode())   / 1024
    total_kb = html_kb + css_kb + js_kb

    # Extract <head> content for resource analysis
    head_match = re.search(r"<head>(.*?)</head>", html_text, re.DOTALL | re.IGNORECASE)
    head = head_match.group(1) if head_match else ""

    # Render-blocking scripts: <script src=...> in <head> without defer or async
    head_scripts = re.findall(r"<script[^>]+src=[^>]+>", head, re.IGNORECASE)
    blocking = sum(
        1 for s in head_scripts
        if "defer" not in s.lower() and "async" not in s.lower()
    )

    # External origins referenced in <head> — extract scheme+host only
    def origin(url: str) -> str:
        m = re.match(r"https?://[^/\"'\s>]+", url)
        return m.group(0) if m else ""

    all_refs    = re.findall(r'''(?:href|src)=["']([^"'\s>]+)["']''', head, re.IGNORECASE)
    cdn_origins = {origin(r) for r in all_refs if r.startswith("http")} - {""}

    # Preconnected origins: iterate every <link> tag and check for rel=preconnect
    preconnected_origins = set()
    for link_tag in re.findall(r"<link[^>]+>", head, re.IGNORECASE):
        if "preconnect" in link_tag.lower():
            href = re.search(r'''href=["']([^"'\s>]+)["']''', link_tag, re.IGNORECASE)
            if href:
                o = origin(href.group(1))
                if o:
                    preconnected_origins.add(o)

    missing_preconnects = max(0, len(cdn_origins - preconnected_origins))

    # Images missing explicit width or height (causes CLS)
    all_imgs = re.findall(r"<img[^>]+>", html_text, re.IGNORECASE)
    cls_risk = sum(
        1 for img in all_imgs
        if "width" not in img.lower() or "height" not in img.lower()
    )

    score = total_kb + (blocking * 50) + (missing_preconnects * 10) + (cls_risk * 5)

    metrics = {
        "load_score":          round(score, 2),
        "total_kb":            round(total_kb, 1),
        "html_kb":             round(html_kb, 1),
        "css_kb":              round(css_kb, 1),
        "js_kb":               round(js_kb, 1),
        "blocking_scripts":    blocking,
        "missing_preconnects": missing_preconnects,
        "cls_risk_images":     cls_risk,
    }
    return round(score, 2), metrics


# ──────────────────────────────────────────────
# FILE I/O
# ──────────────────────────────────────────────
def read_files() -> dict[str, str]:
    return {f: (WEBSITE_DIR / f).read_text(encoding="utf-8")
            for f in EDITABLE if (WEBSITE_DIR / f).exists()}

def restore_files(snapshot: dict[str, str]):
    for name, body in snapshot.items():
        (WEBSITE_DIR / name).write_text(body, encoding="utf-8")

def apply_changes(response: str) -> list[str]:
    """Parse <FILE name="…">…</FILE> blocks and write to WEBSITE_DIR."""
    changed = []
    for m in re.finditer(r'<FILE name="([^"]+)">(.*?)</FILE>', response, re.DOTALL):
        fname, body = m.group(1).strip(), m.group(2).strip()
        if fname in EDITABLE:
            (WEBSITE_DIR / fname).write_text(body, encoding="utf-8")
            changed.append(fname)
    return changed


# ──────────────────────────────────────────────
# GIT  (push improvements back to GitHub)
# ──────────────────────────────────────────────
def git_push(exp: int, score_before: float, score_after: float):
    import subprocess
    delta = score_before - score_after
    msg   = f"perf: experiment {exp:03d}  score {score_before:.1f} → {score_after:.1f}  (-{delta:.1f})"
    for cmd in [
        f"git -C {WEBSITE_DIR} add {' '.join(EDITABLE)}",
        f'git -C {WEBSITE_DIR} commit -m "{msg}"',
        f"git -C {WEBSITE_DIR} push",
    ]:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  [git warn] {r.stderr.strip()[:120]}")
            break


# ──────────────────────────────────────────────
# AGENT
# ──────────────────────────────────────────────
def call_agent(best_score: float, history: list, files: dict[str, str]) -> str:
    program    = (Path(__file__).parent / "program.md").read_text()
    recent     = json.dumps(history[-6:], indent=2) if history else "none yet"
    files_block = "\n\n".join(f"=== {n} ===\n{b}" for n, b in files.items())

    prompt = f"""{program}

## Current snapshot
load_score : {best_score:.1f}  (lower is better)
breakdown  : {json.dumps(measure()[1], indent=2)}

## Recent experiment history  (last 6)
{recent}

## Current file contents
{files_block}

## Your task
Study the history. Choose ONE untried, high-impact optimisation to reduce the load_score.
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
    print(f"Max exp : {MAX_EXPERIMENTS}")
    print(f"Push    : {'yes' if PUSH_CHANGES else 'no'}\n")

    # Baseline
    print("Measuring baseline …")
    best_score, baseline_metrics = measure()
    print(f"\nBaseline load_score: {best_score:.1f}  (lower is better)")
    for k, v in baseline_metrics.items():
        print(f"  {k:<25} {v}")

    log = {
        "started":     datetime.now().isoformat(),
        "website":     str(WEBSITE_DIR),
        "baseline":    baseline_metrics,
        "experiments": [],
    }

    history = []
    exp     = 0

    print(f"\nStarting optimisation loop … (max {MAX_EXPERIMENTS} experiments)\n")

    while exp < MAX_EXPERIMENTS:
        exp += 1
        print(f"── Experiment {exp:03d}/{MAX_EXPERIMENTS}  │  best: {best_score:.1f} ──")

        snapshot = read_files()

        # Ask the agent for one optimisation
        print("  Agent thinking …")
        t0       = time.time()
        response = call_agent(best_score, history, snapshot)
        print(f"  Agent responded in {time.time()-t0:.1f}s")

        changed = apply_changes(response)
        if not changed:
            print("  No file changes in response — skipping.\n")
            continue
        print(f"  Changed: {changed}")

        new_score, new_metrics = measure()
        delta    = best_score - new_score   # positive = improvement
        improved = delta >= MIN_GAIN

        print(f"  Metrics: kb={new_metrics['total_kb']} | blocking={new_metrics['blocking_scripts']} | preconnect_missing={new_metrics['missing_preconnects']} | cls={new_metrics['cls_risk_images']}")

        entry = {
            "experiment":    exp,
            "timestamp":     datetime.now().isoformat(),
            "files_changed": changed,
            "score_before":  best_score,
            "score_after":   new_score,
            "delta":         round(delta, 2),
            "kept":          improved,
            "metrics":       new_metrics,
        }

        if improved:
            best_score = new_score
            print(f"  IMPROVED  -{delta:.1f}  →  {best_score:.1f}  ✓")
            if PUSH_CHANGES:
                git_push(exp, entry["score_before"], best_score)
                print("  Pushed to GitHub.")
        else:
            print(f"  No gain  (Δ {-delta:+.1f}) — reverting.")
            restore_files(snapshot)

        history.append(entry)
        log["experiments"].append(entry)
        LOG_FILE.write_text(json.dumps(log, indent=2))
        print()

    print(f"Done! Ran {MAX_EXPERIMENTS} experiments.")
    print(f"Best load_score: {best_score:.1f}  (baseline was {log['baseline']['load_score']:.1f})")
    print(f"Full log: {LOG_FILE}")


if __name__ == "__main__":
    main()
