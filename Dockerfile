FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Local-only development routes must never be enabled in a container image.
ENV ENABLE_LOCAL_EVAL=false

EXPOSE 8080
CMD ["python", "main.py"]
