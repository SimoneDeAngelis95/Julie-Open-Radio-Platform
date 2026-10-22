FROM python:3.13-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg postgresql-client-17 && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 1000 julie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Image sources must be readable regardless of the host checkout's umask.
RUN chmod -R a+rX /app && DJANGO_SECRET_KEY=build-time-static-only python manage.py collectstatic --noinput
USER julie
CMD ["gunicorn", "julie.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "600", "--access-logfile", "-"]
