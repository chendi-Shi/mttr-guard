FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-enterprise.txt .
RUN pip install --no-cache-dir -r requirements-enterprise.txt \
    && groupadd --gid 10001 mttr \
    && useradd --uid 10001 --gid 10001 --no-create-home mttr \
    && mkdir /data && chown 10001:10001 /data
COPY mttr_guard ./mttr_guard
USER 10001:10001
EXPOSE 8787
ENTRYPOINT ["python", "-m", "mttr_guard.enterprise"]
