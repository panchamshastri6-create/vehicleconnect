FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD sh -c 'gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120 wsgi:app'
