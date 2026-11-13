# Training image for the matching model (Guideline 8.1): the pinned requirements and the chain
# s05 -> s06 -> s08 -> ci/gate.py (ci/train_chain.sh). BigQuery steps run outside; mount their outputs.
#
# Build and run (from Model-Development/matching):
#   docker build -t vigil-matching-train .
#   docker run --rm -v "$PWD/data:/app/data" vigil-matching-train
# Smoke data (no GCP): make it with ci/make_ci_feature_table.py first, then
#   docker run --rm -v "$PWD/data:/app/data" -e FEATURES=data/ci/features.csv \
#     -e TFIDF=data/ci/features.tfidf.pkl -e TRIALS=1 vigil-matching-train --report-only
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ml.txt

COPY vigil_model/ vigil_model/
COPY scripts/ scripts/
COPY eval/ eval/
COPY ci/ ci/
COPY labels/ labels/
COPY sql/ sql/
COPY pyproject.toml ./

RUN useradd --create-home app && mkdir -p data && chown app data
USER app
ENV MPLCONFIGDIR=/tmp/matplotlib
ENTRYPOINT ["sh", "ci/train_chain.sh"]
