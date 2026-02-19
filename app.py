import os
import json
import uuid
import sqlite3
import random
import string
import time
from datetime import datetime

import pandas as pd
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

APP_TITLE = "RCP Bandelette — saisie structurée (offline)"

# Chemins configurables via variables d'environnement
APP_DATA_DIR = os.getenv("APP_DATA_DIR", "data")
APP_EXPORT_DIR = os.getenv("APP_EXPORT_DIR", "exports")
DB_PATH = os.path.join(APP_DATA_DIR, "rcp_bandelette.sqlite")
PDF_DIR = os.path.join(APP_EXPORT_DIR, "pdf")
CSV_DIR = os.path.join(APP_EXPORT_DIR, "csv")


# ---------------------------
# Utils: filesystem & database
# ---------------------------
def ensure_dirs():
    """Crée les dossiers nécessaires s'ils n'existent pas."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(CSV_DIR, exist_ok=True)


def get_conn():
    """Obtient une connexion SQLite avec optimisations."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    # Activer WAL mode pour meilleures performances en lecture/écriture concurrente
    cur.execute("PRAGMA journal_mode=WAL")
    # Timeout pour gérer les verrous de base de données
    cur.execute("PRAGMA busy_timeout=5000")
    # Activer les clés étrangères
    cur.execute("PRAGMA foreign_keys=ON")
    return conn


def db_write(fn, retries=5):
    """
    Exécute une fonction d'écriture avec retry exponentiel en cas d'erreur de verrouillage.
    
    Args:
        fn: Fonction qui prend une connexion et un curseur en paramètres et retourne un résultat
        retries: Nombre de tentatives (défaut: 5)
    
    Returns:
        Le résultat de la fonction fn
    
    Raises:
        sqlite3.OperationalError: Si toutes les tentatives échouent
    """
    for attempt in range(retries):
        try:
            conn = get_conn()
            try:
                cur = conn.cursor()
                result = fn(conn, cur)
                conn.commit()
                return result
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            error_msg = str(e).lower()
            if 'locked' in error_msg or 'busy' in error_msg:
                if attempt < retries - 1:
                    # Backoff exponentiel : 0.01s, 0.02s, 0.04s, 0.08s, 0.16s
                    wait_time = 0.01 * (2 ** attempt)
                    time.sleep(wait_time)
                    continue
            # Si ce n'est pas une erreur de verrouillage, ou si on a épuisé les tentatives, relancer
            raise
    raise sqlite3.OperationalError("Échec après toutes les tentatives de retry")


def generate_rcp_code() -> str:
    """Génère un code RCP unique à 6 caractères alphanumériques."""
    chars = string.ascii_uppercase + string.digits
    conn = get_conn()
    try:
        cur = conn.cursor()
        for _ in range(100):  # Limiter les tentatives
            code = ''.join(random.choices(chars, k=6))
            cur.execute("SELECT code FROM rcp WHERE code = ?", (code,))
            if cur.fetchone() is None:
                return code
        raise ValueError("Impossible de générer un code RCP unique")
    finally:
        conn.close()


def migrate_db():
    """Migre la base de données depuis l'ancienne structure vers la nouvelle."""
    # Lecture seule pour vérifier la structure
    conn = get_conn()
    cur = conn.cursor()
    
    # Vérifier si la table rcp existe
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rcp'")
    rcp_exists = cur.fetchone() is not None
    
    # Vérifier si la colonne rcp_code existe dans fiches
    cur.execute("PRAGMA table_info(fiches)")
    columns = [col[1] for col in cur.fetchall()]
    has_rcp_code = "rcp_code" in columns
    
    conn.close()
    
    # Écritures avec retry
    def _migrate_tables(conn, cur):
        if not rcp_exists:
            # Créer la table RCP
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rcp (
                    code TEXT PRIMARY KEY,
                    date_rcp TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        else:
            # Vérifier si la colonne date_rcp existe
            cur.execute("PRAGMA table_info(rcp)")
            rcp_columns = [col[1] for col in cur.fetchall()]
            if "date_rcp" not in rcp_columns:
                cur.execute("ALTER TABLE rcp ADD COLUMN date_rcp TEXT")
            # Vérifier si la colonne is_archived existe
            if "is_archived" not in rcp_columns:
                cur.execute("ALTER TABLE rcp ADD COLUMN is_archived INTEGER DEFAULT 0")
            # Vérifier si la colonne medecins_presents existe
            if "medecins_presents" not in rcp_columns:
                cur.execute("ALTER TABLE rcp ADD COLUMN medecins_presents TEXT")
    
    db_write(_migrate_tables)
    
    if not has_rcp_code:
        # Générer un code RCP unique pour les fiches existantes
        chars = string.ascii_uppercase + string.digits
        default_rcp_code = None
        conn = get_conn()
        try:
            cur = conn.cursor()
            for _ in range(100):  # Essayer jusqu'à 100 fois
                code = ''.join(random.choices(chars, k=6))
                cur.execute("SELECT code FROM rcp WHERE code = ?", (code,))
                if cur.fetchone() is None:
                    default_rcp_code = code
                    break
        finally:
            conn.close()
        
        if default_rcp_code:
            def _migrate_rcp_code(conn, cur):
                now = datetime.now().isoformat(timespec="seconds")
                cur.execute(
                    "INSERT INTO rcp (code, created_at, updated_at) VALUES (?, ?, ?)",
                    (default_rcp_code, now, now)
                )
                
                # Ajouter la colonne rcp_code
                cur.execute(f"ALTER TABLE fiches ADD COLUMN rcp_code TEXT")
                cur.execute(f"UPDATE fiches SET rcp_code = ? WHERE rcp_code IS NULL", (default_rcp_code,))
            
            db_write(_migrate_rcp_code)


def init_db():
    def _init_tables(conn, cur):
        # Table RCP (dossiers)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rcp (
                code TEXT PRIMARY KEY,
                date_rcp TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_archived INTEGER DEFAULT 0,
                medecins_presents TEXT
            )
            """
        )
        # Table fiches (liées aux RCP)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fiches (
                id TEXT PRIMARY KEY,
                rcp_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY (rcp_code) REFERENCES rcp(code)
            )
            """
        )
        
        # Créer des index pour accélérer les requêtes fréquentes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fiches_rcp_code ON fiches(rcp_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_fiches_updated_at ON fiches(updated_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rcp_date_rcp ON rcp(date_rcp)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rcp_updated_at ON rcp(updated_at)")
    
    db_write(_init_tables)

    # Migrer si nécessaire
    migrate_db()


def create_rcp(date_rcp: str) -> str:
    """Crée une nouvelle RCP avec une date et retourne son code."""
    code = generate_rcp_code()
    now = datetime.now().isoformat(timespec="seconds")
    
    def _create_rcp(conn, cur):
        cur.execute(
            "INSERT INTO rcp (code, date_rcp, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (code, date_rcp, now, now)
        )
    
    db_write(_create_rcp)
    # Invalider le cache
    get_all_rcp.clear()
    return code


@st.cache_data(ttl=2)  # Cache pendant 2 secondes pour éviter les requêtes répétées
def get_all_rcp() -> list:
    """Retourne la liste de toutes les RCP avec le nombre de fiches."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT r.code, r.date_rcp, r.created_at, r.updated_at, 
                   COALESCE(r.is_archived, 0) as is_archived, COUNT(f.id) as nb_fiches
            FROM rcp r
            LEFT JOIN fiches f ON r.code = f.rcp_code
            GROUP BY r.code, r.date_rcp, r.created_at, r.updated_at, r.is_archived
            ORDER BY r.date_rcp DESC, r.updated_at DESC
        """)
        results = cur.fetchall()
        return [{"code": r[0], "date_rcp": r[1], "created_at": r[2], "updated_at": r[3], "is_archived": bool(r[4]), "nb_fiches": r[5]} for r in results]
    finally:
        conn.close()


def get_rcp_date(rcp_code: str) -> str:
    """Récupère la date d'une RCP spécifique."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT date_rcp FROM rcp WHERE code = ?", (rcp_code,))
        result = cur.fetchone()
        return result[0] if result and result[0] else ''
    finally:
        conn.close()


def get_rcp_medecins_presents(rcp_code: str) -> str:
    """Récupère la liste des médecins présents d'une RCP."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT medecins_presents FROM rcp WHERE code = ?", (rcp_code,))
        result = cur.fetchone()
        return result[0] if result and result[0] else ''
    finally:
        conn.close()


def update_rcp_medecins_presents(rcp_code: str, medecins_presents: str):
    """Met à jour la liste des médecins présents d'une RCP."""
    now = datetime.now().isoformat(timespec="seconds")
    
    def _update_medecins(conn, cur):
        cur.execute(
            "UPDATE rcp SET medecins_presents = ?, updated_at = ? WHERE code = ?",
            (medecins_presents, now, rcp_code)
        )
    
    db_write(_update_medecins)
    # Invalider le cache
    get_all_rcp.clear()


def archive_rcp(rcp_code: str, archived: bool = True):
    """Archive ou désarchive une RCP."""
    now = datetime.now().isoformat(timespec="seconds")
    
    def _archive_rcp(conn, cur):
        cur.execute(
            "UPDATE rcp SET is_archived = ?, updated_at = ? WHERE code = ?",
            (1 if archived else 0, now, rcp_code)
        )
    
    db_write(_archive_rcp)
    # Invalider le cache
    get_all_rcp.clear()


