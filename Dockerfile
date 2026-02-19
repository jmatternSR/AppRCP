# Utiliser l'image Python officielle
FROM python:3.11-slim

# Définir le répertoire de travail
WORKDIR /app

# Installer les dépendances système nécessaires (curl pour healthcheck)
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copier le fichier requirements.txt
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY app.py .

# Créer les répertoires nécessaires
RUN mkdir -p data exports/pdf exports/csv

# Exposer le port Streamlit (par défaut 8501)
EXPOSE 8501

# Commande de santé pour vérifier que l'application fonctionne
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Commande pour lancer l'application Streamlit
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]

