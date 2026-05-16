# autoresearch - website edition

This is karpathy's [autoresearch](https://github.com/karpathy/autoresearch) loop applied to web performance instead of LLM training.

The original loop minimises `val_bpb` (language model perplexity) by having Claude rewrite model code, one experiment at a time. This version minimises `load_score` (a proxy for page load time) by having Claude rewrite HTML, CSS, and JS.

## How it works

Every iteration:
1. Measure `load_score` from static file analysis (no browser needed)
2. Ask Claude to make one targeted optimisation
3. If `load_score` drops by more than 5 points, keep it and push to GitHub
4. Otherwise revert and try something else
5. Repeat up to 100 times

## The metric

```
load_score = total_asset_kb
           + 50 * render-blocking scripts in <head>
           + 10 * CDN origins missing a preconnect hint
           +  5 * images missing width/height
```

Lower is better, exactly like `val_bpb`.

## Setup

```bash
# on Google Colab (recommended)
git clone https://github.com/03shraddha/autoresearch.git
cd autoresearch

export ANTHROPIC_API_KEY="your-key"
export GITHUB_TOKEN="your-token"

python prepare.py   # clones personal-website, smoke-tests measurement
python train.py     # runs the loop
```

## Files

| File | What it does |
|------|-------------|
| `train.py` | Main loop: measure, call agent, keep or revert |
| `prepare.py` | One-time setup: clone website repo, configure git |
| `program.md` | System prompt for the Claude agent |
| `pyproject.toml` | Python deps (just `anthropic`) |

## Target repo

Optimises [03shraddha/personal-website](https://github.com/03shraddha/personal-website). Successful experiments are automatically committed and pushed there.
