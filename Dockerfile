FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps and Git (needed for repo operations) and cleanup apt cache
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY . /app

# Create non-root user for safety, grant ownership of /app and /tmp
RUN adduser --disabled-password --gecos '' appuser || true && chown -R appuser:appuser /app /tmp
USER appuser
RUN git config --global user.name "24f2002843"
RUN git config --global user.email "24f2002843@ds.study.iitm.ac.in"

EXPOSE 8000

# Entrypoint: run uvicorn serving the FastAPI app
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]