FROM python:3.12-slim

ARG APP_UID=10001
RUN groupadd --gid ${APP_UID} app \
 && useradd --uid ${APP_UID} --gid ${APP_UID} --no-create-home app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fetch_prices.py .

USER ${APP_UID}:${APP_UID}
ENTRYPOINT ["python", "-u", "/app/fetch_prices.py"]
