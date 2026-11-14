"""Generate synthetic training, validation and held-out questions from the templates.

Each example: {"text", "labels" (list), "audience", "split"}, plus "oos_kind" ("advice" or
"unrelated") when out_of_scope is one of the labels.

Split by name and by wording, three ways (train 60% / val 20% / heldout 20%):
  - the device, company and product-code values are divided into three disjoint pools, so a
    val or heldout question never names a device, company or code seen in training (generic
    device types are shared);
  - each label's templates are divided the same way (out_of_scope within each kind), so val
    and heldout measure phrasings the model has never seen, not just new names.
heldout is never used for tuning: it was scored once, after the guardrail rules were frozen.

The hand-written test set is NOT generated here: see data/test/README.md.

Usage (from Model-Development/chatbot/):
    python -m query_router.generate
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .labels import LABELS
from .templates import CLOSERS, CONTEXTS, JOINERS, OPENERS, TEMPLATES, UNRELATED

SLOTS_FILE = Path(__file__).with_name("slots.json")
OUT_DIR = Path("data/generated")
SPLITS = ("train", "val", "heldout")
SPLIT_SHARES = {"val": 0.2, "heldout": 0.2}        # the rest is train
SPLIT_SLOTS = ("device", "company", "code")
MULTI_LABEL_SHARE = 0.25     # share of questions that combine two labels
CONTEXT_SHARE = 0.2          # share of questions, of every label, with a context sentence first


def load_slots(path: Path = SLOTS_FILE) -> dict[str, list[str]]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _three_way(items: list, rng: random.Random, min_each: int = 1) -> dict[str, list]:
    """Shuffle and cut items into val / heldout / train (train keeps at least one)."""
    items = list(items)
    rng.shuffle(items)
    n = len(items)
    n_val = max(min_each, round(n * SPLIT_SHARES["val"]))
    n_held = max(min_each, round(n * SPLIT_SHARES["heldout"]))
    if n - n_val - n_held < 1:
        raise ValueError(f"too few items to split three ways: {n}")
    return {"val": items[:n_val], "heldout": items[n_val:n_val + n_held],
            "train": items[n_val + n_held:]}


def split_pools(slots: dict, seed: int) -> dict[str, dict]:
    """Per split, the slot values, with SPLIT_SLOTS divided into disjoint pools."""
    rng = random.Random(seed)
    pools = {sp: dict(slots) for sp in SPLITS}
    for key in SPLIT_SLOTS:
        parts = _three_way(sorted(set(slots[key])), rng, min_each=2)
        for sp in SPLITS:
            pools[sp][key] = parts[sp]
    return pools


def split_templates(templates: dict, seed: int) -> dict[str, dict]:
    """Per split, each label's templates; disjoint across splits. out_of_scope is split
    within each kind (advice / unrelated) so every split has both."""
    rng = random.Random(f"{seed}-templates")
    out = {sp: {} for sp in SPLITS}
    for label, items in templates.items():
        groups = ([[t for t in items if t[1] not in UNRELATED],
                   [t for t in items if t[1] in UNRELATED]]
                  if label == "out_of_scope" else [list(items)])
        for sp in SPLITS:
            out[sp][label] = []
        for group in groups:
            parts = _three_way(group, rng)
            for sp in SPLITS:
                out[sp][label] += parts[sp]
    return out


def split_contexts(contexts=CONTEXTS, seed: int = 13) -> dict[str, list[str]]:
    """Per split, the context sentences; disjoint across splits, like the templates."""
    return _three_way(sorted(contexts), random.Random(f"{seed}-contexts"), min_each=2)


def random_udi(rng: random.Random) -> str:
    return "00" + "".join(rng.choice("0123456789") for _ in range(12))


def fill(template: str, slots: dict, rng: random.Random) -> str:
    device, device2 = rng.sample(slots["device"], 2)
    return template.format(device=device, device2=device2, company=rng.choice(slots["company"]),
                           generic=rng.choice(slots["generic"]), code=rng.choice(slots["code"]),
                           event=rng.choice(slots["event"]), year=rng.choice(slots["year"]),
                           udi=random_udi(rng))


def add_noise(text: str, rng: random.Random) -> str:
    """Openers, closers, casing and the occasional typo, as real users write."""
    text = rng.choice(OPENERS) + text + rng.choice(CLOSERS)
    r = rng.random()
    if r < 0.15:
        text = text.upper()
    elif r < 0.55:
        text = text[:1].upper() + text[1:]
    if rng.random() < 0.15:                      # swap two adjacent letters in one word
        words = text.split(" ")
        idx = [i for i, w in enumerate(words) if len(w) > 4 and w.isalpha()]
        if idx:
            i = rng.choice(idx)
            w = words[i]
            j = rng.randrange(len(w) - 1)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2:]
            text = " ".join(words)
    return text


def make_example(slots: dict, rng: random.Random, templates: dict = TEMPLATES,
                 contexts=CONTEXTS) -> dict:
    n_labels = 2 if rng.random() < MULTI_LABEL_SHARE else 1
    picked = rng.sample(list(LABELS), n_labels)
    parts, audiences, oos_kind = [], [], None
    for label in picked:
        audience, template = rng.choice(templates[label])
        parts.append(fill(template, slots, rng))
        audiences.append(audience)
        if label == "out_of_scope":
            oos_kind = "unrelated" if template in UNRELATED else "advice"
    text = parts[0] if n_labels == 1 else rng.choice(JOINERS).join(parts)
    if contexts and rng.random() < CONTEXT_SHARE:   # drawn after the labels: same rate for all
        text = rng.choice(contexts) + text
    audience = next((a for a in audiences if a != "neutral"), "neutral")
    ex = {"text": add_noise(text, rng), "labels": sorted(picked), "audience": audience}
    if oos_kind:
        ex["oos_kind"] = oos_kind
    return ex


def generate(n_train: int, n_val: int, n_heldout: int = 0, seed: int = 13,
             slots: dict | None = None) -> dict[str, list[dict]]:
    pools = split_pools(slots or load_slots(), seed)
    tmpl = split_templates(TEMPLATES, seed)
    ctx = split_contexts(CONTEXTS, seed)
    out = {}
    for split, n in (("train", n_train), ("val", n_val), ("heldout", n_heldout)):
        if n == 0:
            continue
        rng = random.Random(f"{seed}-{split}")
        seen, rows = set(), []
        attempts = 0
        while len(rows) < n and attempts < n * 20:
            attempts += 1
            ex = make_example(pools[split], rng, tmpl[split], ctx[split])
            key = ex["text"].lower()
            if key in seen:
                continue
            seen.add(key)
            rows.append({**ex, "split": split})
        out[split] = rows
    return out


def write_jsonl(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate synthetic train/val/heldout questions.")
    ap.add_argument("--n-train", type=int, default=4000)
    ap.add_argument("--n-val", type=int, default=1000)
    ap.add_argument("--n-heldout", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    data = generate(a.n_train, a.n_val, a.n_heldout, a.seed)
    for split, rows in data.items():
        write_jsonl(rows, a.out_dir / f"{split}.jsonl")
        counts = {lbl: sum(lbl in r["labels"] for r in rows) for lbl in LABELS}
        print(f"{split}: {len(rows)} questions, per label {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
