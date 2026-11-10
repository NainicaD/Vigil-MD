# The website's API (api/app.py) in one container. Build from the repository root:
#
#   docker build -f Model-Development/chatbot/Dockerfile --build-arg DATASET=vigil_md_dev -t vigil-md-api .
#
# Stage 1 trains the query router with the pinned versions of requirements.txt, as CI does
# (a fresh retrain reproduces champion_metrics.json), so the model and scikit-learn always
# match. Stage 2 is the slim server: serving libraries only, the trained model, the search
# index of DATASET (built beforehand with python -m device_search.build_index --dataset ...),
# and, once it exists, the front end's build (see the COPY line near the end).
# No credentials are baked in: on Cloud Run the service account is used; locally, mount
# your gcloud application-default credentials (README, "API").

FROM python:3.12-slim AS train
WORKDIR /build
COPY Model-Development/chatbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY Model-Development/chatbot/query_router query_router
ENV MLFLOW_TRACKING_URI=sqlite:////tmp/mlflow.db \
    VIGIL_MLFLOW_ARTIFACTS=/tmp/mlflow-artifacts \
    VIGIL_AUTHOR=docker-build \
    MLFLOW_DISABLE_AGENT_HINT=1
RUN python -m query_router.generate && python -m query_router.train

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
COPY --from=train /build/models/baseline models/baseline
ARG DATASET=vigil_md_dev
COPY Model-Development/chatbot/data/search/${DATASET}.sqlite data/search/${DATASET}.sqlite
ENV VIGIL_BQ_DATASET=${DATASET}
# The front end's static build, once Web-App/frontend/ exists (npm ci && npm run build):
# COPY Web-App/frontend/dist web
# ENV VIGIL_WEB_DIST=/app/web
RUN useradd --create-home app
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${PORT}"]