def delete_rcp(rcp_code: str):
    """Supprime une RCP et toutes ses fiches."""
    def _delete_rcp(conn, cur):
        cur.execute("DELETE FROM fiches WHERE rcp_code = ?", (rcp_code,))
        cur.execute("DELETE FROM rcp WHERE code = ?", (rcp_code,))
    
    db_write(_delete_rcp)
    # Invalider le cache
    get_all_rcp.clear()
    load_fiches.clear()


def upsert_fiche(fiche_id: str, rcp_code: str, payload: dict):
    now = datetime.now().isoformat(timespec="seconds")
    
    # Vérifier si la fiche existe (lecture seule)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM fiches WHERE id = ?", (fiche_id,))
        exists = cur.fetchone() is not None
    finally:
        conn.close()
    
    def _upsert_fiche(conn, cur):
        if exists:
            cur.execute(
                "UPDATE fiches SET updated_at = ?, payload_json = ? WHERE id = ?",
                (now, json.dumps(payload, ensure_ascii=False), fiche_id),
            )
        else:
            cur.execute(
                "INSERT INTO fiches (id, rcp_code, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (fiche_id, rcp_code, now, now, json.dumps(payload, ensure_ascii=False)),
            )
        # Mettre à jour la date de modification de la RCP
        cur.execute("UPDATE rcp SET updated_at = ? WHERE code = ?", (now, rcp_code))
    
    db_write(_upsert_fiche)
    # Invalider le cache
    get_all_rcp.clear()
    load_fiches.clear()


@st.cache_data(ttl=2)  # Cache pendant 2 secondes
def load_fiches(rcp_code: str = None) -> pd.DataFrame:
    """Charge les fiches, optionnellement filtrées par RCP."""
    conn = get_conn()
    try:
        if rcp_code:
            query = "SELECT * FROM fiches WHERE rcp_code = ? ORDER BY updated_at DESC"
            df = pd.read_sql_query(query, conn, params=(rcp_code,))
        else:
            query = "SELECT * FROM fiches ORDER BY updated_at DESC"
            df = pd.read_sql_query(query, conn)
    finally:
        conn.close()
    
    if df.empty:
        return df
    # Parse JSON into columns (flat) - optimisé avec vectorisation
    parsed = df["payload_json"].apply(json.loads)
    flat = pd.json_normalize(parsed)
    out = pd.concat([df.drop(columns=["payload_json"]), flat], axis=1)
    return out


