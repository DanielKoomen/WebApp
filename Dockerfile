FROM python:3

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN pip install yt-dlp flask Flask-Babel bs4 requests Pillow gunicorn redis musicbrainzngs

RUN mkdir /app
WORKDIR /app

COPY ./src .

RUN pybabel compile -d translations

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["gunicorn", \
    "-b", "0.0.0.0:8080", \
    "--workers", "4", \
    "--threads", "4", \
    # "--access-logfile", "-", \
    "app"]
