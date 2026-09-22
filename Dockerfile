FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ADD https://github.com/google/fonts/raw/main/ofl/lalezar/Lalezar-Regular.ttf /app/static/Lalezar.ttf
RUN mkdir -p /tmp/files && chmod -R a+r /app/static
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
