import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score

labels = pd.read_csv("fake_labels.csv")
scores = pd.read_csv("fake_scores.csv")

# join scores to labels using pair_id
df = labels.merge(scores, on="pair_id", how="inner")

# safety check: every labeled pair must have a score
assert len(df) == len(labels), "Some pairs have no score!"

threshold = 0.5
df["pred"] = (df["score"] >= threshold).astype(int)

print("pairs:    ", len(df))
print("precision:", round(precision_score(df["label"], df["pred"]), 4))
print("recall:   ", round(recall_score(df["label"], df["pred"]), 4))
print("f1:       ", round(f1_score(df["label"], df["pred"]), 4))