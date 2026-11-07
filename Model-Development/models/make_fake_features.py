import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
n = 5000

label = rng.choice([1, 0], size=n, p=[0.1, 0.9])
device_id = rng.integers(0, 800, size=n)          # ~6 pairs per device
has_id = rng.choice([1, 0], size=n, p=[0.63, 0.37])  # 37% no usable model number (Q14)

def coin(p):
    """Random 0/1 for each row with probability p (p can differ per row)."""
    return (rng.random(n) < p).astype(int)

id_exact = has_id * coin(np.where(label == 1, 0.85, 0.05))
id_token_only = has_id * (1 - id_exact) * coin(np.where(label == 1, 0.3, 0.02))
raw_equal = id_exact * coin(0.8)
mfr_equal = coin(np.where(label == 1, 0.55, 0.4))
mfr_alias = (1 - mfr_equal) * coin(np.where(label == 1, 0.6, 0.02))
di_count = rng.integers(1, 40, size=n)
product_code = coin(np.where(label == 1, 0.95, 0.5)).astype(float)
product_code[rng.random(n) < 0.2] = np.nan        # 20% unknown

df = pd.DataFrame({
    "pair_id": [f"p{i:05d}" for i in range(n)],
    "device_id": device_id,
    "label": label,
    "id_exact": id_exact,
    "id_token_only": id_token_only,
    "raw_identifier_equal": raw_equal,
    "mfr_equal": mfr_equal,
    "mfr_alias": mfr_alias,
    "id_and_mfr": id_exact * ((mfr_equal + mfr_alias) > 0),
    "model_key_di_count": di_count,
    "model_key_distinctive": (di_count <= 20).astype(int),
    "conflicting_sibling": coin(np.where(label == 1, 0.01, 0.1)),
    "prefix_only": coin(0.03),
    "product_code_match": product_code,
    "maude_has_identifier": has_id,
})

df.to_csv("fake_features.csv", index=False)
print("Saved", n, "fake feature rows")