def get_fiche_by_id(fiche_id: str) -> dict:
    """Récupère une fiche par son ID."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT rcp_code, payload_json FROM fiches WHERE id = ?", (fiche_id,))
    res = cur.fetchone()
    conn.close()
    if res:
        return {"rcp_code": res[0], "payload": json.loads(res[1])}
    return None


def transfer_fiche(fiche_id: str, target_rcp_code: str):
    """Transfère une fiche vers une autre RCP."""
    # Vérifications en lecture seule
    conn = get_conn()
    try:
        cur = conn.cursor()
        
        # Récupérer le code RCP actuel
        cur.execute("SELECT rcp_code FROM fiches WHERE id = ?", (fiche_id,))
        res = cur.fetchone()
        if not res:
            return False
        
        source_rcp_code = res[0]
        
        # Vérifier que la RCP cible existe
        cur.execute("SELECT code FROM rcp WHERE code = ?", (target_rcp_code,))
        if cur.fetchone() is None:
            return False
    finally:
        conn.close()
    
    # Écriture avec retry
    now = datetime.now().isoformat(timespec="seconds")
    
    def _transfer_fiche(conn, cur):
        # Transférer la fiche
        cur.execute("UPDATE fiches SET rcp_code = ?, updated_at = ? WHERE id = ?", (target_rcp_code, now, fiche_id))
        
        # Mettre à jour les dates de modification des deux RCP
        cur.execute("UPDATE rcp SET updated_at = ? WHERE code = ?", (now, source_rcp_code))
        cur.execute("UPDATE rcp SET updated_at = ? WHERE code = ?", (now, target_rcp_code))
    
    db_write(_transfer_fiche)
    # Invalider le cache
    get_all_rcp.clear()
    load_fiches.clear()
    return True


def delete_fiche(fiche_id: str):
    """Supprime une fiche et met à jour la date de modification de la RCP."""
    # Récupérer le code RCP avant suppression (lecture seule)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT rcp_code FROM fiches WHERE id = ?", (fiche_id,))
        res = cur.fetchone()
        rcp_code = res[0] if res else None
    finally:
        conn.close()
    
    # Écriture avec retry
    def _delete_fiche(conn, cur):
        cur.execute("DELETE FROM fiches WHERE id = ?", (fiche_id,))
        
        # Mettre à jour la date de modification de la RCP
        if rcp_code:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute("UPDATE rcp SET updated_at = ? WHERE code = ?", (now, rcp_code))
    
    db_write(_delete_fiche)
    # Invalider le cache
    get_all_rcp.clear()
    load_fiches.clear()


# ---------------------------
# PDF generation
# ---------------------------
def generate_pdf_fiche(fiche_id: str, payload: dict, rcp_code: str = None) -> str:
    """Génère un PDF amélioré pour une seule fiche."""
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    patiente_nom = payload.get("patiente_nom", "Non renseigné")
    # Nettoyer le nom pour le nom de fichier
    safe_name = "".join(c for c in patiente_nom if c.isalnum() or c in (' ', '-', '_')).strip()[:30]
    filename = f"fiche_{safe_name}_{ts}.pdf" if safe_name else f"fiche_{fiche_id}_{ts}.pdf"
    out_path = os.path.join(PDF_DIR, filename)

    doc = SimpleDocTemplate(out_path, pagesize=A4, 
                           rightMargin=2*cm, leftMargin=2*cm,
                           topMargin=2*cm, bottomMargin=2*cm)
    story = []
    styles = getSampleStyleSheet()
    
    # Style personnalisé pour le titre
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        textColor=colors.HexColor('#1f4788'),
        spaceAfter=30,
        alignment=TA_CENTER
    )
    
    # Style pour les sections
    section_style = ParagraphStyle(
        'Section',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=colors.HexColor('#2c3e50'),
        spaceAfter=12,
        spaceBefore=12,
        borderColor=colors.HexColor('#3498db'),
        borderWidth=1,
        borderPadding=5,
        backColor=colors.HexColor('#ecf0f1')
    )
    
    # Titre principal
    story.append(Paragraph("RCP de pelvi-périnéologie", title_style))
    
    # Date de la RCP juste en dessous du titre (même police)
    rcp_date = payload.get("rcp_date", "")
    if rcp_date and rcp_date != "None":
        try:
            date_obj = datetime.strptime(rcp_date, "%Y-%m-%d")
            mois_fr = [
                "janvier", "février", "mars", "avril", "mai", "juin",
                "juillet", "août", "septembre", "octobre", "novembre", "décembre"
            ]
            jour = date_obj.day
            mois = mois_fr[date_obj.month - 1]
            annee = date_obj.year
            date_formatted = f"{jour} {mois} {annee}"
        except:
            date_formatted = rcp_date
    else:
        date_formatted = "Date non renseignée"
    
    # Style pour la date (même style que le titre mais sans espacement après)
    date_style = ParagraphStyle(
        'DateStyle',
        parent=title_style,
        spaceAfter=20,
    )
    story.append(Paragraph(date_formatted, date_style))
    story.append(Spacer(1, 0.8*cm))
    
    # Liste des médecins présents (avant la section Identité)
    if rcp_code:
        medecins_presents = get_rcp_medecins_presents(rcp_code)
        if medecins_presents and medecins_presents.strip():
            # Style pour les médecins présents
            medecins_style = ParagraphStyle(
                'MedecinsStyle',
                parent=styles['Normal'],
                fontSize=11,
                textColor=colors.HexColor('#2c3e50'),
                spaceAfter=15,
                alignment=TA_CENTER,
                leading=14
            )
            story.append(Paragraph("Médecins présents", medecins_style))
            # Afficher la liste des médecins (gérer les retours à la ligne et les virgules)
            medecins_list = medecins_presents.replace('\n', ', ').replace(',,', ',').strip()
            if medecins_list:
                medecins_paragraph = Paragraph(medecins_list, medecins_style)
                story.append(medecins_paragraph)
            story.append(Spacer(1, 0.5*cm))
    
    def add_section(title, data_dict):
        """Ajoute une section au PDF."""
        story.append(Paragraph(title, section_style))
        
        # Style pour les cellules du tableau (permet le wrapping du texte)
        cell_style_key = ParagraphStyle(
            'CellKey',
            parent=styles['Normal'],
            fontSize=10,
            fontName='Helvetica-Bold',
            textColor=colors.black,
            leading=12,
            spaceAfter=0,
            spaceBefore=0,
        )
        cell_style_value = ParagraphStyle(
            'CellValue',
            parent=styles['Normal'],
            fontSize=10,
            fontName='Helvetica',
            textColor=colors.black,
            leading=12,
            spaceAfter=0,
            spaceBefore=0,
        )
        
        # Créer un tableau pour les données avec Paragraph pour le wrapping
        table_data = []
        for key, value in data_dict.items():
            if value and str(value).strip() and str(value) != "None":
                # Utiliser Paragraph pour permettre le wrapping du texte
                key_para = Paragraph(str(key).replace('\n', '<br/>'), cell_style_key)
                value_para = Paragraph(str(value).replace('\n', '<br/>'), cell_style_value)
                table_data.append([key_para, value_para])
        
        if table_data:
            data_table = Table(table_data, colWidths=[6*cm, 10*cm])
            data_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f8f9fa')),
                ('TEXTCOLOR', (0, 0), (0, -1), colors.black),
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dee2e6')),
                ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('WORDWRAP', (0, 0), (-1, -1), True),  # Activer le word wrap
            ]))
            story.append(data_table)
            story.append(Spacer(1, 0.5*cm))
    
    # Section Identité - titre avec nom et date de naissance
    patiente_nom = payload.get("patiente_nom", "").strip()
    patiente_ddn = payload.get("patiente_ddn", "").strip()
    if patiente_ddn:
        identite_title = f"{patiente_nom} ({patiente_ddn})" if patiente_nom else f"({patiente_ddn})"
    else:
        identite_title = patiente_nom if patiente_nom else "Identité"
    
    identite_data = {
        # Ligne Date RCP supprimée du rendu PDF
        "Chirurgien responsable": payload.get("chirurgien", ""),
    }
    add_section(identite_title, identite_data)
    
    # Section Anthropométrie - titre avec taille, poids et IMC
    taille_cm = payload.get("taille_cm", "").strip()
    poids_kg = payload.get("poids_kg", "").strip()
    imc = payload.get("imc", "")
    imc_str = str(imc).strip() if imc else ""
    
    # Construire le titre
    anthropo_parts = []
    if taille_cm:
        anthropo_parts.append(f"{taille_cm} cm")
    if poids_kg:
        anthropo_parts.append(f"{poids_kg} kg")
    anthropo_title = " / ".join(anthropo_parts) if anthropo_parts else "Anthropométrie"
    if imc_str:
        anthropo_title = f"{anthropo_title} (IMC : {imc_str})"
    
    # Pas de données à afficher dans le tableau pour l'anthropométrie car tout est dans le titre
    # On peut passer un dictionnaire vide ou ne pas appeler add_section
    if anthropo_title != "Anthropométrie":
        # Afficher juste le titre sans tableau
        story.append(Paragraph(anthropo_title, section_style))
        story.append(Spacer(1, 0.5*cm))
    
    # Section Antécédents
    antecedents = payload.get("antecedents", {})
    # Inclure toutes les valeurs sauf "Non" et "NA" (inclut seulement "Oui")
    antecedents_filtered = {k: v for k, v in antecedents.items() if v and v != "Non" and v != "NA"}
    if antecedents_filtered:
        add_section("Antécédents / Contexte", antecedents_filtered)
    
    # Section Symptômes
    symptomes_data = {
        "Type d'incontinence": payload.get("iu_type", ""),
        "Sévérité (protections/jour)": payload.get("severite_protections_j", ""),
        "Gêne globale (/10)": payload.get("gene_10", ""),
        "Score USP": payload.get("score_usp", ""),
        "Score HAV": payload.get("score_hav", ""),
        "Dysurie": payload.get("dysurie", ""),
    }
    symptomes_filtered = {k: v for k, v in symptomes_data.items() if v and str(v).strip()}
    if symptomes_filtered:
        add_section("Symptômes / Interrogatoire", symptomes_filtered)
    
    # Section Examen clinique
    examen = payload.get("examen", {})
    # Inclure toutes les valeurs sauf "Non", "Négatif", "Négative" et "NA" (inclut seulement "Oui", "Positif", "Positive")
    examen_filtered = {k: v for k, v in examen.items() if v and str(v).strip() and v != "Non" and v != "Négatif" and v != "Négative" and v != "NA"}
    if examen_filtered:
        add_section("Examen clinique", examen_filtered)
    
    # Section Débitmétrie
    debit_data = {
        "Qmax (mL/s)": payload.get("qmax_ml_s", ""),
        "Volume uriné (mL)": payload.get("volume_urine_ml", ""),
        "RPM (mL)": payload.get("rpm_ml", ""),
        "Courbe normale": payload.get("courbe_normale", ""),
    }
    debit_filtered = {k: v for k, v in debit_data.items() if v and str(v).strip()}
    if debit_filtered:
        add_section("Débitmétrie / Bilan uro", debit_filtered)
    
    # Section Examens d'imagerie
    examens_imagerie = payload.get("examens_imagerie", "")
    if examens_imagerie and examens_imagerie.strip():
        story.append(Paragraph("Examens d'imagerie", section_style))
        examens_imagerie_style = ParagraphStyle(
            'ExamensImagerie',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=12,
            leftIndent=0,
            rightIndent=0,
        )
        story.append(Paragraph(examens_imagerie.strip(), examens_imagerie_style))
        story.append(Spacer(1, 0.5*cm))
    
    # Section Proposition RCP
    prop = payload.get("proposition_rcp", "")
    if prop and prop.strip():
        story.append(Paragraph("Proposition de la RCP", section_style))
        prop_style = ParagraphStyle(
            'Proposition',
            parent=styles['Normal'],
            fontSize=10,
            leading=14,
            spaceAfter=12,
            leftIndent=0.5*cm,
            rightIndent=0.5*cm,
        )
        story.append(Paragraph(prop.replace('\n', '<br/>'), prop_style))
    
    doc.build(story)
    return out_path


def generate_pdf_rcp(rcp_code: str) -> list:
    """Génère un PDF pour chaque fiche de la RCP et retourne la liste des chemins."""
    # Charger toutes les fiches de la RCP
    df_fiches = load_fiches(rcp_code)
    if df_fiches.empty:
        raise ValueError(f"Aucune fiche trouvée pour la RCP {rcp_code}")
    
    # Récupérer les données JSON complètes
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, payload_json FROM fiches WHERE rcp_code = ? ORDER BY created_at", (rcp_code,))
    fiches_data = [(row[0], json.loads(row[1])) for row in cur.fetchall()]
    conn.close()
    
    pdf_paths = []
    for fiche_id, payload in fiches_data:
        pdf_path = generate_pdf_fiche(fiche_id, payload, rcp_code)
        pdf_paths.append(pdf_path)
    
    return pdf_paths


# ---------------------------
# Import/Export CSV
# ---------------------------
def export_rcp_to_csv(rcp_code: str) -> str:
    """Exporte une RCP et ses fiches en CSV pour synchronisation."""
    conn = get_conn()
    try:
        # Export RCP
        df_rcp = pd.read_sql_query("SELECT * FROM rcp WHERE code = ?", conn, params=(rcp_code,))
        
        # Export fiches de cette RCP
        df_fiches = pd.read_sql_query("SELECT * FROM fiches WHERE rcp_code = ? ORDER BY updated_at DESC", conn, params=(rcp_code,))
    finally:
        conn.close()
    
    if df_fiches.empty and df_rcp.empty:
        return None
    
    # Créer un DataFrame unifié pour l'export - optimisé avec apply au lieu de iterrows
    export_data = []
    
    # Pour chaque fiche, ajouter les infos RCP - optimisé
    if not df_fiches.empty:
        payloads = df_fiches["payload_json"].apply(json.loads)
        for idx in df_fiches.index:
            fiche_row = df_fiches.loc[idx]
            payload = payloads.loc[idx]
            row = {
                "type": "fiche",
                "id": fiche_row["id"],
                "rcp_code": fiche_row["rcp_code"],
                "created_at": fiche_row["created_at"],
                "updated_at": fiche_row["updated_at"],
                **payload
            }
            export_data.append(row)
    
    # Pour la RCP, ajouter une entrée
    if not df_rcp.empty:
        rcp_row = df_rcp.iloc[0]
        row = {
            "type": "rcp",
            "code": rcp_row["code"],
            "date_rcp": rcp_row.get("date_rcp", ""),
            "created_at": rcp_row["created_at"],
            "updated_at": rcp_row["updated_at"]
        }
        export_data.append(row)
    
    df_export = pd.DataFrame(export_data)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(CSV_DIR, f"export_rcp_{rcp_code}_{ts}.csv")
    df_export.to_csv(out_csv, index=False, encoding="utf-8")
    return out_csv


def import_rcp_from_csv(uploaded_file, target_rcp_code: str) -> dict:
    """Importe les données depuis un CSV dans une RCP spécifique et retourne un rapport."""
    try:
        df = pd.read_csv(uploaded_file)
        
        if df.empty:
            return {"success": False, "message": "Le fichier CSV est vide."}
        
        # Vérifier que la RCP cible existe (lecture seule)
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT code FROM rcp WHERE code = ?", (target_rcp_code,))
            if cur.fetchone() is None:
                return {"success": False, "message": f"La RCP {target_rcp_code} n'existe pas."}
        finally:
            conn.close()
        
        imported_fiches = 0
        updated_fiches = 0
        errors = []
        
        # Traiter uniquement les fiches de la RCP cible
        if "type" in df.columns:
            fiche_rows = df[df["type"] == "fiche"]
        else:
            # Format ancien sans colonne type
            fiche_rows = df
        
        # Grouper par ID et garder seulement la fiche la plus récente pour chaque ID
        if not fiche_rows.empty and "id" in fiche_rows.columns:
            # Convertir updated_at en datetime pour comparaison
            fiche_rows = fiche_rows.copy()
            fiche_rows["updated_at_parsed"] = pd.to_datetime(
                fiche_rows["updated_at"], 
                errors="coerce", 
                format="%Y-%m-%dT%H:%M:%S"
            )
            # Trier par ID et updated_at décroissant, puis garder le premier de chaque groupe
            fiche_rows = fiche_rows.sort_values(["id", "updated_at_parsed"], ascending=[True, False])
            fiche_rows = fiche_rows.drop_duplicates(subset=["id"], keep="first")
            fiche_rows = fiche_rows.drop(columns=["updated_at_parsed"])
        
        skipped_older = 0
        
        # Récupérer toutes les fiches existantes en une seule requête pour optimiser (lecture seule)
        existing_fiches = {}
        if not fiche_rows.empty and "id" in fiche_rows.columns:
            fiche_ids = [str(fid) for fid in fiche_rows["id"].dropna().unique()]
            if fiche_ids:
                conn = get_conn()
                try:
                    cur = conn.cursor()
                    placeholders = ','.join(['?'] * len(fiche_ids))
                    cur.execute(f"SELECT id, updated_at FROM fiches WHERE id IN ({placeholders})", fiche_ids)
                    for row in cur.fetchall():
                        existing_fiches[row[0]] = row[1]
                finally:
                    conn.close()
        
        # Traiter les fiches par batch
        batch_size = 50
        now = datetime.now().isoformat(timespec="seconds")
        
        # Préparer les données de toutes les fiches
        fiche_operations = []
        
        for _, row in fiche_rows.iterrows():
            try:
                fiche_id = row.get("id")
                if pd.isna(fiche_id):
                    continue
                
                fiche_id = str(fiche_id)
                rcp_code = target_rcp_code
                
                # Reconstruire le payload depuis les colonnes
                payload = {}
                payload_fields = [
                    "rcp_date", "chirurgien",
                    "patiente_nom", "patiente_ddn", "poids_kg", "taille_cm", "imc",
                    "iu_type", "severite_protections_j", "gene_10", "score_usp", "score_hav", "dysurie",
                    "qmax_ml_s", "volume_urine_ml", "rpm_ml", "courbe_normale", "proposition_rcp"
                ]
                
                for field in payload_fields:
                    if field in row and not pd.isna(row[field]):
                        payload[field] = str(row[field])
                
                # Antécédents
                if "antecedents" in row and not pd.isna(row["antecedents"]):
                    try:
                        payload["antecedents"] = json.loads(str(row["antecedents"]))
                    except:
                        payload["antecedents"] = {}
                else:
                    antecedents = {}
                    for col in df.columns:
                        if col.startswith("antecedents."):
                            key = col.replace("antecedents.", "")
                            if not pd.isna(row[col]):
                                antecedents[key] = str(row[col])
                    payload["antecedents"] = antecedents if antecedents else {}
                
                # Examen
                if "examen" in row and not pd.isna(row["examen"]):
                    try:
                        payload["examen"] = json.loads(str(row["examen"]))
                    except:
                        payload["examen"] = {}
                else:
                    examen = {}
                    for col in df.columns:
                        if col.startswith("examen."):
                            key = col.replace("examen.", "")
                            if not pd.isna(row[col]):
                                examen[key] = str(row[col])
                    payload["examen"] = examen if examen else {}
                
                created_at = row.get("created_at", now)
                updated_at = row.get("updated_at", now)
                
                # Vérifier si la fiche existe
                existing_updated_at = existing_fiches.get(fiche_id)
                
                operation = None
                if existing_updated_at:
                    # Comparer les dates (format ISO)
                    try:
                        existing_date = datetime.fromisoformat(existing_updated_at.replace("Z", "+00:00") if "Z" in existing_updated_at else existing_updated_at)
                        new_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00") if "Z" in updated_at else updated_at)
                        
                        # Ne mettre à jour que si la nouvelle date est plus récente
                        if new_date > existing_date:
                            operation = ("update", fiche_id, rcp_code, updated_at, payload)
                        else:
                            skipped_older += 1
                    except Exception:
                        # En cas d'erreur de parsing de date, mettre à jour quand même
                        operation = ("update", fiche_id, rcp_code, updated_at, payload)
                else:
                    operation = ("insert", fiche_id, rcp_code, created_at, updated_at, payload)
                
                if operation:
                    fiche_operations.append(operation)
                    
            except Exception as e:
                errors.append(f"Erreur fiche {row.get('id', 'unknown')}: {str(e)}")
        
        # Traiter les opérations par batch avec db_write
        for i in range(0, len(fiche_operations), batch_size):
            batch = fiche_operations[i:i+batch_size]
            
            def _process_batch(conn, cur):
                nonlocal imported_fiches, updated_fiches
                for op in batch:
                    if op[0] == "update":
                        _, fiche_id, rcp_code, updated_at, payload = op
                        cur.execute(
                            "UPDATE fiches SET rcp_code = ?, updated_at = ?, payload_json = ? WHERE id = ?",
                            (rcp_code, updated_at, json.dumps(payload, ensure_ascii=False), fiche_id)
                        )
                        updated_fiches += 1
                    elif op[0] == "insert":
                        _, fiche_id, rcp_code, created_at, updated_at, payload = op
                        cur.execute(
                            "INSERT INTO fiches (id, rcp_code, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                            (fiche_id, rcp_code, created_at, updated_at, json.dumps(payload, ensure_ascii=False))
                        )
                        imported_fiches += 1
            
            db_write(_process_batch)
        
        # Mettre à jour la date de modification de la RCP une seule fois à la fin
        if imported_fiches > 0 or updated_fiches > 0:
            def _update_rcp_date(conn, cur):
                cur.execute("UPDATE rcp SET updated_at = ? WHERE code = ?", (now, target_rcp_code))
            
            db_write(_update_rcp_date)
            # Invalider le cache
            get_all_rcp.clear()
            load_fiches.clear()
        
        message = f"Import terminé: {imported_fiches} fiches créées, {updated_fiches} fiches mises à jour dans la RCP {target_rcp_code}."
        if skipped_older > 0:
            message += f" {skipped_older} fiches ignorées (version plus ancienne conservée)."
        if errors:
            message += f" {len(errors)} erreurs."
        
        return {
            "success": True,
            "message": message,
            "imported_fiches": imported_fiches,
            "updated_fiches": updated_fiches,
            "errors": errors
        }
        
    except Exception as e:
        return {"success": False, "message": f"Erreur lors de l'import: {str(e)}"}


# ---------------------------
# Streamlit UI
# ---------------------------
def format_date_fr(date_str: str) -> str:
    """Formate une date au format 'jour mois année' en français."""
    if not date_str or date_str == 'None':
        return "Non renseignée"
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        mois_fr = [
            "janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre"
        ]
        jour = date_obj.day
        mois = mois_fr[date_obj.month - 1]
        annee = date_obj.year
        return f"{jour} {mois} {annee}"
    except:
        return date_str


def compute_imc(poids_kg, taille_cm):
    try:
        p = float(poids_kg) if poids_kg not in (None, "",) else None
        t = float(taille_cm) if taille_cm not in (None, "",) else None
        if p and t and t > 0:
            t_m = t / 100.0
            return round(p / (t_m * t_m), 1)
    except Exception:
        pass
    return None


def show_database_page():
    """Affiche toutes les fiches dans un tableau."""
    st.title("🗄️ Base de données")
    st.caption("Vue d'ensemble de toutes les fiches de toutes les RCP.")
    
    # Bouton retour
    if st.button("← Retour à l'accueil"):
        st.session_state["page"] = "home"
        st.rerun()
    
    st.divider()
    
    # Charger toutes les fiches
    df_all_fiches = load_fiches()
    
    if df_all_fiches.empty:
        st.info("Aucune fiche enregistrée.")
    else:
        # Sélectionner les colonnes à afficher
        display_columns = ["rcp_code", "patiente_nom", "rcp_date", "chirurgien", "created_at", "updated_at"]
        
        # Filtrer les colonnes qui existent
        available_columns = [col for col in display_columns if col in df_all_fiches.columns]
        
        # Créer un DataFrame avec les colonnes sélectionnées
        df_display = df_all_fiches[available_columns].copy()
        
        # Renommer les colonnes pour un affichage plus lisible
        column_names = {
            "rcp_code": "Code RCP",
            "patiente_nom": "Patiente",
            "rcp_date": "Date RCP",
            "chirurgien": "Chirurgien",
            "created_at": "Créée le",
            "updated_at": "Modifiée le"
        }
        df_display = df_display.rename(columns=column_names)
        
        # Formater les dates
        if "Créée le" in df_display.columns:
            df_display["Créée le"] = df_display["Créée le"].apply(lambda x: x[:10] if isinstance(x, str) and len(x) >= 10 else x)
        if "Modifiée le" in df_display.columns:
            df_display["Modifiée le"] = df_display["Modifiée le"].apply(lambda x: x[:10] if isinstance(x, str) and len(x) >= 10 else x)
        
        # Afficher le tableau
        st.dataframe(df_display, use_container_width=True, height=600)
        
        # Statistiques
        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Nombre total de fiches", len(df_all_fiches))
        with col2:
            nb_rcp = df_all_fiches["rcp_code"].nunique() if "rcp_code" in df_all_fiches.columns else 0
            st.metric("Nombre de RCP", nb_rcp)
        with col3:
            if "patiente_nom" in df_all_fiches.columns:
                nb_patientes = df_all_fiches["patiente_nom"].notna().sum()
                st.metric("Fiches avec patiente renseignée", nb_patientes)


def show_liste_rcp_page():
    """Affiche la page avec la liste de toutes les RCP."""
    st.title("📋 Liste des RCP")
    st.caption("Vue d'ensemble de toutes les RCP créées.")
    
    # Liste des RCP
    rcp_list = get_all_rcp()
    if not rcp_list:
        st.info("Aucune RCP créée.")
    else:
        # Créer un DataFrame pour un affichage en tableau
        rcp_data = []
        for rcp in rcp_list:
            date_rcp_display = format_date_fr(rcp.get('date_rcp', ''))
            created_date = rcp['created_at'][:10] if len(rcp['created_at']) >= 10 else rcp['created_at']
            updated_date = rcp['updated_at'][:10] if len(rcp['updated_at']) >= 10 else rcp['updated_at']
            status = "Archivée" if rcp.get('is_archived', False) else "Active"
            
            rcp_data.append({
                "Date RCP": date_rcp_display,
                "Code": rcp['code'],
                "Nombre de fiches": rcp['nb_fiches'],
                "Statut": status,
                "Créée le": created_date,
                "Modifiée le": updated_date
            })
        
        df_rcp = pd.DataFrame(rcp_data)
        
        # Afficher le tableau
        st.dataframe(
            df_rcp,
            use_container_width=True,
            height=400,
            hide_index=True
        )
        
        # Statistiques
        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Nombre total de RCP", len(rcp_list))
        with col2:
            nb_archived = sum(1 for r in rcp_list if r.get('is_archived', False))
            st.metric("RCP archivées", nb_archived)
        with col3:
            total_fiches = sum(r['nb_fiches'] for r in rcp_list)
            st.metric("Total de fiches", total_fiches)
        
        # Liste détaillée avec boutons d'action
        st.markdown("---")
        st.markdown("### Accès rapide aux RCP")
        for rcp in rcp_list:
            with st.container():
                col1, col2, col3, col4, col5 = st.columns([3, 2, 2, 2, 1])
                with col1:
                    date_rcp_display = format_date_fr(rcp.get('date_rcp', ''))
                    status_text = " (Archivée)" if rcp.get('is_archived', False) else ""
                    st.markdown(f"**Date RCP:** {date_rcp_display}{status_text}")
                with col2:
                    st.markdown(f"**Fiches:** {rcp['nb_fiches']}")
                with col3:
                    created_date = rcp['created_at'][:10] if len(rcp['created_at']) >= 10 else rcp['created_at']
                    st.caption(f"Créé le: {created_date}")
                with col4:
                    updated_date = rcp['updated_at'][:10] if len(rcp['updated_at']) >= 10 else rcp['updated_at']
                    st.caption(f"Modifié le: {updated_date}")
                with col5:
                    if st.button("📂 Ouvrir", key=f"open_liste_{rcp['code']}"):
                        st.session_state["page"] = "rcp_detail"
                        st.session_state["current_rcp_code"] = rcp['code']
                        st.rerun()
                st.divider()


def show_home_page():
    """Affiche la page d'accueil avec la liste des RCP."""
    st.title("📋 RCP pelvi-périnéo")
    st.caption("Sélectionnez une RCP pour voir ses fiches ou créez-en une nouvelle.")
    
    # Formulaire pour créer une nouvelle RCP
    with st.expander("➕ Créer une nouvelle RCP", expanded=False):
        with st.form("create_rcp_form"):
            date_rcp = st.date_input("Date de la RCP", value=datetime.now().date())
            submitted = st.form_submit_button("Créer la RCP", type="primary")
            if submitted:
                date_str = date_rcp.strftime("%Y-%m-%d")
                new_code = create_rcp(date_str)
                st.session_state["page"] = "rcp_detail"
                st.session_state["current_rcp_code"] = new_code
                date_formatted = format_date_fr(date_str)
                st.success(f"RCP créée avec succès (Date: {date_formatted})")
                st.rerun()

    st.divider()
    
    # Liste des RCP
    rcp_list = get_all_rcp()
    if not rcp_list:
        st.info("Aucune RCP créée. Créez-en une nouvelle ci-dessus.")
    else:
        st.markdown("### Liste des RCPs")
        for rcp in rcp_list:
            with st.container():
                col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
                with col1:
                    date_rcp_display = format_date_fr(rcp.get('date_rcp', ''))
                    st.markdown(f"**Date RCP:** {date_rcp_display}")
                with col2:
                    st.markdown(f"**Fiches:** {rcp['nb_fiches']}")
                with col3:
                    created_date = rcp['created_at'][:10] if len(rcp['created_at']) >= 10 else rcp['created_at']
                    st.caption(f"Créé le: {created_date}")
                with col4:
                    if st.button("📂 Ouvrir", key=f"open_{rcp['code']}"):
                        st.session_state["page"] = "rcp_detail"
                        st.session_state["current_rcp_code"] = rcp['code']
                        st.rerun()
                st.divider()


