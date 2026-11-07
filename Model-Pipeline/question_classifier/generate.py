"""Generate synthetic training and validation questions from the templates.

Each example: {"text", "labels" (list), "audience", "split"}, plus "oos_kind" ("advice" or
"unrelated") when out_of_scope is one of the labels.

Split by name and by wording:
  - the device, company and product-code values are divided into two disjoint pools, so a
    validation question never names a device, company or code seen in training (generic
    device types are shared);
  - about a quarter of each label's templates are held out for validation, so validation
    measures phrasings the model has never seen, not just new names.

The hand-written test set is NOT generated here: see data/test/README.md.

Usage (from Model-Pipeline/):
    python -m question_classifier.generate --n-train 4000 --n-val 800
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .labels import LABELS
from .templates import CLOSERS, JOINERS, OPENERS, TEMPLATES, UNRELATED

SLOTS_FILE = Path(__file__).with_name("slots.json")
OUT_DIR = Path("data/generated")
SPLIT_SLOTS = ("device", "company", "code")
VAL_TEMPLATE_SHARE = 0.25
MULTI_LABEL_SHARE = 0.25     # share of questions that combine two labels


def load_slots(path: Path = SLOTS_FILE) -> dict[str, list[str]]:
    data = json.loads(path.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def split_pools(slots: dict, val_share: float, seed: int) -> dict[str, dict]:
    """{"train": slots, "val": slots} with SPLIT_SLOTS divided into disjoint pools."""
    rng = random.Random(seed)
    pools = {"train": dict(slots), "val": dict(slots)}
    for key in SPLIT_SLOTS:
        values = sorted(set(slots[key]))
        rng.shuffle(values)
        n_val = max(2, round(len(values) * val_share))
        pools["val"][key], pools["train"][key] = values[:n_val], values[n_val:]
    return pools


def split_templates(templates: dict, val_share: float, seed: int) -> dict[str, dict]:
    """{"train": templates, "val": templates}: per label, a disjoint held-out share for val
    (at least one template each side)."""
    rng = random.Random(f"{seed}-templates")
    out = {"train": {}, "val": {}}
    for label, items in templates.items():
        items = list(items)
        rng.shuffle(items)
        n_val = min(len(items) - 1, max(1, round(len(items) * val_share)))
        out["val"][label], out["train"][label] = items[:n_val], items[n_val:]
    return out


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


def make_example(slots: dict, rng: random.Random, templates: dict = TEMPLATES) -> dict:
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
    audience = next((a for a in audiences if a != "neutral"), "neutral")
    ex = {"text": add_noise(text, rng), "labels": sorted(picked), "audience": audience}
    if oos_kind:
        ex["oos_kind"] = oos_kind
    return ex


def generate(n_train: int, n_val: int, seed: int = 13, val_share: float = 0.25,
             slots: dict | None = None) -> dict[str, list[dict]]:
    pools = split_pools(slots or load_slots(), val_share, seed)
    tmpl = split_templates(TEMPLATES, VAL_TEMPLATE_SHARE, seed)
    out = {}
    for split, n in (("train", n_train), ("val", n_val)):
        rng = random.Random(f"{seed}-{split}")
        seen, rows = set(), []
        attempts = 0
        while len(rows) < n and attempts < n * 20:
            attempts += 1
            ex = make_example(pools[split], rng, tmpl[split])
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
    ap = argparse.ArgumentParser(description="Generate synthetic train/val questions.")
    ap.add_argument("--n-train", type=int, default=4000)
    ap.add_argument("--n-val", type=int, default=800)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    data = generate(a.n_train, a.n_val, a.seed)
    for split, rows in data.items():
        write_jsonl(rows, a.out_dir / f"{split}.jsonl")
        counts = {lbl: sum(lbl in r["labels"] for r in rows) for lbl in LABELS}
        print(f"{split}: {len(rows)} questions, per label {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
