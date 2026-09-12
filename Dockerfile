FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

EXPOSE 5000

# waitress rather than "flask run": the Flask dev server is single-threaded
# and explicitly not meant to serve real traffic.
CMD ["python", "-m", "waitress", "--host", "0.0.0.0", "--port", "5000", "app.main:app"]
