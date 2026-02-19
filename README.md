# AppRCP - Application de saisie structurée pour RCP Pelvi-Périnéologie

Application Streamlit pour la saisie structurée des fiches de Réunion de Concertation Pluridisciplinaire (RCP) en pelvi-périnéologie.

## Fonctionnalités

- 📝 Saisie structurée de fiches RCP
- 📊 Gestion des RCP (création, archivage, suppression)
- 📄 Génération de PDF pour les fiches et les RCP complètes
- 📥 Export CSV des données
- 💾 Base de données SQLite locale (offline)

## Prérequis

- Python 3.11 ou supérieur
- Docker et Docker Compose (optionnel, pour le déploiement)

## Installation

### Installation locale

1. Cloner le dépôt :
```bash
git clone <url-du-repo>
cd AppRCP
```

2. Créer un environnement virtuel :
```bash
python -m venv .venv
```

3. Activer l'environnement virtuel :
- Sur Windows :
```bash
.venv\Scripts\activate
```
- Sur Linux/Mac :
```bash
source .venv/bin/activate
```

4. Installer les dépendances :
```bash
pip install -r requirements.txt
```

5. Lancer l'application :
```bash
streamlit run app.py
```

L'application sera accessible sur `http://localhost:8501`

## Déploiement avec Docker

### Construction et lancement avec Docker Compose

```bash
docker-compose up -d
```

L'application sera accessible sur `http://localhost:8501`

### Construction et lancement manuel

1. Construire l'image Docker :
```bash
docker build -t apprcp .
```

2. Lancer le conteneur :
```bash
docker run -d \
  -p 8501:8501 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/exports:/app/exports \
  --name apprcp \
  apprcp
```

## Structure du projet

```
AppRCP/
├── app.py                 # Application principale
├── requirements.txt       # Dépendances Python
├── Dockerfile            # Configuration Docker
├── docker-compose.yml    # Configuration Docker Compose
├── .dockerignore         # Fichiers ignorés par Docker
├── .gitignore           # Fichiers ignorés par Git
├── data/                # Base de données SQLite (non versionnée)
└── exports/             # Exports PDF et CSV (non versionnés)
```

## Utilisation

1. **Créer une RCP** : Accédez à la page d'accueil et créez une nouvelle RCP avec une date
2. **Ajouter des fiches** : Dans une RCP, ajoutez des fiches pour chaque patiente
3. **Saisir les données** : Remplissez le formulaire de fiche avec toutes les informations
4. **Générer des PDF** : Générez des PDF individuels ou pour toute la RCP
5. **Exporter en CSV** : Exportez les données pour analyse externe

## Données

Les données sont stockées dans :
- **Base de données** : `data/rcp_bandelette.sqlite`
- **Exports PDF** : `exports/pdf/`
- **Exports CSV** : `exports/csv/`

⚠️ **Important** : Les répertoires `data/` et `exports/` ne sont pas versionnés dans Git pour préserver la confidentialité des données.

## Développement

Pour contribuer au projet :

1. Fork le dépôt
2. Créer une branche pour votre fonctionnalité (`git checkout -b feature/ma-fonctionnalite`)
3. Commiter vos changements (`git commit -am 'Ajout de ma fonctionnalité'`)
4. Pousser vers la branche (`git push origin feature/ma-fonctionnalite`)
5. Ouvrir une Pull Request

## Licence

[À définir]

## Support

Pour toute question ou problème, ouvrez une issue sur le dépôt GitHub.

