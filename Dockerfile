FROM python:3.11-slim

WORKDIR /app

# curl pour le healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p data exports/pdf exports/csv

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 ${PORT:+--server.port=$PORT}"]





