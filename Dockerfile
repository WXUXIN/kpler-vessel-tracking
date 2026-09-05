FROM python:3.11-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim
WORKDIR /app
COPY --from=build /install /usr/local
COPY ship_positions.json ./
COPY static ./static
ENV PYTHONUNBUFFERED=1
