FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directory for persistent data
RUN mkdir -p /app/data

# Note: With host networking, EXPOSE is just documentation
# The actual port is determined by the PORT environment variable at runtime

# Default environment variables
ENV HOST=0.0.0.0
ENV PORT=8000

CMD ["sh", "-c", "uvicorn app.main:app --host $HOST --port $PORT"]

