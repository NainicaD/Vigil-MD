import numpy as np
import pandas as pd

rng = np.random.default_rng(42)
n = 1000

# 10% matches, 90% non-matches (real data is skewed like this)
labels = rng.choice([1, 0], size=n, p=[0.1, 0.9])

# real matches tend to get high scores, non-matches low scores
scores = np.where(labels == 1, rng.beta(8, 2, n), rng.beta(2, 8, n))

pair_ids = [f"p{i:05d}" for i in range(n)]

labels_df = pd.DataFrame({"pair_id": pair_ids, "label": labels})
scores_df = pd.DataFrame({
    "pair_id": pair_ids,
    "score": scores.round(4),
    "model_name": "fake_model",
    "mlflow_run_id": "none",
    "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
})

labels_df.to_csv("fake_labels.csv", index=False)
scores_df.to_csv("fake_scores.csv", index=False)
print("Saved", n, "fake pairs")