def show_rcp_detail_page(rcp_code: str):
    """Affiche la page de détail d'une RCP avec ses fiches."""
    # Récupérer les infos de la RCP
    rcp_list = get_all_rcp()
    current_rcp = next((r for r in rcp_list if r['code'] == rcp_code), None)
    
    # Afficher le titre avec la date de la RCP
    date_rcp_display = "RCP"
    if current_rcp and current_rcp.get('date_rcp') and current_rcp['date_rcp'] != 'None':
        date_formatted = format_date_fr(current_rcp['date_rcp'])
        if date_formatted != "Non renseignée":
            date_rcp_display = f"RCP du {date_formatted}"
        else:
            date_rcp_display = "RCP"
    
    # Vérifier si la RCP est archivée
    is_archived = current_rcp.get('is_archived', False) if current_rcp else False
    
    # Titre avec indicateur d'archivage
    title_text = f"📁 {date_rcp_display}"
    if is_archived:
        title_text += " (Archivée)"
    st.title(title_text)
    
    # Boutons d'action en haut
    col1, col2, col3, col4, col5, col6, col7 = st.columns([1, 1, 1, 1, 1, 1, 3])
    with col1:
        if st.button("← Retour à l'accueil"):
            st.session_state["page"] = "home"
            st.session_state["current_rcp_code"] = None
            st.rerun()
    with col2:
        if st.button("➕ Ajouter une fiche", type="primary", disabled=is_archived):
            st.session_state["page"] = "fiche_form"
            st.session_state["current_fiche_id"] = str(uuid.uuid4())
            st.rerun()
    with col3:
        if st.button("📄 Générer PDF", key="btn_pdf"):
            # Fermer les autres expanders et ouvrir celui-ci
            st.session_state["show_pdf_dialog"] = True
            st.session_state["show_export_dialog"] = False
            st.session_state["show_import_dialog"] = False
            st.rerun()
    with col4:
        if st.button("📥 Exporter CSV", key="btn_export_csv", disabled=is_archived):
            # Fermer les autres expanders et ouvrir celui-ci
            st.session_state["show_pdf_dialog"] = False
            st.session_state["show_export_dialog"] = True
            st.session_state["show_import_dialog"] = False
            st.rerun()
    with col5:
        if st.button("📤 Importer CSV", key="btn_import_csv", disabled=is_archived):
            # Fermer les autres expanders et ouvrir celui-ci
            st.session_state["show_pdf_dialog"] = False
            st.session_state["show_export_dialog"] = False
            st.session_state["show_import_dialog"] = True
            st.rerun()
    with col6:
        # Bouton Archiver/Désarchiver en haut à droite
        if is_archived:
            if st.button("📦 Désarchiver", type="secondary", key="btn_unarchive"):
                archive_rcp(rcp_code, False)
                st.success("RCP désarchivée.")
                st.rerun()
        else:
            if st.button("📦 Archiver", type="secondary", key="btn_archive"):
                archive_rcp(rcp_code, True)
                st.success("RCP archivée.")
                st.rerun()
    
    # Modal pour Générer PDF
    if st.session_state.get("show_pdf_dialog", False):
        col_title, col_close = st.columns([10, 1])
        with col_title:
            st.markdown("### 📄 Générer PDF du dossier")
        with col_close:
            if st.button("❌", key="close_pdf_x", help="Fermer"):
                st.session_state["show_pdf_dialog"] = False
                st.rerun()
        
        st.caption(f"Génère un PDF complet avec toutes les fiches de la RCP.")
        
        df_check = load_fiches(rcp_code)
        if df_check.empty:
            st.warning("Aucune fiche dans cette RCP.")
        else:
            if st.button("Générer les PDFs", type="primary", key="generate_pdf_btn"):
                try:
                    pdf_paths = generate_pdf_rcp(rcp_code)
                    st.success(f"{len(pdf_paths)} PDF(s) généré(s) avec succès.")
                    for pdf_path in pdf_paths:
                        st.code(pdf_path)
                        with open(pdf_path, "rb") as f:
                            filename = os.path.basename(pdf_path)
                            st.download_button(
                                f"Télécharger {filename}",
                                f,
                                file_name=filename,
                                mime="application/pdf",
                                key=f"dl_{filename}"
                            )
                except Exception as e:
                    st.error(f"Erreur lors de la génération des PDFs: {str(e)}")
        
        st.divider()
    
    # Modal pour Exporter CSV (seulement si non archivée)
    if st.session_state.get("show_export_dialog", False) and not is_archived:
        col_title, col_close = st.columns([10, 1])
        with col_title:
            st.markdown("### 📥 Exporter CSV")
        with col_close:
            if st.button("❌", key="close_export_x", help="Fermer"):
                st.session_state["show_export_dialog"] = False
                st.rerun()
        
        st.caption(f"Exporte cette RCP et ses fiches pour synchronisation.")
        
        if st.button("Exporter", type="primary", key="export_csv_btn"):
            csv_path = export_rcp_to_csv(rcp_code)
            if csv_path:
                st.success("CSV généré avec succès.")
                st.code(csv_path)
                with open(csv_path, "rb") as f:
                    st.download_button("Télécharger le CSV", f, file_name=os.path.basename(csv_path), mime="text/csv")
            else:
                st.warning("Aucune donnée à exporter.")
        
        st.divider()
    
    # Modal pour Importer CSV (seulement si non archivée)
    if st.session_state.get("show_import_dialog", False) and not is_archived:
        col_title, col_close = st.columns([10, 1])
        with col_title:
            st.markdown("### 📤 Importer CSV")
        with col_close:
            if st.button("❌", key="close_import_x", help="Fermer"):
                st.session_state["show_import_dialog"] = False
                st.rerun()
        
        st.caption(f"Importez un fichier CSV pour synchroniser les fiches dans cette RCP ({rcp_code}).")
        
        uploaded_file = st.file_uploader("Choisir un fichier CSV", type=["csv"], key=f"import_csv_{rcp_code}")
        if uploaded_file is not None:
            result = import_rcp_from_csv(uploaded_file, rcp_code)
            if result["success"]:
                st.success(result["message"])
                if result.get("errors"):
                    with st.expander("Voir les erreurs"):
                        for error in result["errors"]:
                            st.text(error)
                st.session_state["show_import_dialog"] = False
                st.rerun()
            else:
                st.error(result["message"])
        
        st.divider()
    
    st.divider()
    
    # Champ pour la liste des médecins présents
    st.markdown("### Médecins présents")
    medecins_presents = get_rcp_medecins_presents(rcp_code)
    medecins_presents_input = st.text_area(
        "Liste des médecins présents (un par ligne ou séparés par des virgules)",
        value=medecins_presents,
        height=100,
        disabled=is_archived,
        key="medecins_presents_input"
    )
    if not is_archived:
        if st.button("💾 Enregistrer la liste des médecins", key="save_medecins"):
            update_rcp_medecins_presents(rcp_code, medecins_presents_input)
            st.success("Liste des médecins enregistrée.")
            st.rerun()
    
    st.divider()
    
    # Liste des fiches
    df_fiches = load_fiches(rcp_code)
    if df_fiches.empty:
        st.info("Aucune fiche dans ce dossier. Cliquez sur 'Ajouter une fiche' pour commencer.")
    else:
        st.markdown("### Fiches du dossier")
        for idx, row in df_fiches.iterrows():
            with st.container():
                col1, col2, col3, col4 = st.columns([3, 2, 2, 1])
                with col1:
                    patiente_nom = row.get("patiente_nom", "Non renseigné")
                    st.markdown(f"**{patiente_nom}**")
                with col2:
                    rcp_date = row.get("rcp_date", "N/A")
                    st.caption(f"Date RCP: {rcp_date}")
                with col3:
                    created_date = row.get("created_at", "")[:10] if len(str(row.get("created_at", ""))) >= 10 else "N/A"
                    st.caption(f"Créée: {created_date}")
                with col4:
                    if st.button("✏️ Modifier", key=f"edit_{row['id']}"):
                        st.session_state["page"] = "fiche_form"
                        st.session_state["current_fiche_id"] = row['id']
                        st.rerun()
                st.divider()
    
    # Bouton supprimer RCP
    st.markdown("---")
    if st.button("🗑️ Supprimer cette RCP", type="secondary"):
        delete_rcp(rcp_code)
        st.session_state["page"] = "home"
        st.session_state["current_rcp_code"] = None
        st.success("RCP supprimée.")
        st.rerun()


