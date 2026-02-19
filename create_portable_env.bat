@echo off
REM ============================================================================
REM  Script de préparation d'une version « portable » de l'application
REM  - Crée un environnement virtuel local (.venv) dans ce dossier
REM  - Installe toutes les dépendances depuis requirements.txt
REM  - Génère un script run_portable.bat pour lancer l'application
REM 
REM  Prérequis (à faire UNE SEULE FOIS sur une machine de build) :
REM    - Avoir Python 3 installé et accessible dans le PATH (commande : python)
REM 
REM  Utilisation :
REM    1. Double-cliquer sur create_portable_env.bat (ou l'exécuter dans un terminal)
REM    2. À la fin, utiliser run_portable.bat pour lancer l'app
REM    3. Copier tout le dossier (y compris .venv) sur une autre machine pour une
REM       utilisation « portable » (aucune installation système nécessaire).
REM ============================================================================

SETLOCAL

echo.
echo [1/4] Vérification de Python...
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERREUR : Python n'est pas disponible dans le PATH.
    echo Installez Python 3 puis relancez ce script.
    pause
    EXIT /B 1
)

echo [2/4] Création de l'environnement virtuel local (.venv)...
IF EXIST .venv (
    echo   Un environnement .venv existe déjà, il sera réutilisé.
) ELSE (
    python -m venv .venv
    IF ERRORLEVEL 1 (
        echo ERREUR : impossible de créer l'environnement virtuel.
        pause
        EXIT /B 1
    )
)

echo [3/4] Installation / mise à jour des dépendances...
CALL .venv\Scripts\python.exe -m pip install --upgrade pip
CALL .venv\Scripts\python.exe -m pip install -r requirements.txt
IF ERRORLEVEL 1 (
    echo ERREUR : échec de l'installation des dépendances.
    pause
    EXIT /B 1
)

echo [4/4] Génération du script de lancement portable (run_portable.bat)...
> run_portable.bat (
    echo @echo off
    echo REM Lance l'application Streamlit en utilisant l'environnement virtuel local (.venv)
    echo REM Aucune installation système n'est nécessaire sur la machine cible.
    echo SETLOCAL
    echo cd /d %%~dp0
    echo echo.
    echo echo Démarrage de l'application RCP...
    echo echo (Fenêtre Streamlit dans le navigateur ^; fermer ici pour arrêter.)
    echo .venv\Scripts\python.exe -m streamlit run app.py
    echo ENDLOCAL
)

echo.
echo ============================================================================
echo  Preparation terminee.
echo  - Pour lancer l'application sur cette machine OU une autre machine,
echo    il suffit de copier TOUT le dossier et d'executer : run_portable.bat
echo  - Assurez-vous que les sous-dossiers "data" et "exports" restent dans
echo    le meme dossier que app.py pour conserver l'acces a la base SQLite.
echo ============================================================================
echo.
pause

ENDLOCAL



