FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-inter && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir google-cloud-storage google-cloud-firestore google-cloud-tasks Pillow
WORKDIR /app
COPY render.py subtitles.ass ./
ENTRYPOINT ["python", "render.py"]
