# The website's API (api/app.py) in one container. Build from the repository root:
#
#   docker build -f Model-Development/chatbot/Dockerfile --build-arg DATASET=vigil_md_dev -t vigil-md-api .
#
# The model stage trains the query router with the pinned versions of requirements.txt, as CI
# does (a fresh retrain reproduces champion_metrics.json), or takes the published one (below);
# either way it was built with the same scikit-learn as the server. The last stage is the slim
# server: serving libraries only, the trained model, the search
# index of DATASET (built beforehand with python -m device_search.build_index --dataset ...),
# and the website, built by the "web" stage from Web-App/frontend/ (Node 22, npm ci).
# No credentials are baked in: on Cloud Run the service account is used; locally, mount
# your gcloud application-default credentials (README, "API").
#
# The router comes from one of two stages (Docker builds only the one chosen):
#   MODEL=train      (default) retrain it from this commit, as CI does
#   MODEL=published  use the approved model from Artifact Registry, downloaded beforehand:
#       python -m query_router.publish --download <version> --out models/published
#       docker build ... --build-arg MODEL=published ...
#     The build checks the SHA-256 of every file against its manifest; /api/health then
#     reports the version.
ARG MODEL=train

FROM python:3.12-slim AS model-train
WORKDIR /build
COPY Model-Development/chatbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Model-Development/chatbot/query_router query_router
ENV MLFLOW_TRACKING_URI=sqlite:////tmp/mlflow.db \
    VIGIL_MLFLOW_ARTIFACTS=/tmp/mlflow-artifacts \
    VIGIL_AUTHOR=docker-build \
    MLFLOW_DISABLE_AGENT_HINT=1
RUN python -m query_router.generate && python -m query_router.train && cp -R models/baseline /model

FROM python:3.12-slim AS model-published
COPY Model-Development/chatbot/models/published/ /model/
RUN python -c "import hashlib, json, pathlib, sys; d = pathlib.Path('/model'); \
m = json.loads((d / 'manifest.json').read_text()); \
bad = [n for n, h in m['sha256'].items() if hashlib.sha256((d / n).read_bytes()).hexdigest() != h]; \
sys.exit(f'checksum mismatch: {bad}') if bad else print('published model', m['version'], 'checksums match')"

FROM model-${MODEL} AS model

FROM node:22-slim AS web
WORKDIR /web
COPY Web-App/frontend/package.json Web-App/frontend/package-lock.json ./
RUN npm ci
COPY Web-App/frontend/ ./
# Read at build time by Vite: call the real API on the same origin, no example data.
ENV VITE_USE_MOCKS=false VITE_API_BASE_URL=/api
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY Model-Development/chatbot/requirements-serve.txt .
RUN pip install --no-cache-dir -r requirements-serve.txt
COPY Model-Development/chatbot/answer answer
COPY Model-Development/chatbot/api api
COPY Model-Development/chatbot/device_search device_search
COPY Model-Development/chatbot/query_layer query_layer
COPY Model-Development/chatbot/query_router query_router
COPY --from=model /model models/baseline
ARG DATASET=vigil_md_dev
COPY Model-Development/chatbot/data/search/${DATASET}.sqlite data/search/${DATASET}.sqlite
ENV VIGIL_BQ_DATASET=${DATASET}
COPY --from=web /web/dist web
ENV VIGIL_WEB_DIST=/app/web
RUN useradd --create-home app
USER app
EXPOSE 8080
# --forwarded-allow-ips: trust Cloud Run's proxy for the visitor's address (the rate limit is
# per visitor); Cloud Run's proxy is the only way into the container.
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${PORT} --forwarded-allow-ips='*'"]