def save_fiche_data(fiche_id: str, rcp_code: str, rcp_date: str, chirurgien: str, patiente_nom: str, 
                   patiente_ddn: str, motif_value: str, poids_kg: str, taille_cm: str, imc: str,
                   antecedents: dict, antecedents_texte_libre: str, severite: str, gene_10: str,
                   score_usp: str, score_hav: str, dysurie: str, symptomes_texte_libre: str,
                   examen: dict, examen_texte_libre: str, qmax: str, volume_urine: str, rpm: str,
                   courbe_normale: str, examens_imagerie: str, proposition_rcp: str):
    """Fonction helper pour enregistrer une fiche avec toutes les valeurs."""
    try:
        rcp_date_val = rcp_date.strip() if rcp_date else ""
        chirurgien_val = chirurgien.strip() if chirurgien else ""
        patiente_nom_val = patiente_nom.strip() if patiente_nom else ""
        patiente_ddn_val = patiente_ddn.strip() if patiente_ddn else ""
        poids_kg_val = poids_kg.strip() if poids_kg else ""
        taille_cm_val = taille_cm.strip() if taille_cm else ""
        severite_val = severite.strip() if severite else ""
        gene_10_val = gene_10.strip() if gene_10 else ""
        score_usp_val = score_usp.strip() if score_usp else ""
        score_hav_val = score_hav.strip() if score_hav else ""
        dysurie_val = dysurie.strip() if dysurie else ""
        qmax_val = qmax.strip() if qmax else ""
        volume_urine_val = volume_urine.strip() if volume_urine else ""
        rpm_val = rpm.strip() if rpm else ""
        proposition_rcp_val = proposition_rcp.strip() if proposition_rcp else ""
        antecedents_texte_libre_val = antecedents_texte_libre.strip() if antecedents_texte_libre else ""
        symptomes_texte_libre_val = symptomes_texte_libre.strip() if symptomes_texte_libre else ""
        examen_texte_libre_val = examen_texte_libre.strip() if examen_texte_libre else ""
        examens_imagerie_val = examens_imagerie.strip() if examens_imagerie else ""
        
        payload_out = {
            "rcp_date": rcp_date_val,
            "chirurgien": chirurgien_val,
            "patiente_nom": patiente_nom_val,
            "patiente_ddn": patiente_ddn_val,
            "motif": motif_value,
            "poids_kg": poids_kg_val,
            "taille_cm": taille_cm_val,
            "imc": imc,
            "antecedents": antecedents,
            "antecedents_texte_libre": antecedents_texte_libre_val,
            "iu_type": motif_value,  # Garder pour compatibilité
            "severite_protections_j": severite_val,
            "gene_10": gene_10_val,
            "score_usp": score_usp_val,
            "score_hav": score_hav_val,
            "dysurie": dysurie_val,
            "symptomes_texte_libre": symptomes_texte_libre_val,
            "examen": examen,
            "examen_texte_libre": examen_texte_libre_val,
            "qmax_ml_s": qmax_val,
            "volume_urine_ml": volume_urine_val,
            "rpm_ml": rpm_val,
            "courbe_normale": courbe_normale,
            "examens_imagerie": examens_imagerie_val,
            "proposition_rcp": proposition_rcp_val,
        }
        upsert_fiche(fiche_id, rcp_code, payload_out)
        return True
    except Exception as e:
        st.error(f"Erreur lors de l'enregistrement: {str(e)}")
        import traceback
        st.code(traceback.format_exc())
        return False


