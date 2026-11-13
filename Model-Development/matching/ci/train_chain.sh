#!/bin/sh
# The training chain the Docker image runs (Guideline 8.1): s05 -> s06 -> s08 -> model gate.
# The BigQuery steps (s03, s04) stay outside the image: mount their outputs at /app/data.
# Arguments go to ci/gate.py (e.g. --report-only for synthetic data). No MLflow inside the image.
set -eu
FEATURES=${FEATURES:-data/label_set_v2_fs2_features.parquet}
TFIDF=${TFIDF:-data/label_set_v2_fs2_features.tfidf.pkl}
TRIALS=${TRIALS:-10}

python scripts/s05_build_hand_features.py --feature-set fs2 --tfidf "$TFIDF"
python scripts/s06_train_models.py --data "$FEATURES" --manifest labels/label_set_v2.manifest.json \
  --trials "$TRIALS" --no-mlflow
python eval/s08_all.py --exp none --ie-gate --hand-features data/hand_features.csv --no-mlflow
python eval/compare_models.py
python ci/gate.py "$@"
