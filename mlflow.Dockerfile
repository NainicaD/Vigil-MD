# The stock MLflow image cannot read or write gs:// paths, which is where run artifacts now live.
FROM ghcr.io/mlflow/mlflow:v3.18.0
RUN pip install --no-cache-dir google-cloud-storage
