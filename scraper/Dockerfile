FROM mcr.microsoft.com/playwright/python:v1.49.0-noble
# The base image ships browsers in /ms-playwright but not the python package.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/srv
RUN pip install --no-cache-dir playwright==1.49.0 fastapi==0.115.6 "uvicorn[standard]==0.34.0"
WORKDIR /srv
COPY app/data/clubs.json /srv/app/data/clubs.json
COPY scraper/scrape.py /srv/scrape.py
COPY scraper/server.py /srv/server.py
USER pwuser
# default entrypoint = one-shot CronJob scan; the warm service overrides command
CMD ["python3", "/srv/scrape.py"]