def show_fiche_form_page(rcp_code: str, fiche_id: str):
    """Affiche le formulaire de saisie/modification d'une fiche."""
    st.title("📝 Formulaire de fiche")
    
    # Charger les données de la fiche pour vérifier si elle existe
    fiche_data = get_fiche_by_id(fiche_id)
    payload = {}
    if fiche_data:
        payload = fiche_data["payload"]
    
    
    # Boutons d'action
    col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 1, 8])
    with col1:
        if st.button("← Retour à la RCP"):
            st.session_state["page"] = "rcp_detail"
            st.rerun()
    with col2:
        # Afficher le bouton supprimer seulement si la fiche existe
        if fiche_data:
            if st.button("🗑️ Supprimer la fiche", type="secondary"):
                delete_fiche(fiche_id)
                st.success("Fiche supprimée.")
                st.session_state["page"] = "rcp_detail"
                st.rerun()
    with col3:
        # Afficher le bouton transférer seulement si la fiche existe
        if fiche_data:
            if st.button("🔄 Transférer à une autre RCP"):
                st.session_state["show_transfer"] = True
                st.rerun()
    with col4:
        # Bouton générer PDF de la fiche (seulement si la fiche existe)
        if fiche_data:
            if st.button("📄 Générer PDF", key="generate_pdf_fiche_btn"):
                try:
                    payload = fiche_data["payload"]
                    pdf_path = generate_pdf_fiche(fiche_id, payload, rcp_code)
                    st.session_state["generated_pdf_path"] = pdf_path
                    st.session_state["show_pdf_fiche_dialog"] = True
                    st.rerun()
                except Exception as e:
                    st.error(f"Erreur lors de la génération du PDF: {str(e)}")
    
    # Dialog pour afficher le PDF généré
    if st.session_state.get("show_pdf_fiche_dialog", False) and st.session_state.get("generated_pdf_path"):
        st.divider()
        col_title, col_close = st.columns([10, 1])
        with col_title:
            st.markdown("### 📄 PDF généré")
        with col_close:
            if st.button("❌", key="close_pdf_fiche_x", help="Fermer"):
                st.session_state["show_pdf_fiche_dialog"] = False
                st.session_state["generated_pdf_path"] = None
                st.rerun()
        
        pdf_path = st.session_state["generated_pdf_path"]
        st.success("PDF généré avec succès.")
        st.code(pdf_path)
        with open(pdf_path, "rb") as f:
            st.download_button(
                "Télécharger le PDF",
                f,
                file_name=os.path.basename(pdf_path),
                mime="application/pdf",
                key="dl_fiche_pdf"
            )
        st.divider()
    
    # Interface de transfert
    if st.session_state.get("show_transfer", False) and fiche_data:
        st.divider()
        st.markdown("### Transférer la fiche vers une autre RCP")
        
        # Récupérer toutes les RCP (sauf la RCP actuelle)
        all_rcp = get_all_rcp()
        rcp_options = [r for r in all_rcp if r['code'] != rcp_code]
        
        if not rcp_options:
            st.warning("Aucune autre RCP disponible pour le transfert.")
        else:
            # Créer une liste d'options avec la date de la RCP
            rcp_display = []
            for r in rcp_options:
                date_str = format_date_fr(r.get('date_rcp', ''))
                rcp_display.append(f"{r['code']} - {date_str}")
            
            selected_idx = st.selectbox(
                "Sélectionner la RCP de destination",
                range(len(rcp_display)),
                format_func=lambda x: rcp_display[x] if x < len(rcp_display) else ""
            )
            
            col1, col2 = st.columns([1, 5])
            with col1:
                if st.button("✅ Confirmer le transfert", type="primary"):
                    target_rcp = rcp_options[selected_idx]['code']
                    if transfer_fiche(fiche_id, target_rcp):
                        st.success(f"Fiche transférée vers la RCP {target_rcp}.")
                        st.session_state["show_transfer"] = False
                        st.session_state["current_rcp_code"] = target_rcp
                        st.session_state["page"] = "rcp_detail"
                        st.rerun()
                    else:
                        st.error("Erreur lors du transfert.")
            with col2:
                if st.button("❌ Annuler"):
                    st.session_state["show_transfer"] = False
                    st.rerun()
    
    # Masquer le code RCP et l'ID de la fiche (commentés pour ne plus les afficher)
    # st.markdown(f"**RCP:** {rcp_code}")
    # st.markdown(f"**ID Fiche:** {fiche_id}")
    st.divider()
    
    # Charger le payload si la fiche existe
    payload = {}
    if fiche_data:
        payload = fiche_data["payload"]
        # Vérifier que la fiche appartient bien à la RCP courante
        if fiche_data["rcp_code"] != rcp_code:
            st.warning(f"Cette fiche appartient à la RCP {fiche_data['rcp_code']}, pas à {rcp_code}.")
    
    # Récupérer la date de la RCP depuis la base de données
    rcp_date_from_db = get_rcp_date(rcp_code)
    
    # Utiliser la date de la RCP si c'est une nouvelle fiche, sinon utiliser la valeur existante
    default_rcp_date = rcp_date_from_db if not fiche_data else payload.get("rcp_date", rcp_date_from_db)

    with st.form("fiche_form", clear_on_submit=False):
        # Bouton enregistrer en haut du formulaire
        submitted_top = st.form_submit_button("💾 Enregistrer la fiche", type="primary", key="form_submit_button_top")
        
        # Section Identité
        st.markdown("### Identité")
        with st.container():
            col1, col2 = st.columns(2)
            with col1:
                rcp_date = st.text_input("Date de la RCP", value=default_rcp_date, disabled=True, key="form_rcp_date")
                patiente_nom = st.text_input("Identité patiente (Nom, Prénom)", value=payload.get("patiente_nom", ""), key="form_patiente_nom")
            with col2:
                patiente_ddn = st.text_input("DDN", value=payload.get("patiente_ddn", ""), key="form_patiente_ddn")
                chirurgien = st.text_input("Chirurgien responsable", value=payload.get("chirurgien", ""), key="form_chirurgien")
        
        # Champ Motif à la fin de la section Identité (synchronisé avec celui des Symptômes)
        motif_options = ["IUE", "POP", "autre"]
        # Initialiser la valeur partagée depuis le payload
        if "motif_shared" not in st.session_state:
            motif_value_shared = payload.get("motif", payload.get("iu_type", "IUE"))
            # Convertir l'ancienne valeur si nécessaire
            if motif_value_shared not in motif_options:
                if "effort" in str(motif_value_shared).lower():
                    motif_value_shared = "IUE"
                elif "prolapsus" in str(motif_value_shared).lower() or "pop" in str(motif_value_shared).lower():
                    motif_value_shared = "POP"
                else:
                    motif_value_shared = "autre"
            st.session_state.motif_shared = motif_value_shared
        
        try:
            motif_index_identite = motif_options.index(st.session_state.motif_shared) if st.session_state.motif_shared in motif_options else 0
        except:
            motif_index_identite = 0
        motif_identite = st.selectbox("Motif", motif_options, index=motif_index_identite, key="motif_identite")
        # Mettre à jour la valeur partagée (utiliser .get() pour éviter les erreurs)
        if "motif_shared" not in st.session_state or st.session_state.get("motif_shared") != motif_identite:
            # On ne peut pas modifier directement dans un formulaire, donc on stocke après
            pass
        # La valeur sera accessible via la clé "motif_identite" dans session_state
        
        st.divider()
        
        # Section Antécédents
        st.markdown("### Antécédents / contexte")
        a1, a2, a3 = st.columns(3)

        # Checkboxes Oui/Non avec possibilité de décocher (NA si aucun coché)
        def yn(label, key):
            current = payload.get("antecedents", {}).get(key, "NA")
            with st.container():
                st.markdown(f"**{label}**")
                col_oui, col_non = st.columns(2)
                with col_oui:
                    checked_oui = st.checkbox("✓ Oui", value=(current == "Oui"), key=f"oui_{key}")
                with col_non:
                    checked_non = st.checkbox("✗ Non", value=(current == "Non"), key=f"non_{key}")
                st.markdown("<br>", unsafe_allow_html=True)  # Espacement
            
            # Si les deux sont cochés, prioriser "Oui"
            if checked_oui:
                return "Oui"
            elif checked_non:
                return "Non"
            else:
                return "NA"

        antecedents = {}
        with a1:
            antecedents["Rééducation périnéo-sphinctérienne"] = yn("Rééducation périnéo-sphinctérienne", "Rééducation périnéo-sphinctérienne")
            antecedents["ATCD maladie neurologique"] = yn("ATCD maladie neurologique", "ATCD maladie neurologique")
            antecedents["ATCD chirurgie incontinence urinaire"] = yn("ATCD chirurgie incontinence urinaire", "ATCD chirurgie incontinence urinaire")

        with a2:
            antecedents["ATCD chirurgie prolapsus (POP)"] = yn("ATCD chirurgie prolapsus (POP)", "ATCD chirurgie prolapsus (POP)")
            antecedents["ATCD chirurgie pelvienne autre que POP"] = yn("ATCD chirurgie pelvienne autre que POP", "ATCD chirurgie pelvienne autre que POP")
            antecedents["ATCD irradiation pelvienne"] = yn("ATCD irradiation pelvienne", "ATCD irradiation pelvienne")

        with a3:
            antecedents["Troubles ano-rectaux"] = yn("Troubles ano-rectaux", "Troubles ano-rectaux")
            antecedents["Troubles génito-sexuels"] = yn("Troubles génito-sexuels", "Troubles génito-sexuels")
            antecedents["Ménopause"] = yn("Ménopause", "Ménopause")
        
        # Champ texte libre pour les antécédents
        st.markdown("---")
        antecedents_texte_libre = st.text_area("**Autres antécédents / remarques**", value=payload.get("antecedents_texte_libre", ""), height=80, key="form_antecedents_texte_libre")
        # Les antécédents seront reconstruits depuis les checkboxes dans session_state

        st.divider()
        
        # Section Symptômes
        st.markdown("### Symptômes / interrogatoire")
        with st.container():
            col_s1, col_s2 = st.columns(2)
            with col_s1:
                severite = st.text_input("Sévérité (nombre de protections / jour)", value=str(payload.get("severite_protections_j", "") or ""), key="form_severite")
            with col_s2:
                gene_10 = st.text_input("Gêne globale (/10)", value=str(payload.get("gene_10", "") or ""), key="form_gene_10")
                st.markdown("")  # Espacement

            c1, c2, c3 = st.columns(3)
            with c1:
                score_usp = st.text_input("Score USP", value=str(payload.get("score_usp", "") or ""), key="form_score_usp")
            with c2:
                score_hav = st.text_input("Score HAV", value=str(payload.get("score_hav", "") or ""), key="form_score_hav")
            with c3:
                dysurie = st.text_input("Dysurie", value=str(payload.get("dysurie", "") or ""), key="form_dysurie")
        
        # Champ texte libre pour les symptômes
        st.markdown("---")
        symptomes_texte_libre = st.text_area("**Autres symptômes / remarques**", value=payload.get("symptomes_texte_libre", ""), height=80, key="form_symptomes_texte_libre")

        st.divider()
        
        # Section Examen clinique
        st.markdown("### Examen clinique")
        
        # Anthropométrie au début de l'examen clinique
        st.markdown("#### Anthropométrie")
        anthro_col1, anthro_col2, anthro_col3 = st.columns(3)
        with anthro_col1:
            poids_kg = st.text_input("Poids (kg)", value=str(payload.get("poids_kg", "") or ""), key="form_poids_kg")
        with anthro_col2:
            taille_cm = st.text_input("Taille (cm)", value=str(payload.get("taille_cm", "") or ""), key="form_taille_cm")
        with anthro_col3:
            imc = compute_imc(poids_kg, taille_cm)
            st.text_input("IMC (calculé)", value=str(imc or ""), disabled=True, key="form_imc")
            # L'IMC sera recalculé depuis poids_kg et taille_cm quand nécessaire
        
        st.markdown("---")
        st.markdown("#### Examen physique")
        ex1, ex2, ex3 = st.columns(3)

        def ex_yn(label, key):
            current = payload.get("examen", {}).get(key, "NA")
            with st.container():
                st.markdown(f"**{label}**")
                col_oui, col_non = st.columns(2)
                with col_oui:
                    checked_oui = st.checkbox("✓ Oui", value=(current == "Oui"), key=f"ex_oui_{key}")
                with col_non:
                    checked_non = st.checkbox("✗ Non", value=(current == "Non"), key=f"ex_non_{key}")
                st.markdown("<br>", unsafe_allow_html=True)  # Espacement
            
            # Si les deux sont cochés, prioriser "Oui"
            if checked_oui:
                return "Oui"
            elif checked_non:
                return "Non"
            else:
                return "NA"

        examen = {}
        with ex1:
            examen["Hypermobilité urétrale"] = ex_yn("Hypermobilité urétrale", "Hypermobilité urétrale")
            # Test à la toux avec checkboxes
            test_toux_current = payload.get("examen", {}).get("Test à la toux (positif)", "NA")
            with st.container():
                st.markdown("**Test à la toux**")
                col_positif, col_negatif = st.columns(2)
                with col_positif:
                    checked_positif = st.checkbox("✓ Positif", value=(test_toux_current == "Positif"), key="test_toux_positif")
                with col_negatif:
                    checked_negatif = st.checkbox("✗ Négatif", value=(test_toux_current == "Négatif"), key="test_toux_negatif")
                st.markdown("<br>", unsafe_allow_html=True)  # Espacement
            if checked_positif:
                examen["Test à la toux (positif)"] = "Positif"
            elif checked_negatif:
                examen["Test à la toux (positif)"] = "Négatif"
            else:
                examen["Test à la toux (positif)"] = "NA"
        with ex2:
            # Manœuvre de soutènement avec checkboxes
            manoeuvre_current = payload.get("examen", {}).get("Manœuvre de soutènement (positive)", "NA")
            with st.container():
                st.markdown("**Manœuvre de soutènement**")
                col_positive, col_negative = st.columns(2)
                with col_positive:
                    checked_positive = st.checkbox("✓ Positive", value=(manoeuvre_current == "Positive"), key="manoeuvre_positive")
                with col_negative:
                    checked_negative = st.checkbox("✗ Négative", value=(manoeuvre_current == "Négative"), key="manoeuvre_negative")
                st.markdown("<br>", unsafe_allow_html=True)  # Espacement
            if checked_positive:
                examen["Manœuvre de soutènement (positive)"] = "Positive"
            elif checked_negative:
                examen["Manœuvre de soutènement (positive)"] = "Négative"
            else:
                examen["Manœuvre de soutènement (positive)"] = "NA"
            examen["Inversion de commande"] = ex_yn("Inversion de commande", "Inversion de commande")
        with ex3:
            examen["Prolapsus associé"] = ex_yn("Prolapsus associé", "Prolapsus associé")
            testing_releveurs_input = st.text_input(
                "Testing des releveurs (/5)",
                value=str(payload.get("examen", {}).get("Testing des releveurs (/5)", "") or ""),
                key="form_testing_releveurs"
            )
            examen["Testing des releveurs (/5)"] = testing_releveurs_input
        
        # Champ texte libre pour l'examen clinique
        st.markdown("---")
        examen_texte_libre = st.text_area("**Autres observations / remarques**", value=payload.get("examen_texte_libre", ""), height=80, key="form_examen_texte_libre")
        # L'examen sera reconstruit depuis les checkboxes dans session_state

        st.divider()
        
        # Section Débitmétrie
        st.markdown("### Débitmétrie / bilan uro")
        with st.container():
            d1, d2, d3, d4 = st.columns(4)
            with d1:
                qmax = st.text_input("Qmax (mL/s)", value=str(payload.get("qmax_ml_s", "") or ""), key="form_qmax")
            with d2:
                volume_urine = st.text_input("Volume uriné (mL)", value=str(payload.get("volume_urine_ml", "") or ""), key="form_volume_urine")
            with d3:
                rpm = st.text_input("RPM (mL)", value=str(payload.get("rpm_ml", "") or ""), key="form_rpm")
            with d4:
                courbe_normale = st.radio(
                    "Courbe normale",
                    ["Non", "Oui"],
                    index=0 if payload.get("courbe_normale", "Non") == "Non" else 1,
                    horizontal=True,
                    key="form_courbe_normale"
                )

        st.divider()
        
        # Section Examens d'imagerie
        st.markdown("### Examens d'imagerie")
        examens_imagerie = st.text_area("**Examens d'imagerie**", value=payload.get("examens_imagerie", ""), height=120, key="form_examens_imagerie")
        
        st.divider()
        
        # Section Proposition
        st.markdown("### Proposition de la RCP")
        proposition_rcp = st.text_area("**Proposition / synthèse**", value=payload.get("proposition_rcp", ""), height=120, key="form_proposition_rcp")

        # Bouton enregistrer en bas du formulaire
        submitted = st.form_submit_button("💾 Enregistrer la fiche", type="primary", key="form_submit_button")

        # Exécuter l'enregistrement si l'un des boutons du formulaire est cliqué
        if submitted or submitted_top:
            # Récupérer la valeur du motif depuis le widget de la section Identité
            motif_value = motif_identite
            
            # Utiliser la fonction helper pour enregistrer
            success = save_fiche_data(
                fiche_id, rcp_code, rcp_date, chirurgien, patiente_nom, patiente_ddn,
                motif_value, poids_kg, taille_cm, imc, antecedents, antecedents_texte_libre,
                severite, gene_10, score_usp, score_hav, dysurie, symptomes_texte_libre,
                examen, examen_texte_libre, qmax, volume_urine, rpm, courbe_normale, examens_imagerie, proposition_rcp
            )
            
            if success:
                # Mettre à jour motif_shared pour la synchronisation (après la soumission du formulaire)
                st.session_state.motif_shared = motif_value
                st.success("Fiche enregistrée.")
                # Retourner à la page de détail de la RCP
                st.session_state["page"] = "rcp_detail"
                st.session_state["current_rcp_code"] = rcp_code
                st.rerun()


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    
    # CSS personnalisé pour améliorer la présentation
    st.markdown("""
        <style>
        /* Empêcher l'étirement vertical des boutons dans les colonnes */
        div[data-testid="column"] button {
            height: auto !important;
            min-height: 38px !important;
            padding-top: 0.5rem !important;
            padding-bottom: 0.5rem !important;
            white-space: normal !important;
            word-wrap: break-word !important;
        }
        /* S'assurer que les colonnes n'étirent pas leur contenu verticalement */
        div[data-testid="column"] {
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            justify-content: flex-start;
        }
        /* Empêcher les colonnes de forcer une hauteur minimale */
        div[data-testid="column"] > div {
            height: auto !important;
            min-height: auto !important;
        }
        /* Améliorer l'espacement des sections */
        h3 {
            margin-top: 1.5rem !important;
            margin-bottom: 1rem !important;
            padding-bottom: 0.5rem !important;
            border-bottom: 2px solid #e0e0e0 !important;
        }
        h4 {
            margin-top: 1rem !important;
            margin-bottom: 0.75rem !important;
            color: #666 !important;
        }
        /* Améliorer l'espacement des conteneurs */
        .stContainer {
            padding: 0.5rem 0 !important;
        }
        /* Améliorer la lisibilité des checkboxes */
        label[data-baseweb="checkbox"] {
            font-size: 0.95rem !important;
        }
        </style>
    """, unsafe_allow_html=True)
    
    ensure_dirs()
    init_db()
    
    # Initialiser la navigation
    if "page" not in st.session_state:
        st.session_state["page"] = "home"
    
    # Navigation principale (sidebar)
    with st.sidebar:
        st.markdown("## Navigation")
        if st.button("🏠 Accueil"):
            st.session_state["page"] = "home"
            st.rerun()
        if st.button("📋 Liste RCP"):
            st.session_state["page"] = "liste_rcp"
            st.rerun()
        if st.button("🗄️ Base de données"):
            st.session_state["page"] = "database"
            st.rerun()
    st.divider()
    
    # Afficher la page appropriée
    if st.session_state["page"] == "home":
        show_home_page()
    elif st.session_state["page"] == "liste_rcp":
        show_liste_rcp_page()
    elif st.session_state["page"] == "database":
        show_database_page()
    elif st.session_state["page"] == "rcp_detail":
        current_rcp_code = st.session_state.get("current_rcp_code")
        if current_rcp_code:
            show_rcp_detail_page(current_rcp_code)
        else:
            st.error("Aucune RCP sélectionnée.")
            st.session_state["page"] = "home"
            st.rerun()
    elif st.session_state["page"] == "fiche_form":
        current_rcp_code = st.session_state.get("current_rcp_code")
        current_fiche_id = st.session_state.get("current_fiche_id")
        if current_rcp_code and current_fiche_id:
            show_fiche_form_page(current_rcp_code, current_fiche_id)
        else:
            st.error("Erreur: RCP ou fiche non définie.")
            st.session_state["page"] = "home"
            st.rerun()


if __name__ == "__main__":
    main()
