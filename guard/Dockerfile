FROM python:3.12-slim

# ffmpeg не нужен: снимки и клипы отдаёт Frigate по HTTP.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
