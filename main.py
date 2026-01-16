"""
MonDevisPro API
Génère des devis et factures PDF + Word professionnels
Version 3.0.0
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import uuid
from datetime import datetime, timedelta
import requests
from io import BytesIO

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# Word
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml
# Supabase Storage
from supabase import create_client, Client

app = FastAPI(
    title="MonDevisPro API",
    description="API de génération de devis et factures PDF + Word",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PDF_FOLDER = "generated_pdfs"
os.makedirs(PDF_FOLDER, exist_ok=True)

# Configuration Supabase Storage
# Essayer plusieurs noms de variables possibles (Railway peut utiliser différents préfixes)
SUPABASE_URL = (
    os.getenv("SUPABASE_URL") or 
    os.getenv("RAILWAY_SUPABASE_URL") or
    os.getenv("DATABASE_URL") or  # Parfois Railway utilise DATABASE_URL
    ""
)
SUPABASE_SERVICE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY") or 
    os.getenv("RAILWAY_SUPABASE_SERVICE_KEY") or
    os.getenv("SUPABASE_SERVICE_ROLE_KEY") or
    ""
)

# Debug: Afficher TOUTES les variables d'environnement qui contiennent "SUPABASE"
print("=== DEBUG ENV VARIABLES ===")
all_env = {k: v[:20] + "..." if v and len(v) > 20 else v for k, v in os.environ.items() if "SUPABASE" in k.upper() or "DATABASE" in k.upper()}
for key, value in all_env.items():
    print(f"{key}: {value}")
print("==========================")

print(f"=== SUPABASE CONFIG ===")
print(f"SUPABASE_URL (env): {'OUI' if os.getenv('SUPABASE_URL') else 'NON'}")
print(f"SUPABASE_SERVICE_KEY (env): {'OUI' if os.getenv('SUPABASE_SERVICE_KEY') else 'NON'}")
print(f"URL finale: {SUPABASE_URL[:50] if SUPABASE_URL else 'VIDE'}...")
print(f"KEY finale: {SUPABASE_SERVICE_KEY[:20] if SUPABASE_SERVICE_KEY else 'VIDE'}...")
print(f"Longueur URL: {len(SUPABASE_URL) if SUPABASE_URL else 0}")
print(f"Longueur KEY: {len(SUPABASE_SERVICE_KEY) if SUPABASE_SERVICE_KEY else 0}")
print(f"=======================")

# Initialiser le client Supabase UNE SEULE FOIS
supabase_client: Optional[Client] = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print("✅ Supabase client créé")
        
        # Vérifier que le bucket 'documents' existe
        try:
            buckets = supabase_client.storage.list_buckets()
            bucket_names = [b.name for b in buckets]
            if 'documents' not in bucket_names:
                print("⚠️ ATTENTION: Le bucket 'documents' n'existe pas dans Supabase Storage!")
                print(f"   Buckets disponibles: {bucket_names}")
            else:
                print("✅ Bucket 'documents' trouvé")
        except Exception as e:
            print(f"⚠️ Erreur lors de la vérification des buckets: {e}")
    except Exception as e:
        print(f"❌ Erreur lors de la création du client Supabase: {e}")
        supabase_client = None
else:
    print("❌ Supabase non configuré - variables d'environnement manquantes")

def upload_to_supabase(filepath: str, filename: str) -> str:
    """Upload un fichier sur Supabase Storage et retourne l'URL publique"""
    if not supabase_client:
        print(f"⚠️ Supabase non configuré, fichier local conservé: {filename}")
        return f"/download/{filename}"
    
    try:
        # Vérifier que le fichier existe
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Le fichier {filepath} n'existe pas")
        
        file_size = os.path.getsize(filepath)
        print(f"📁 Taille du fichier {filename}: {file_size} bytes")
        
        # Lire le fichier
        with open(filepath, 'rb') as f:
            file_data = f.read()
        
        if len(file_data) == 0:
            raise ValueError(f"Le fichier {filename} est vide")
        
        print(f"📤 Début upload de {filename} ({len(file_data)} bytes)")
        
        # Déterminer le content-type
        content_type = "application/pdf" if filename.endswith('.pdf') else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        
        # Essayer de supprimer le fichier existant d'abord
        try:
            result = supabase_client.storage.from_('documents').remove([filename])
            print(f"🗑️  Tentative de suppression du fichier existant: {result}")
        except Exception as e:
            print(f"ℹ️  Fichier n'existe pas encore (normal): {e}")
        
        # Upload sur Supabase Storage
        # La bibliothèque supabase-py attend file_data directement, pas file_options avec upsert
        upload_response = supabase_client.storage.from_('documents').upload(
            path=filename,
            file=file_data,
            file_options={"content-type": content_type}
        )
        
        print(f"📥 Réponse upload: {upload_response}")
        print(f"📥 Type de réponse: {type(upload_response)}")
        
        # Vérifier que l'upload a réussi
        # La réponse peut être un dict avec 'error' ou une liste
        if isinstance(upload_response, dict) and upload_response.get('error'):
            error_msg = upload_response.get('error', 'Erreur inconnue')
            raise Exception(f"Erreur upload Supabase: {error_msg}")
        
        print(f"✅ Upload réussi pour {filename}")
        
        # Générer l'URL publique
        # get_public_url retourne directement une chaîne d'URL
        public_url = supabase_client.storage.from_('documents').get_public_url(filename)
        
        print(f"🔗 Type URL publique: {type(public_url)}")
        print(f"🔗 URL publique brute: {public_url}")
        
        # Convertir en string si nécessaire
        if isinstance(public_url, dict):
            public_url = public_url.get('publicUrl', '') or public_url.get('public_url', '')
        elif not isinstance(public_url, str):
            public_url = str(public_url)
        
        if not public_url or public_url == '' or public_url == 'None':
            raise Exception(f"URL publique vide ou invalide: {public_url}")
        
        print(f"✅ URL publique finale: {public_url}")
        
        # Supprimer le fichier local seulement après confirmation de l'upload
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                print(f"🗑️  Fichier local supprimé: {filepath}")
            except Exception as e:
                print(f"⚠️  Impossible de supprimer le fichier local: {e}")
        
        return public_url
        
    except FileNotFoundError as e:
        print(f"❌ Erreur fichier non trouvé: {e}")
        return f"/download/{filename}"
    except Exception as e:
        print(f"❌ Erreur upload Supabase pour {filename}: {e}")
        print(f"   Type d'erreur: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        # Ne pas supprimer le fichier local en cas d'erreur
        return f"/download/{filename}"

# Couleurs par défaut (utilisées si couleur_pdf n'est pas défini)
COULEUR_DEFAUT = '#2F665B'
BLEU_CLAIR = HexColor('#3498db')
GRIS_FONCE = HexColor('#2c3e50')
GRIS_CLAIR = HexColor('#ecf0f1')
GRIS_TEXTE = HexColor('#555555')


# ==================== MODÈLES ====================

class Prestation(BaseModel):
    description: str
    quantite: float
    unite: str
    prix_unitaire: float
    tva_taux: Optional[float] = None  # Taux TVA par ligne (20, 10, 5.5, 0, etc.)

class PrestationFinale(BaseModel):
    """Prestation avec montants figés après remise (source unique de vérité)"""
    description: str
    quantite: float
    unite: str
    ht_apres_remise: float  # HT après remise (FIGÉ, ne jamais recalculer)
    tva_taux: float  # Taux TVA (FIGÉ, ne jamais modifier)

class Entreprise(BaseModel):
    nom: str
    gerant: Optional[str] = ""
    siret: str
    adresse: str
    cp_ville: str
    tel: str
    email: str = ""
    logo_url: Optional[str] = None
    tva_taux: Optional[float] = 20.0
    mention_legale_tva: Optional[str] = ""
    conditions_paiement: Optional[str] = "30% à la commande, solde à réception"
    delai_validite: Optional[int] = 30
    forme_juridique: Optional[str] = "auto-entrepreneur"
    capital_social: Optional[str] = ""
    rcs: Optional[str] = ""
    tva_intracommunautaire: Optional[str] = ""
    couleur_pdf: Optional[str] = None

class Client(BaseModel):
    nom: str
    adresse: Optional[str] = ""
    cp_ville: Optional[str] = ""
    tel: Optional[str] = ""
    email: Optional[str] = ""

class DevisRequest(BaseModel):
    entreprise: Entreprise
    client: Client
    prestations: List[Prestation]
    numero_devis: Optional[str] = None  # Numéro fourni par le frontend
    tva_taux: float = 20.0
    conditions_paiement: str = "30% à la commande, solde à réception"
    delai_realisation: str = "À définir"
    validite_jours: int = 30
    remise_type: Optional[str] = None  # "pourcentage" ou "fixe"
    remise_valeur: Optional[float] = 0

class DevisDataFromAI(BaseModel):
    client_nom: str
    prestations: List[Prestation]
    delai: Optional[str] = "À définir"
    remise_type: Optional[str] = None
    remise_valeur: Optional[float] = 0

class DevisRequestSimple(BaseModel):
    entreprise: Entreprise
    devis_data: DevisDataFromAI
    validite_jours: int = 30

class RIB(BaseModel):
    iban: Optional[str] = ""
    bic: Optional[str] = ""
    titulaire: Optional[str] = ""
    
class FactureRequest(BaseModel):
    entreprise: Entreprise
    client: Client
    prestations: List[Prestation]
    tva_taux: float = 20.0
    numero_devis_origine: Optional[str] = None
    date_echeance_jours: int = 30
    mention_legale_tva: Optional[str] = ""
    rib: Optional[RIB] = None
    remise_type: Optional[str] = None  # "pourcentage" ou "montant"
    remise_valeur: Optional[float] = 0
    statut: Optional[str] = "en_attente"  # "en_attente", "payee", etc.
    acompte_ttc_deja_facture: Optional[float] = 0  # Montant TTC de l'acompte déjà facturé (pour facture finale)
    is_facture_acompte: Optional[bool] = False  # True si c'est une facture d'acompte
    taux_acompte: Optional[float] = None  # Pourcentage d'acompte (ex: 30 pour 30%) - pour facture d'acompte uniquement
    lignes_finales_devis: Optional[List[PrestationFinale]] = None  # Lignes du devis après remise (source unique de vérité) - si présent, utiliser directement sans recalcul


# ==================== FONCTIONS UTILITAIRES ====================

def get_couleur_principale(data) -> HexColor:
    """Récupère la couleur principale depuis couleur_pdf ou utilise la couleur par défaut"""
    couleur_hex = data.entreprise.couleur_pdf if data.entreprise.couleur_pdf else COULEUR_DEFAUT
    # S'assurer que la couleur commence par #
    if not couleur_hex.startswith('#'):
        couleur_hex = '#' + couleur_hex
    try:
        return HexColor(couleur_hex)
    except:
        # En cas d'erreur, utiliser la couleur par défaut
        return HexColor(COULEUR_DEFAUT)

def hex_to_rgb(hex_color: str) -> tuple:
    """Convertit une couleur hex (#RRGGBB) en tuple RGB (r, g, b)"""
    # Enlever le # si présent
    hex_color = hex_color.lstrip('#')
    try:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except:
        # En cas d'erreur, retourner la couleur par défaut
        hex_default = COULEUR_DEFAUT.lstrip('#')
        return tuple(int(hex_default[i:i+2], 16) for i in (0, 2, 4))

def get_couleur_principale_rgb(data) -> RGBColor:
    """Récupère la couleur principale au format RGBColor pour Word"""
    couleur_hex = data.entreprise.couleur_pdf if data.entreprise.couleur_pdf else COULEUR_DEFAUT
    # S'assurer que la couleur commence par #
    if not couleur_hex.startswith('#'):
        couleur_hex = '#' + couleur_hex
    r, g, b = hex_to_rgb(couleur_hex)
    return RGBColor(r, g, b)

def get_couleur_principale_hex_string(data) -> str:
    """Récupère la couleur principale au format hex string (sans #) pour Word set_cell_shading"""
    couleur_hex = data.entreprise.couleur_pdf if data.entreprise.couleur_pdf else COULEUR_DEFAUT
    # Enlever le # si présent
    return couleur_hex.lstrip('#')

def telecharger_logo(logo_url: str) -> Optional[ImageReader]:
    try:
        if not logo_url or logo_url.strip() == "":
            return None
        response = requests.get(logo_url, timeout=10)
        if response.status_code == 200:
            image_data = BytesIO(response.content)
            return ImageReader(image_data)
    except Exception as e:
        print(f"Erreur téléchargement logo: {e}")
    return None

def telecharger_logo_bytes(logo_url: str) -> Optional[BytesIO]:
    """Télécharge le logo et retourne les bytes pour Word"""
    try:
        if not logo_url or logo_url.strip() == "":
            return None
        response = requests.get(logo_url, timeout=10)
        if response.status_code == 200:
            return BytesIO(response.content)
    except Exception as e:
        print(f"Erreur téléchargement logo: {e}")
    return None

def tronquer_texte(texte: str, max_chars: int) -> str:
    if not texte:
        return ""
    if len(texte) <= max_chars:
        return texte
    return texte[:max_chars-3] + "..."

def formater_adresse_complete(adresse: str, cp_ville: str) -> str:
    parties = []
    if adresse and adresse.strip():
        parties.append(adresse.strip())
    if cp_ville and cp_ville.strip():
        parties.append(cp_ville.strip())
    return ", ".join(parties) if parties else ""


# ==================== GÉNÉRATION PDF ====================

def dessiner_bloc_emetteur(c, width, height, data, y_position):
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(15*mm, y_position - 32*mm, 85*mm, 38*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(get_couleur_principale(data))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20*mm, y_position, "ÉMETTEUR")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    y_text = y_position - 5*mm
    
    c.drawString(20*mm, y_text, tronquer_texte(data.entreprise.nom, 40))
    
    adresse = data.entreprise.adresse if data.entreprise.adresse else ""
    cp_ville = data.entreprise.cp_ville if data.entreprise.cp_ville else ""
    
    ligne_y = y_text - 5*mm
    
    if len(adresse) <= 35:
        if adresse:
            c.drawString(20*mm, ligne_y, adresse)
            ligne_y -= 5*mm
    else:
        mots = adresse.split()
        ligne1 = ""
        ligne2 = ""
        for mot in mots:
            if len(ligne1 + " " + mot) <= 35:
                ligne1 = (ligne1 + " " + mot).strip()
            else:
                ligne2 = (ligne2 + " " + mot).strip()
        c.drawString(20*mm, ligne_y, ligne1)
        ligne_y -= 5*mm
        if ligne2:
            c.drawString(20*mm, ligne_y, ligne2)
            ligne_y -= 5*mm
    
    if cp_ville:
        c.drawString(20*mm, ligne_y, cp_ville)
        ligne_y -= 5*mm
    
    c.drawString(20*mm, ligne_y, f"Tél : {data.entreprise.tel}")
    ligne_y -= 5*mm
    c.drawString(20*mm, ligne_y, f"Email : {tronquer_texte(data.entreprise.email, 35)}")
    ligne_y -= 5*mm
    c.drawString(20*mm, ligne_y, f"SIRET : {data.entreprise.siret}")


def dessiner_bloc_client(c, width, height, data, y_position):
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(110*mm, y_position - 32*mm, 85*mm, 38*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(get_couleur_principale(data))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(115*mm, y_position, "DESTINATAIRE")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    y_text = y_position - 5*mm
    
    c.drawString(115*mm, y_text, data.client.nom)
    ligne_y = y_text - 5*mm
    
    if data.client.adresse:
        adresse = data.client.adresse
        if len(adresse) <= 35:
            c.drawString(115*mm, ligne_y, adresse)
            ligne_y -= 5*mm
        else:
            mots = adresse.split()
            ligne1 = ""
            ligne2 = ""
            for mot in mots:
                if len(ligne1 + " " + mot) <= 35:
                    ligne1 = (ligne1 + " " + mot).strip()
                else:
                    ligne2 = (ligne2 + " " + mot).strip()
            c.drawString(115*mm, ligne_y, ligne1)
            ligne_y -= 5*mm
            if ligne2:
                c.drawString(115*mm, ligne_y, ligne2)
                ligne_y -= 5*mm
    
    if data.client.cp_ville:
        c.drawString(115*mm, ligne_y, data.client.cp_ville)
        ligne_y -= 5*mm
    
    if data.client.tel:
        c.drawString(115*mm, ligne_y, f"Tél : {data.client.tel}")
        ligne_y -= 5*mm
    
    if data.client.email:
        c.drawString(115*mm, ligne_y, f"Email : {data.client.email}")


def dessiner_en_tete_page(c, width, height, data, numero_devis, logo, date_validite):
    """Dessine l'en-tête de page (pour la première page et les pages suivantes)"""
    c.setFillColor(get_couleur_principale(data))
    c.rect(0, height - 45*mm, width, 45*mm, fill=True, stroke=False)
    
    text_start_x = 15*mm
    
    if logo:
        try:
            logo_size = 30*mm
            c.drawImage(logo, 15*mm, height - 40*mm, width=logo_size, height=logo_size, preserveAspectRatio=True, mask='auto')
            text_start_x = 50*mm
        except Exception as e:
            print(f"Erreur logo: {e}")
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(text_start_x, height - 18*mm, tronquer_texte(data.entreprise.nom.upper(), 30))
    
    if data.entreprise.gerant and data.entreprise.gerant.strip():
        c.setFont("Helvetica", 9)
        c.drawString(text_start_x, height - 26*mm, f"Gérant : {data.entreprise.gerant}")
    
    c.setFont("Helvetica-Bold", 28)
    c.drawRightString(width - 20*mm, height - 18*mm, "DEVIS")
    c.setFont("Helvetica", 11)
    c.drawRightString(width - 20*mm, height - 28*mm, f"N° {numero_devis}")
    c.setFont("Helvetica", 9)
    c.drawRightString(width - 20*mm, height - 36*mm, f"Date : {datetime.now().strftime('%d/%m/%Y')}")


def dessiner_totaux(c, width, y_totaux, total_ht, total_ht_avant_acompte, total_acompte, remise, tva_taux, total_ht_final, total_ttc, data):
    """Dessine les totaux à droite - tva_taux peut être un dict (tva_par_taux) ou un float (taux unique)"""
    x_label = 130*mm
    x_value = width - 18*mm
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    c.drawString(x_label, y_totaux, "Total HT")
    c.drawRightString(x_value, y_totaux, f"{total_ht:.2f} €")
    
    # Afficher la remise si elle existe
    y_offset = 6*mm
    if remise > 0:
        if hasattr(data, 'remise_type') and data.remise_type == "pourcentage":
            c.drawString(x_label, y_totaux - y_offset, f"Remise ({data.remise_valeur}%)")
        else:
            c.drawString(x_label, y_totaux - y_offset, "Remise")
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{remise:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
    
    # Afficher "Total HT après remise" si remise ou acompte
    if remise > 0 or total_acompte > 0:
        c.drawString(x_label, y_totaux - y_offset, "Total HT après remise")
        total_ht_apres_remise = total_ht_avant_acompte - remise
        c.drawRightString(x_value, y_totaux - y_offset, f"{total_ht_apres_remise:.2f} €")
        y_offset += 6*mm
    
    # Afficher l'acompte si présent
    if total_acompte > 0:
        c.drawString(x_label, y_totaux - y_offset, "Acompte déduit")
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{total_acompte:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
    
    # Calculer tva_par_taux depuis les prestations si non fourni
    # (pour compatibilité avec l'ancien code)
    if isinstance(tva_taux, dict):
        tva_par_taux = tva_taux
    else:
        # Fallback: calculer avec un seul taux (ancien comportement)
        montant_tva = total_ht_final * (tva_taux / 100)
        tva_par_taux = {tva_taux: montant_tva} if tva_taux > 0 else {}
    
    # Afficher chaque taux de TVA séparément
    for taux in sorted(tva_par_taux.keys()):
        montant = tva_par_taux[taux]
        if taux > 0:
            c.drawString(x_label, y_totaux - y_offset, f"TVA ({taux}%)")
            c.drawRightString(x_value, y_totaux - y_offset, f"{montant:.2f} €")
            y_offset += 6*mm
        elif len(tva_par_taux) == 1:  # Seulement si c'est le seul taux et qu'il est à 0
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
            c.setFont("Helvetica", 10)
            y_offset += 6*mm
    
    c.setFillColor(get_couleur_principale(data))
    c.roundRect(x_label - 5*mm, y_totaux - y_offset - 8*mm, 68*mm, 10*mm, 2*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x_label, y_totaux - y_offset - 5*mm, "TOTAL TTC")
    c.drawRightString(x_value, y_totaux - y_offset - 5*mm, f"{total_ttc:.2f} €")
    
    return y_totaux - y_offset - 8*mm  # Retourner la position Y finale


def dessiner_lignes_prestations(c, width, prestations, y_table, data, index_debut=0):
    """Dessine les lignes de prestations (en-tête + lignes) et retourne la position Y finale et les totaux calculés"""
    # En-tête du tableau
    c.setFillColor(get_couleur_principale(data))
    c.rect(15*mm, y_table, width - 30*mm, 10*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18*mm, y_table + 3*mm, "Description")
    c.drawString(90*mm, y_table + 3*mm, "Qté")
    c.drawString(105*mm, y_table + 3*mm, "Unité")
    c.drawString(125*mm, y_table + 3*mm, "P.U. HT")
    c.drawString(150*mm, y_table + 3*mm, "TVA")
    c.drawRightString(width - 18*mm, y_table + 3*mm, "Total HT")
    
    y_ligne = y_table - 2*mm
    total_ht_avant_acompte = 0
    total_acompte = 0
    
    # Taux TVA global par défaut
    tva_taux_global = getattr(data, 'tva_taux', 20.0)
    
    # Dessiner les lignes
    for i, prestation in enumerate(prestations):
        y_ligne -= 10*mm
        total_ligne = prestation.quantite * prestation.prix_unitaire
        
        # RÈGLE FISCALE : Toujours utiliser le taux du devis (source unique de vérité)
        if prestation.tva_taux is not None:
            tva_taux_ligne = prestation.tva_taux
        else:
            if tva_taux_global is None:
                raise ValueError(f"Taux TVA manquant pour la prestation '{prestation.description}'. Le taux doit être défini soit sur la prestation, soit globalement dans le devis.")
            tva_taux_ligne = tva_taux_global
        
        # Séparer les prestations positives et les acomptes (négatifs)
        if total_ligne >= 0:
            total_ht_avant_acompte += total_ligne
        else:
            total_acompte += abs(total_ligne)
        
        # Alterner les couleurs de fond
        if (index_debut + i) % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(prestation.description, 50))
        c.drawString(90*mm, y_ligne + 2*mm, str(prestation.quantite))
        c.drawString(105*mm, y_ligne + 2*mm, prestation.unite)
        c.drawString(125*mm, y_ligne + 2*mm, f"{prestation.prix_unitaire:.2f} €")
        c.drawString(150*mm, y_ligne + 2*mm, f"{tva_taux_ligne:.1f}%")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne:.2f} €")
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    return y_ligne - 10*mm, total_ht_avant_acompte, total_acompte


def dessiner_tableau_prestations(c, width, data, y_table, tva_taux_global):
    """Dessine le tableau des prestations pour une facture avec totaux - TVA par ligne"""
    # En-tête du tableau
    c.setFillColor(get_couleur_principale(data))
    c.rect(15*mm, y_table, width - 30*mm, 10*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18*mm, y_table + 3*mm, "Description")
    c.drawString(90*mm, y_table + 3*mm, "Qté")
    c.drawString(105*mm, y_table + 3*mm, "Unité")
    c.drawString(125*mm, y_table + 3*mm, "P.U. HT")
    c.drawString(150*mm, y_table + 3*mm, "TVA")
    c.drawRightString(width - 18*mm, y_table + 3*mm, "Total HT")
    
    y_ligne = y_table - 2*mm
    total_ht_avant_remise = 0  # HT avant remise (pour affichage)
    total_ht_apres_remise = 0  # HT après remise ligne par ligne
    tva_par_taux = {}  # Dictionnaire pour grouper la TVA par taux
    
    # Récupérer l'acompte TTC déjà facturé (pour facture finale uniquement)
    acompte_ttc_deja_facture = getattr(data, 'acompte_ttc_deja_facture', 0) or 0
    is_facture_acompte = getattr(data, 'is_facture_acompte', False)
    taux_acompte = getattr(data, 'taux_acompte', None)  # Pourcentage d'acompte (ex: 30 pour 30%)
    lignes_finales_devis = getattr(data, 'lignes_finales_devis', None)  # Lignes du devis après remise (source unique de vérité)
    
    # RÈGLE FISCALE : Si lignes_finales_devis est présent, utiliser directement sans recalcul
    # Ces lignes sont la source unique de vérité (déjà calculées avec remise sur le devis)
    if lignes_finales_devis and len(lignes_finales_devis) > 0:
        # ÉTAPE 1 : Normalisation et fusion des lignes (AVANT tout calcul)
        # Normaliser les descriptions pour éviter les doublons (mur/MUR)
        descriptions_vues = {}
        lignes_finales_normalisees = []
        
        for ligne in lignes_finales_devis:
            desc_normalisee = ligne.description.strip().lower()
            
            # Si description déjà vue, fusionner avec la ligne existante
            if desc_normalisee in descriptions_vues:
                index_existant = descriptions_vues[desc_normalisee]
                ligne_existante = lignes_finales_normalisees[index_existant]
                
                # Vérifier que le taux TVA est identique (sinon erreur)
                if ligne_existante['tva_taux'] != ligne.tva_taux:
                    raise ValueError(f"Conflit de taux TVA pour '{ligne.description}': {ligne_existante['tva_taux']}% vs {ligne.tva_taux}%. Les lignes avec la même description doivent avoir le même taux TVA.")
                
                # Fusionner : additionner les HT et quantités
                ligne_existante['ht_apres_remise'] += ligne.ht_apres_remise
                ligne_existante['quantite'] += ligne.quantite
            else:
                # Nouvelle ligne
                descriptions_vues[desc_normalisee] = len(lignes_finales_normalisees)
                lignes_finales_normalisees.append({
                    'description': ligne.description,  # Garder la description originale pour affichage
                    'description_norm': desc_normalisee,
                    'quantite': ligne.quantite,
                    'unite': ligne.unite,
                    'ht_apres_remise': ligne.ht_apres_remise,
                    'tva_taux': ligne.tva_taux
                })
        
        # ÉTAPE 2 : Calculer les totaux UNIQUEMENT à partir des lignes finales (source de vérité)
        # MODE : Utiliser les lignes finales du devis (AUCUN RECALCUL)
        total_ht_avant_remise = 0  # Non utilisé si lignes déjà remisées
        total_ht_apres_remise = 0
        tva_par_taux = {}
        remise_totale = 0  # Ne sera pas affichée si lignes déjà remisées
        
        for i, ligne_norm in enumerate(lignes_finales_normalisees):
            y_ligne -= 10*mm
            
            # Utiliser directement les valeurs figées du devis (après normalisation)
            # ht_ligne_final est définitif, tva_rate est figé
            ht_ligne_final = ligne_norm['ht_apres_remise']
            tva_taux_ligne = ligne_norm['tva_taux']
            
            # Si facture d'acompte : calculer l'acompte sur le HT après remise
            if is_facture_acompte and taux_acompte is not None and taux_acompte > 0:
                total_ligne_ht_final = ht_ligne_final * (taux_acompte / 100)
            else:
                # Facture complète : utiliser tout le HT après remise
                total_ligne_ht_final = ht_ligne_final
            
            # RÈGLE : total_ht = sum(ligne.ht_ligne_final for ligne in lignes)
            total_ht_apres_remise += total_ligne_ht_final
            
            # RÈGLE : total_tva = sum(ligne.ht_ligne_final * ligne.tva_rate for ligne in lignes)
            montant_tva_ligne = total_ligne_ht_final * (tva_taux_ligne / 100)
            tva_par_taux[tva_taux_ligne] = tva_par_taux.get(tva_taux_ligne, 0) + montant_tva_ligne
            
            # Alterner les couleurs de fond
            if i % 2 == 0:
                c.setFillColor(HexColor('#f8f9fa'))
                c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
            
            c.setFillColor(GRIS_FONCE)
            c.setFont("Helvetica", 9)
            c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(ligne_norm['description'], 50))
            c.drawString(90*mm, y_ligne + 2*mm, str(ligne_norm['quantite']))
            c.drawString(105*mm, y_ligne + 2*mm, ligne_norm['unite'])
            
            # Afficher le prix unitaire
            if is_facture_acompte and taux_acompte is not None:
                prix_affiche = total_ligne_ht_final / ligne_norm['quantite'] if ligne_norm['quantite'] > 0 else 0
            else:
                prix_affiche = ht_ligne_final / ligne_norm['quantite'] if ligne_norm['quantite'] > 0 else 0
            
            c.drawString(125*mm, y_ligne + 2*mm, f"{prix_affiche:.2f} €")
            c.drawString(150*mm, y_ligne + 2*mm, f"{tva_taux_ligne:.1f}%")
            c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne_ht_final:.2f} €")
        
        # Les lignes sont déjà remisées, donc remise_totale reste à 0
        # (ne sera pas affichée dans les totaux)
    else:
        # MODE : Calculer normalement (pour compatibilité avec ancien code)
        # Calculer la remise (pourcentage ou montant)
        remise_type = None
        remise_valeur = 0
        if hasattr(data, 'remise_type') and data.remise_type and hasattr(data, 'remise_valeur') and data.remise_valeur and data.remise_valeur > 0:
            remise_type = data.remise_type
            remise_valeur = data.remise_valeur
        
        # Calculer le montant total de remise (pour affichage)
        remise_totale = 0
        if remise_type == "montant":
            remise_totale = remise_valeur
        
        # RÈGLE FISCALE : Pour facture d'acompte, l'acompte doit être calculé APRÈS remise
        # Étape 1 : Calculer HT après remise pour toutes les lignes
        # Étape 2 : Si facture d'acompte, calculer l'acompte sur le HT après remise
        # Étape 3 : Calculer la TVA sur l'acompte (ou sur le HT final pour facture complète)
        
        # Pour chaque ligne : appliquer remise, puis calculer acompte si nécessaire, puis TVA
        # IGNORER les lignes négatives (ne plus les traiter comme des acomptes)
        for i, prestation in enumerate(data.prestations):
            y_ligne -= 10*mm
            # 1. Calculer HT ligne (avant remise) - prix unitaire ORIGINAL du devis
            total_ligne_ht_original = prestation.quantite * prestation.prix_unitaire
            
            # Ignorer les lignes négatives (ancien système d'acompte)
            if total_ligne_ht_original <= 0:
                continue
            
            total_ht_avant_remise += total_ligne_ht_original
            
            # 2. Appliquer la remise sur cette ligne (TOUJOURS en premier)
            if remise_type == "pourcentage":
                remise_ligne = total_ligne_ht_original * (remise_valeur / 100)
            elif remise_type == "montant":
                # Répartir la remise proportionnellement si c'est un montant fixe
                # On calculera la remise totale d'abord, puis on répartira
                remise_ligne = 0  # Sera calculé après
            else:
                remise_ligne = 0
            
            # 3. HT ligne après remise (BASE DE RÉFÉRENCE)
            total_ligne_ht_apres_remise = total_ligne_ht_original - remise_ligne
            
            # 4. Si facture d'acompte : calculer l'acompte sur le HT après remise
            if is_facture_acompte and taux_acompte is not None and taux_acompte > 0:
                # Calculer l'acompte sur le HT après remise
                total_ligne_ht_final = total_ligne_ht_apres_remise * (taux_acompte / 100)
            else:
                # Facture complète : utiliser tout le HT après remise
                total_ligne_ht_final = total_ligne_ht_apres_remise
            
            total_ht_apres_remise += total_ligne_ht_final
            
            # 5. Calculer la TVA sur le HT final (acompte ou complet)
            # RÈGLE FISCALE : Toujours utiliser le taux du devis (source unique de vérité)
            if prestation.tva_taux is not None:
                tva_taux_ligne = prestation.tva_taux
            else:
                # Utiliser le taux global du devis (doit être défini)
                if tva_taux_global is None:
                    raise ValueError(f"Taux TVA manquant pour la prestation '{prestation.description}'. Le taux doit être défini soit sur la prestation, soit globalement dans le devis.")
                tva_taux_ligne = tva_taux_global
            
            montant_tva_ligne = total_ligne_ht_final * (tva_taux_ligne / 100)
            tva_par_taux[tva_taux_ligne] = tva_par_taux.get(tva_taux_ligne, 0) + montant_tva_ligne
            
            # Alterner les couleurs de fond
            if i % 2 == 0:
                c.setFillColor(HexColor('#f8f9fa'))
                c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
            
            c.setFillColor(GRIS_FONCE)
            c.setFont("Helvetica", 9)
            c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(prestation.description, 50))
            c.drawString(90*mm, y_ligne + 2*mm, str(prestation.quantite))
            c.drawString(105*mm, y_ligne + 2*mm, prestation.unite)
            # Afficher le prix unitaire (original pour facture complète, ou acompte pour facture d'acompte)
            if is_facture_acompte and taux_acompte is not None:
                # Afficher le montant d'acompte HT (après remise)
                prix_affiche = total_ligne_ht_final / prestation.quantite if prestation.quantite > 0 else 0
            else:
                prix_affiche = prestation.prix_unitaire
            
            c.drawString(125*mm, y_ligne + 2*mm, f"{prix_affiche:.2f} €")
            # tva_taux_ligne déjà calculé plus haut
            c.drawString(150*mm, y_ligne + 2*mm, f"{tva_taux_ligne:.1f}%")
            # Afficher le total HT de la ligne (acompte ou complet)
            c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne_ht_final:.2f} €")
    
    # Si remise de type "montant", répartir proportionnellement (uniquement si on n'utilise pas lignes_finales_devis)
    if not (lignes_finales_devis and len(lignes_finales_devis) > 0) and remise_type == "montant" and total_ht_avant_remise > 0:
        remise_totale = remise_valeur
        ratio_remise = remise_valeur / total_ht_avant_remise
        # Recalculer avec la répartition proportionnelle
        total_ht_apres_remise = 0
        tva_par_taux = {}
        for prestation in data.prestations:
            total_ligne_ht_original = prestation.quantite * prestation.prix_unitaire
            if total_ligne_ht_original > 0:  # Ignorer les lignes négatives
                remise_ligne = total_ligne_ht_original * ratio_remise
                total_ligne_ht_apres_remise = total_ligne_ht_original - remise_ligne
                
                # Si facture d'acompte : calculer l'acompte sur le HT après remise
                if is_facture_acompte and taux_acompte is not None and taux_acompte > 0:
                    total_ligne_ht_final = total_ligne_ht_apres_remise * (taux_acompte / 100)
                else:
                    total_ligne_ht_final = total_ligne_ht_apres_remise
                
                total_ht_apres_remise += total_ligne_ht_final
                # RÈGLE FISCALE : Toujours utiliser le taux du devis
                if prestation.tva_taux is not None:
                    tva_taux_ligne = prestation.tva_taux
                else:
                    if tva_taux_global is None:
                        raise ValueError(f"Taux TVA manquant pour la prestation '{prestation.description}'.")
                    tva_taux_ligne = tva_taux_global
                montant_tva_ligne = total_ligne_ht_final * (tva_taux_ligne / 100)
                tva_par_taux[tva_taux_ligne] = tva_par_taux.get(tva_taux_ligne, 0) + montant_tva_ligne
    elif remise_type == "pourcentage":
        remise_totale = total_ht_avant_remise * (remise_valeur / 100)
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    y_totaux = y_ligne - 10*mm
    
    # RÈGLE ABSOLUE : Calculer les totaux UNIQUEMENT à partir des lignes finales
    # total_ht = sum(ligne.ht_ligne_final for ligne in lignes)
    # total_tva = sum(ligne.ht_ligne_final * ligne.tva_rate for ligne in lignes)
    # total_ttc = total_ht + total_tva
    # ❌ Aucune remise, aucun recalcul indirect, aucun ajustement global
    
    total_ht_final = total_ht_apres_remise  # Somme des lignes finales
    total_tva = sum(tva_par_taux.values())  # Somme des TVA par ligne
    total_ttc_devis = total_ht_final + total_tva  # Total TTC
    
    # Pour facture finale : déduire l'acompte TTC déjà facturé
    # Pour facture d'acompte : pas de déduction
    if not is_facture_acompte and acompte_ttc_deja_facture > 0:
        net_a_payer_ttc = total_ttc_devis - acompte_ttc_deja_facture
    else:
        net_a_payer_ttc = total_ttc_devis
    
    # RÈGLE : Si lignes_finales_devis est présent, les lignes sont DÉJÀ remisées
    # → NE PAS afficher de remise globale (sinon remise appliquée deux fois)
    lignes_deja_remisees = (lignes_finales_devis and len(lignes_finales_devis) > 0)
    
    x_label = 130*mm
    x_value = width - 18*mm
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    
    y_offset = 0
    
    # Si lignes déjà remisées : afficher directement "Total HT" (somme des lignes)
    # Sinon : afficher "Total HT", puis remise, puis "Total HT après remise"
    if lignes_deja_remisees:
        # Lignes déjà remisées : Total HT = somme des lignes (pas de remise à afficher)
        c.drawString(x_label, y_totaux, "Total HT")
        c.drawRightString(x_value, y_totaux, f"{total_ht_final:.2f} €")
        y_offset = 6*mm
    else:
        # Lignes non remisées : afficher Total HT avant remise, puis remise, puis après remise
        c.drawString(x_label, y_totaux, "Total HT")
        c.drawRightString(x_value, y_totaux, f"{total_ht_avant_remise:.2f} €")
        y_offset = 6*mm
        
        # Afficher la remise si elle existe
        if remise_totale > 0:
            if hasattr(data, 'remise_type') and data.remise_type == "pourcentage":
                c.drawString(x_label, y_totaux - y_offset, f"Remise ({data.remise_valeur}%)")
            else:
                c.drawString(x_label, y_totaux - y_offset, "Remise")
            c.setFillColor(HexColor('#e74c3c'))
            c.drawRightString(x_value, y_totaux - y_offset, f"-{remise_totale:.2f} €")
            c.setFillColor(GRIS_FONCE)
            y_offset += 6*mm
        
        # Afficher "Total HT après remise" si remise
        if remise_totale > 0:
            c.drawString(x_label, y_totaux - y_offset, "Total HT après remise")
            c.drawRightString(x_value, y_totaux - y_offset, f"{total_ht_apres_remise:.2f} €")
            y_offset += 6*mm
    
    # Afficher chaque taux de TVA séparément
    for taux in sorted(tva_par_taux.keys()):
        montant = tva_par_taux[taux]
        if taux > 0:
            c.drawString(x_label, y_totaux - y_offset, f"TVA ({taux}%)")
            c.drawRightString(x_value, y_totaux - y_offset, f"{montant:.2f} €")
            y_offset += 6*mm
        elif len(tva_par_taux) == 1:  # Seulement si c'est le seul taux et qu'il est à 0
            c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
            y_offset += 6*mm
    
    # Total TTC du devis
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x_label, y_totaux - y_offset, "TOTAL TTC")
    c.drawRightString(x_value, y_totaux - y_offset, f"{total_ttc_devis:.2f} €")
    y_offset += 6*mm
    
    # Pour facture finale : afficher acompte TTC déjà payé et net à payer
    if not is_facture_acompte and acompte_ttc_deja_facture > 0:
        c.setFont("Helvetica", 10)
        c.setFillColor(GRIS_FONCE)
        c.drawString(x_label, y_totaux - y_offset, "Acompte TTC déjà payé")
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{acompte_ttc_deja_facture:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
        
        # Net à payer TTC
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(get_couleur_principale(data))
        c.roundRect(x_label - 5*mm, y_totaux - y_offset - 8*mm, 68*mm, 10*mm, 2*mm, fill=True, stroke=False)
        c.setFillColor(white)
        c.drawString(x_label, y_totaux - y_offset - 5*mm, "NET À PAYER TTC")
        c.drawRightString(x_value, y_totaux - y_offset - 5*mm, f"{net_a_payer_ttc:.2f} €")
        y_offset += 6*mm
    
    return y_totaux - y_offset - 5*mm, total_ht_final, net_a_payer_ttc if not is_facture_acompte and acompte_ttc_deja_facture > 0 else total_ttc_devis


def dessiner_pied_page(c, width, data, mention_tva=""):
    c.setStrokeColor(get_couleur_principale(data))
    c.setLineWidth(2)
    c.line(15*mm, 35*mm, width - 15*mm, 35*mm)
    
    c.setFillColor(GRIS_TEXTE)
    c.setFont("Helvetica", 7)
    
    # Récupérer les infos de forme juridique
    forme = getattr(data.entreprise, 'forme_juridique', 'auto-entrepreneur') or 'auto-entrepreneur'
    capital = getattr(data.entreprise, 'capital_social', '') or ''
    rcs = getattr(data.entreprise, 'rcs', '') or ''
    tva_intra = getattr(data.entreprise, 'tva_intracommunautaire', '') or ''
    
    # Ligne 1 : Nom + forme juridique + capital (si applicable)
    if forme in ['sarl', 'eurl', 'sas', 'sasu', 'SARL', 'EURL', 'SAS', 'SASU']:
        ligne1 = f"{data.entreprise.nom} - {forme.upper()}"
        if capital:
            ligne1 += f" au capital de {capital} €"
    elif forme in ['ei', 'EI']:
        ligne1 = f"{data.entreprise.nom} - Entreprise Individuelle"
    elif forme in ['auto-entrepreneur', 'micro-entreprise', 'Auto-entrepreneur', 'Micro-entreprise']:
        ligne1 = f"{data.entreprise.nom} - Auto-entrepreneur"
    else:
        ligne1 = f"{data.entreprise.nom}"
    
    c.drawCentredString(width/2, 28*mm, ligne1)
    
    # Ligne 2 : SIRET + RCS (si applicable)
    ligne2 = f"SIRET : {data.entreprise.siret}"
    if rcs and forme in ['sarl', 'eurl', 'sas', 'sasu', 'SARL', 'EURL', 'SAS', 'SASU']:
        ligne2 += f" - {rcs}"
    elif forme in ['auto-entrepreneur', 'micro-entreprise', 'Auto-entrepreneur', 'Micro-entreprise']:
        ligne2 += " - Dispensé d'immatriculation au RCS"
    
    c.drawCentredString(width/2, 23*mm, ligne2)
    
    # Ligne 3 : Adresse + Tél
    adresse_pied = formater_adresse_complete(data.entreprise.adresse, data.entreprise.cp_ville)
    c.drawCentredString(width/2, 18*mm, f"{adresse_pied} - Tél : {data.entreprise.tel}")
    
    # Ligne 4 : TVA
    if mention_tva:
        c.setFont("Helvetica-Oblique", 7)
        c.drawCentredString(width/2, 13*mm, mention_tva)
    elif tva_intra:
        c.drawCentredString(width/2, 13*mm, f"N° TVA intracommunautaire : {tva_intra}")
    else:
        siret_clean = data.entreprise.siret.replace(' ', '').replace('.', '')
        c.drawCentredString(width/2, 13*mm, f"TVA intracommunautaire : FR{siret_clean[:9] if len(siret_clean) >= 9 else siret_clean}")
    
    c.setFillColor(get_couleur_principale(data))
    c.setFont("Helvetica-Oblique", 6)
    c.drawRightString(width - 15*mm, 8*mm, "Généré par Vocario.fr")


def generer_pdf_devis(data: DevisRequest) -> str:
    # Utiliser le numéro fourni par le frontend, sinon en générer un
    if hasattr(data, 'numero_devis') and data.numero_devis and str(data.numero_devis).strip():
        numero_devis = data.numero_devis
    else:
        numero_devis = f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    filename = f"{numero_devis}.pdf"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    date_validite = (datetime.now() + timedelta(days=data.validite_jours)).strftime("%d/%m/%Y")
    
    logo = telecharger_logo(data.entreprise.logo_url)
    
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    
    # Calculer les totaux ligne par ligne : remise appliquée sur chaque ligne, puis TVA calculée
    total_ht_avant_acompte = 0  # HT avant remise (pour affichage)
    total_ht_apres_remise = 0  # HT après remise ligne par ligne
    total_acompte = 0
    tva_par_taux = {}  # Dictionnaire pour grouper la TVA par taux
    
    # Calculer la remise (pourcentage ou montant)
    remise_type = None
    remise_valeur = 0
    if hasattr(data, 'remise_type') and data.remise_type and hasattr(data, 'remise_valeur') and data.remise_valeur and data.remise_valeur > 0:
        remise_type = data.remise_type
        remise_valeur = data.remise_valeur
    
    # Calculer le montant total de remise (pour affichage)
    remise_totale = 0
    if remise_type == "pourcentage":
        # On calculera la remise totale après avoir sommé les HT
        pass
    elif remise_type == "montant":
        remise_totale = remise_valeur
    
    # Pour chaque ligne : appliquer remise, puis calculer TVA
    for prestation in data.prestations:
        # 1. Calculer HT ligne (avant remise)
        total_ligne_ht = prestation.quantite * prestation.prix_unitaire
        
        if total_ligne_ht >= 0:
            total_ht_avant_acompte += total_ligne_ht
            
            # 2. Appliquer la remise sur cette ligne
            if remise_type == "pourcentage":
                remise_ligne = total_ligne_ht * (remise_valeur / 100)
            elif remise_type == "montant":
                # Répartir la remise proportionnellement si c'est un montant fixe
                # On calculera la remise totale d'abord, puis on répartira
                remise_ligne = 0  # Sera calculé après
            else:
                remise_ligne = 0
            
            # 3. HT ligne après remise
            total_ligne_ht_remise = total_ligne_ht - remise_ligne
            total_ht_apres_remise += total_ligne_ht_remise
            
            # 4. Calculer la TVA sur le HT remisé
            # RÈGLE FISCALE : Toujours utiliser le taux du devis (source unique de vérité)
            if prestation.tva_taux is not None:
                tva_taux_ligne = prestation.tva_taux
            else:
                if data.tva_taux is None:
                    raise ValueError(f"Taux TVA manquant pour la prestation '{prestation.description}'. Le taux doit être défini soit sur la prestation (tva_taux), soit globalement dans le devis (data.tva_taux).")
                tva_taux_ligne = data.tva_taux
            montant_tva_ligne = total_ligne_ht_remise * (tva_taux_ligne / 100)
            tva_par_taux[tva_taux_ligne] = tva_par_taux.get(tva_taux_ligne, 0) + montant_tva_ligne
        else:
            total_acompte += abs(total_ligne_ht)
    
    # Si remise de type "montant", répartir proportionnellement
    if remise_type == "montant" and total_ht_avant_acompte > 0:
        remise_totale = remise_valeur
        ratio_remise = remise_valeur / total_ht_avant_acompte
        # Recalculer avec la répartition proportionnelle
        total_ht_apres_remise = 0
        tva_par_taux = {}
        for prestation in data.prestations:
            total_ligne_ht = prestation.quantite * prestation.prix_unitaire
            if total_ligne_ht >= 0:
                remise_ligne = total_ligne_ht * ratio_remise
                total_ligne_ht_remise = total_ligne_ht - remise_ligne
                total_ht_apres_remise += total_ligne_ht_remise
                # RÈGLE FISCALE : Toujours utiliser le taux du devis
                if prestation.tva_taux is not None:
                    tva_taux_ligne = prestation.tva_taux
                else:
                    if data.tva_taux is None:
                        raise ValueError(f"Taux TVA manquant pour la prestation '{prestation.description}'.")
                    tva_taux_ligne = data.tva_taux
                montant_tva_ligne = total_ligne_ht_remise * (tva_taux_ligne / 100)
                tva_par_taux[tva_taux_ligne] = tva_par_taux.get(tva_taux_ligne, 0) + montant_tva_ligne
    elif remise_type == "pourcentage":
        remise_totale = total_ht_avant_acompte * (remise_valeur / 100)
    
    # Calculer les totaux finaux
    total_ht_final = total_ht_apres_remise - total_acompte
    total_tva = sum(tva_par_taux.values())
    total_ttc = total_ht_final + total_tva
    total_ht = total_ht_avant_acompte  # Pour l'affichage
    
    # Pagination : diviser les prestations en groupes
    lignes_par_page = 11  # Nombre de lignes par page
    prestations_groupes = []
    for i in range(0, len(data.prestations), lignes_par_page):
        prestations_groupes.append(data.prestations[i:i + lignes_par_page])
    
    # Si aucune prestation, créer au moins une page vide
    if not prestations_groupes:
        prestations_groupes = [[]]
    
    mention_tva = ""
    if data.tva_taux == 0:
        mention_tva = "TVA non applicable, article 293 B du Code général des impôts"
    
    # Dessiner chaque groupe de prestations
    for page_num, groupe_prestations in enumerate(prestations_groupes):
        est_premiere_page = (page_num == 0)
        est_derniere_page = (page_num == len(prestations_groupes) - 1)
        
        # Dessiner l'en-tête de page
        dessiner_en_tete_page(c, width, height, data, numero_devis, logo, date_validite)
        
        if est_premiere_page:
            # Dessiner les blocs emetteur/client sur la première page uniquement
            y_position = height - 60*mm
            dessiner_bloc_emetteur(c, width, height, data, y_position)
            dessiner_bloc_client(c, width, height, data, y_position)
            
            c.setFillColor(GRIS_TEXTE)
            c.setFont("Helvetica", 9)
            c.drawRightString(width - 20*mm, y_position - 28*mm, f"Validité : {date_validite}")
            
            y_table = y_position - 50*mm
        else:
            # Sur les pages suivantes, le tableau commence plus haut
            y_table = height - 55*mm
        
        # Dessiner les lignes de prestations
        index_debut = page_num * lignes_par_page
        y_totaux_tableau, _, _ = dessiner_lignes_prestations(c, width, groupe_prestations, y_table, data, index_debut)
        
        # Si dernière page, dessiner les totaux, signature et conditions
        if est_derniere_page:
            y_totaux = y_totaux_tableau
            
            # Dessiner les totaux
            y_fin_totaux = dessiner_totaux(c, width, y_totaux, total_ht, total_ht_avant_acompte, total_acompte, remise_totale, tva_par_taux, total_ht_final, total_ttc, data)
            
            # Bloc signature À GAUCHE (au niveau des totaux)
            y_signature = y_totaux - 5*mm
            c.setStrokeColor(GRIS_CLAIR)
            c.setLineWidth(1)
            c.roundRect(15*mm, y_signature - 35*mm, 80*mm, 40*mm, 3*mm, fill=False, stroke=True)
            
            c.setFillColor(GRIS_TEXTE)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(20*mm, y_signature - 3*mm, "Bon pour accord")
            c.setFont("Helvetica", 8)
            c.drawString(20*mm, y_signature - 13*mm, "Date :")
            c.drawString(20*mm, y_signature - 23*mm, "Signature :")
            c.setFont("Helvetica-Oblique", 7)
            c.drawString(20*mm, y_signature - 31*mm, "(Précédée de \"Bon pour accord\")")
            
            # Vérifier s'il y a assez d'espace pour les conditions APRÈS les totaux/signature
            hauteur_conditions = 35*mm
            espace_necessaire_conditions = hauteur_conditions + 40*mm  # 40mm marge pour le footer
            # Position des conditions après la signature (prendre le plus bas entre signature et totaux)
            y_bas_signature = y_signature - 35*mm
            y_conditions_possible = min(y_fin_totaux, y_bas_signature) - 45*mm
            
            # Si pas assez d'espace pour les conditions sur cette page, créer une nouvelle page
            if y_conditions_possible < espace_necessaire_conditions:
                # Dessiner le footer sur la page actuelle (avec totaux et signature)
                dessiner_pied_page(c, width, data, mention_tva)
                # Créer une nouvelle page pour les conditions
                c.showPage()
                dessiner_en_tete_page(c, width, height, data, numero_devis, logo, date_validite)
                y_conditions = height - 55*mm
            else:
                # Dessiner les conditions sur la même page, APRÈS les totaux/signature
                y_conditions = y_conditions_possible
            
            # Dessiner les conditions
            c.setFillColor(GRIS_CLAIR)
            c.roundRect(15*mm, y_conditions - 25*mm, width - 30*mm, 35*mm, 3*mm, fill=True, stroke=False)
            
            c.setFillColor(get_couleur_principale(data))
            c.setFont("Helvetica-Bold", 10)
            c.drawString(20*mm, y_conditions + 2*mm, "CONDITIONS")
            
            c.setFillColor(GRIS_FONCE)
            c.setFont("Helvetica", 9)
            c.drawString(20*mm, y_conditions - 8*mm, f"• Délai de réalisation : {data.delai_realisation}")
            c.drawString(20*mm, y_conditions - 14*mm, f"• Conditions de paiement : {data.entreprise.conditions_paiement or data.conditions_paiement}")
            c.drawString(20*mm, y_conditions - 20*mm, f"• Devis valable jusqu'au : {date_validite}")
            
            # Dessiner le footer sur cette page (avec totaux, signature et conditions)
            dessiner_pied_page(c, width, data, mention_tva)
        
        # Dessiner le footer sur chaque page (sauf la dernière page qui l'a déjà dessiné)
        if not est_derniere_page:
            dessiner_pied_page(c, width, data, mention_tva)
        
        # Si ce n'est pas la dernière page, créer une nouvelle page
        if not est_derniere_page:
            c.showPage()
    
    try:
        c.save()
        print(f"✅ PDF devis sauvegardé: {filepath}")
    except Exception as e:
        print(f"❌ Erreur lors de la sauvegarde du PDF: {e}")
        raise
    
    return filepath, numero_devis, total_ht_final, total_ttc


def generer_pdf_facture(data: FactureRequest) -> str:
    numero_facture = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    filename = f"{numero_facture}.pdf"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    date_echeance = (datetime.now() + timedelta(days=data.date_echeance_jours)).strftime("%d/%m/%Y")
    
    logo = telecharger_logo(data.entreprise.logo_url)
    
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    
    c.setFillColor(get_couleur_principale(data))
    c.rect(0, height - 45*mm, width, 45*mm, fill=True, stroke=False)
    
    text_start_x = 15*mm
    
    if logo:
        try:
            logo_size = 30*mm
            c.drawImage(logo, 15*mm, height - 40*mm, width=logo_size, height=logo_size, preserveAspectRatio=True, mask='auto')
            text_start_x = 50*mm
        except Exception as e:
            print(f"Erreur logo: {e}")
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(text_start_x, height - 18*mm, tronquer_texte(data.entreprise.nom.upper(), 30))
    
    if data.entreprise.gerant and data.entreprise.gerant.strip():
        c.setFont("Helvetica", 9)
        c.drawString(text_start_x, height - 26*mm, f"Gérant : {data.entreprise.gerant}")
    
    c.setFont("Helvetica-Bold", 28)
    c.drawRightString(width - 20*mm, height - 18*mm, "FACTURE")
    c.setFont("Helvetica", 11)
    c.drawRightString(width - 20*mm, height - 28*mm, f"N° {numero_facture}")
    
    # Vérifier si la facture est payée
    est_payee = hasattr(data, 'statut') and data.statut == 'payee'
    
    if est_payee:
        # Afficher "PAYÉE" en vert à côté du numéro
        c.setFillColor(HexColor('#27ae60'))  # Vert pour "PAYÉE"
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(width - 20*mm, height - 36*mm, "PAYÉE")
        c.setFillColor(white)  # Remettre la couleur blanche pour la suite
    
    c.setFont("Helvetica", 9)
    c.setFillColor(white)
    y_date = height - 42*mm if est_payee else height - 36*mm
    c.drawRightString(width - 20*mm, y_date, f"Date : {datetime.now().strftime('%d/%m/%Y')}")
    
    if data.numero_devis_origine:
        c.setFont("Helvetica", 8)
        y_ref_devis = y_date - 6*mm
        c.drawRightString(width - 20*mm, y_ref_devis, f"Réf. devis : {data.numero_devis_origine}")
    
    y_position = height - 60*mm
    dessiner_bloc_emetteur(c, width, height, data, y_position)
    dessiner_bloc_client(c, width, height, data, y_position)
    
    c.setFillColor(GRIS_TEXTE)
    c.setFont("Helvetica", 9)
    if not est_payee:
        c.drawRightString(width - 20*mm, y_position - 28*mm, f"Échéance : {date_echeance}")
    
    y_table = y_position - 50*mm
    y_totaux, total_ht, total_ttc = dessiner_tableau_prestations(c, width, data, y_table, data.tva_taux)
    
    y_paiement = y_totaux - 45*mm
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(15*mm, y_paiement - 30*mm, width - 30*mm, 40*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(get_couleur_principale(data))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20*mm, y_paiement + 2*mm, "INFORMATIONS DE PAIEMENT")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    
    if est_payee:
        # Si la facture est payée, afficher "Reste à payer : 0 €"
        c.drawString(20*mm, y_paiement - 8*mm, f"• Reste à payer : 0,00 €")
        c.drawString(20*mm, y_paiement - 14*mm, "• Paiement reçu")
    else:
        # Sinon, afficher les informations normales
        c.drawString(20*mm, y_paiement - 8*mm, f"• Date d'échéance : {date_echeance}")
        c.drawString(20*mm, y_paiement - 14*mm, "• Mode de paiement : Virement bancaire, chèque ou espèces")
        c.drawString(20*mm, y_paiement - 20*mm, "• En cas de retard : pénalité de 3 fois le taux d'intérêt légal")
        c.drawString(20*mm, y_paiement - 26*mm, "• Indemnité forfaitaire pour frais de recouvrement : 40€")
    
    # Afficher le RIB si disponible
    if data.rib and data.rib.iban:
        y_rib = y_paiement - 45*mm
        c.setFillColor(GRIS_CLAIR)
        c.roundRect(15*mm, y_rib - 20*mm, width - 30*mm, 30*mm, 3*mm, fill=True, stroke=False)
        
        c.setFillColor(get_couleur_principale(data))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(20*mm, y_rib + 2*mm, "COORDONNÉES BANCAIRES")
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        c.drawString(20*mm, y_rib - 6*mm, f"IBAN : {data.rib.iban}")
        c.drawString(20*mm, y_rib - 12*mm, f"BIC : {data.rib.bic}")
        if data.rib.titulaire:
            c.drawString(20*mm, y_rib - 18*mm, f"Titulaire : {data.rib.titulaire}")
    
    mention_tva = ""
    if data.tva_taux == 0:
        mention_tva = data.mention_legale_tva or "TVA non applicable, article 293 B du Code général des impôts"
    
    dessiner_pied_page(c, width, data, mention_tva)
    try:
        c.save()
        print(f"✅ PDF facture sauvegardé: {filepath}")
    except Exception as e:
        print(f"❌ Erreur lors de la sauvegarde du PDF: {e}")
        raise
    
    return filepath, numero_facture, total_ht, total_ttc


# ==================== GÉNÉRATION WORD ====================

def set_cell_shading(cell, color):
    """Applique une couleur de fond à une cellule Word"""
    shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color}"/>')
    cell._tc.get_or_add_tcPr().append(shading_elm)

def generer_word_devis(data: DevisRequest) -> str:
    """Génère un devis au format Word"""
    # Utiliser le numéro fourni par le frontend, sinon en générer un
    if hasattr(data, 'numero_devis') and data.numero_devis and str(data.numero_devis).strip():
        numero_devis = data.numero_devis
    else:
        numero_devis = f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    filename = f"{numero_devis}.docx"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    date_devis = datetime.now().strftime("%d/%m/%Y")
    date_validite = (datetime.now() + timedelta(days=data.validite_jours)).strftime("%d/%m/%Y")
    
    doc = Document()
    
    # Marges
    sections = doc.sections
    for section in sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)
    
    # Logo si disponible
    logo_bytes = telecharger_logo_bytes(data.entreprise.logo_url)
    if logo_bytes:
        try:
            doc.add_picture(logo_bytes, width=Inches(1.2))
        except:
            pass
    
    # En-tête entreprise
    titre = doc.add_heading(data.entreprise.nom.upper(), 0)
    titre.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in titre.runs:
        run.font.color.rgb = get_couleur_principale_rgb(data)
    
    if data.entreprise.gerant:
        p = doc.add_paragraph(f"Gérant : {data.entreprise.gerant}")
        p.runs[0].font.size = Pt(10)
    
    # DEVIS + Numéro
    doc.add_paragraph()
    titre_devis = doc.add_heading("DEVIS", 1)
    titre_devis.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    p = doc.add_paragraph(f"N° {numero_devis}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Date : {date_devis}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Validité : {date_validite}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    doc.add_paragraph()
    
    # Tableau infos émetteur/destinataire
    table_info = doc.add_table(rows=1, cols=2)
    table_info.autofit = True
    
    # Émetteur
    cell_emetteur = table_info.rows[0].cells[0]
    cell_emetteur.text = ""
    p = cell_emetteur.add_paragraph()
    run = p.add_run("ÉMETTEUR")
    run.bold = True
    run.font.color.rgb = get_couleur_principale_rgb(data)
    cell_emetteur.add_paragraph(data.entreprise.nom)
    cell_emetteur.add_paragraph(data.entreprise.adresse)
    if data.entreprise.cp_ville:
        cell_emetteur.add_paragraph(data.entreprise.cp_ville)
    cell_emetteur.add_paragraph(f"Tél : {data.entreprise.tel}")
    cell_emetteur.add_paragraph(f"Email : {data.entreprise.email}")
    cell_emetteur.add_paragraph(f"SIRET : {data.entreprise.siret}")
    
    # Destinataire
    cell_dest = table_info.rows[0].cells[1]
    cell_dest.text = ""
    p = cell_dest.add_paragraph()
    run = p.add_run("DESTINATAIRE")
    run.bold = True
    run.font.color.rgb = get_couleur_principale_rgb(data)
    cell_dest.add_paragraph(data.client.nom)
    if data.client.adresse:
        cell_dest.add_paragraph(data.client.adresse)
    if data.client.cp_ville:
        cell_dest.add_paragraph(data.client.cp_ville)
    if data.client.tel:
        cell_dest.add_paragraph(f"Tél : {data.client.tel}")
    
    doc.add_paragraph()
    
    # Tableau des prestations
    table = doc.add_table(rows=1, cols=5)
    table.style = 'Table Grid'
    
    # En-tête
    header_cells = table.rows[0].cells
    headers = ['Description', 'Qté', 'Unité', 'P.U. HT', 'Total HT']
    for i, header in enumerate(headers):
        header_cells[i].text = header
        header_cells[i].paragraphs[0].runs[0].bold = True
        header_cells[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)
        set_cell_shading(header_cells[i], get_couleur_principale_hex_string(data))
    
    # Lignes
    total_ht = 0
    for prestation in data.prestations:
        row_cells = table.add_row().cells
        total_ligne = prestation.quantite * prestation.prix_unitaire
        total_ht += total_ligne
        
        row_cells[0].text = prestation.description
        row_cells[1].text = str(prestation.quantite)
        row_cells[2].text = prestation.unite
        row_cells[3].text = f"{prestation.prix_unitaire:.2f} €"
        row_cells[4].text = f"{total_ligne:.2f} €"
    
    doc.add_paragraph()
    
    # Totaux
    montant_tva = total_ht * (data.tva_taux / 100)
    total_ttc = total_ht + montant_tva
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(f"Total HT : {total_ht:.2f} €")
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if data.tva_taux > 0:
        p.add_run(f"TVA ({data.tva_taux}%) : {montant_tva:.2f} €")
    else:
        run = p.add_run("TVA non applicable")
        run.italic = True
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run(f"TOTAL TTC : {total_ttc:.2f} €")
    run.bold = True
    run.font.size = Pt(14)
    run.font.color.rgb = get_couleur_principale_rgb(data)
    
    doc.add_paragraph()
    
    # Conditions
    doc.add_heading("CONDITIONS", 2)
    doc.add_paragraph(f"• Délai de réalisation : {data.delai_realisation}")
    doc.add_paragraph(f"• Conditions de paiement : {data.entreprise.conditions_paiement or data.conditions_paiement}")
    doc.add_paragraph(f"• Devis valable jusqu'au : {date_validite}")
    
    doc.add_paragraph()
    
    # Signature
    doc.add_paragraph("Bon pour accord")
    doc.add_paragraph("Date : ________________")
    doc.add_paragraph("Signature : ________________")
    
    # Pied de page
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"{data.entreprise.nom} - SIRET {data.entreprise.siret}")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(128, 128, 128)
    
    if data.tva_taux == 0:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run("TVA non applicable, article 293 B du Code général des impôts")
        run.font.size = Pt(8)
        run.italic = True
    
    doc.save(filepath)
    
    return filepath, numero_devis, total_ht, total_ttc


def generer_word_facture(data: FactureRequest) -> str:
    """Génère une facture au format Word"""
    numero_facture = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    filename = f"{numero_facture}.docx"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    date_facture = datetime.now().strftime("%d/%m/%Y")
    date_echeance = (datetime.now() + timedelta(days=data.date_echeance_jours)).strftime("%d/%m/%Y")
    
    doc = Document()
    
    # Marges
    sections = doc.sections
    for section in sections:
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.5)
        section.left_margin = Cm(1.5)
        section.right_margin = Cm(1.5)
    
    # Logo si disponible
    logo_bytes = telecharger_logo_bytes(data.entreprise.logo_url)
    if logo_bytes:
        try:
            doc.add_picture(logo_bytes, width=Inches(1.2))
        except:
            pass
    
    # En-tête entreprise
    titre = doc.add_heading(data.entreprise.nom.upper(), 0)
    titre.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for run in titre.runs:
        run.font.color.rgb = get_couleur_principale_rgb(data)
    
    if data.entreprise.gerant:
        p = doc.add_paragraph(f"Gérant : {data.entreprise.gerant}")
        p.runs[0].font.size = Pt(10)
    
    # FACTURE + Numéro
    doc.add_paragraph()
    titre_facture = doc.add_heading("FACTURE", 1)
    titre_facture.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in titre_facture.runs:
        run.font.color.rgb = get_couleur_principale_rgb(data)
    
    p = doc.add_paragraph(f"N° {numero_facture}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Date : {date_facture}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if data.numero_devis_origine:
        p = doc.add_paragraph(f"Réf. devis : {data.numero_devis_origine}")
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p = doc.add_paragraph(f"Échéance : {date_echeance}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    
    doc.add_paragraph()
    
    # Tableau infos émetteur/destinataire
    table_info = doc.add_table(rows=1, cols=2)
    table_info.autofit = True
    
    # Émetteur
    cell_emetteur = table_info.rows[0].cells[0]
    cell_emetteur.text = ""
    p = cell_emetteur.add_paragraph()
    run = p.add_run("ÉMETTEUR")
    run.bold = True
    run.font.color.rgb = get_couleur_principale_rgb(data)
    cell_emetteur.add_paragraph(data.entreprise.nom)
    cell_emetteur.add_paragraph(data.entreprise.adresse)
    if data.entreprise.cp_ville:
        cell_emetteur.add_paragraph(data.entreprise.cp_ville)
    cell_emetteur.add_paragraph(f"Tél : {data.entreprise.tel}")
    cell_emetteur.add_paragraph(f"Email : {data.entreprise.email}")
    cell_emetteur.add_paragraph(f"SIRET : {data.entreprise.siret}")
    
    # Destinataire
    cell_dest = table_info.rows[0].cells[1]
    cell_dest.text = ""
    p = cell_dest.add_paragraph()
    run = p.add_run("DESTINATAIRE")
    run.bold = True
    run.font.color.rgb = get_couleur_principale_rgb(data)
    cell_dest.add_paragraph(data.client.nom)
    if data.client.adresse:
        cell_dest.add_paragraph(data.client.adresse)
    if data.client.cp_ville:
        cell_dest.add_paragraph(data.client.cp_ville)
    if data.client.tel:
        cell_dest.add_paragraph(f"Tél : {data.client.tel}")
    if data.client.email:
        cell_dest.add_paragraph(f"Email : {data.client.email}")
    
    doc.add_paragraph()
    
    # Tableau des prestations
    table = doc.add_table(rows=1, cols=5)
    table.style = 'Table Grid'
    
    # En-tête
    header_cells = table.rows[0].cells
    headers = ['Description', 'Qté', 'Unité', 'P.U. HT', 'Total HT']
    for i, header in enumerate(headers):
        header_cells[i].text = header
        header_cells[i].paragraphs[0].runs[0].bold = True
        header_cells[i].paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)
        set_cell_shading(header_cells[i], get_couleur_principale_hex_string(data))
    
    # Lignes
    total_ht = 0
    for prestation in data.prestations:
        row_cells = table.add_row().cells
        total_ligne = prestation.quantite * prestation.prix_unitaire
        total_ht += total_ligne
        
        row_cells[0].text = prestation.description
        row_cells[1].text = str(prestation.quantite)
        row_cells[2].text = prestation.unite
        row_cells[3].text = f"{prestation.prix_unitaire:.2f} €"
        row_cells[4].text = f"{total_ligne:.2f} €"
    
    doc.add_paragraph()
    
    # Totaux
    montant_tva = total_ht * (data.tva_taux / 100)
    total_ttc = total_ht + montant_tva
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(f"Total HT : {total_ht:.2f} €")
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if data.tva_taux > 0:
        p.add_run(f"TVA ({data.tva_taux}%) : {montant_tva:.2f} €")
    else:
        run = p.add_run("TVA non applicable")
        run.italic = True
    
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run(f"TOTAL TTC : {total_ttc:.2f} €")
    run.bold = True
    run.font.size = Pt(14)
    run.font.color.rgb = get_couleur_principale_rgb(data)
    
    doc.add_paragraph()
    
    # Informations de paiement
    doc.add_heading("INFORMATIONS DE PAIEMENT", 2)
    doc.add_paragraph(f"• Date d'échéance : {date_echeance}")
    doc.add_paragraph("• Mode de paiement : Virement bancaire, chèque ou espèces")
    doc.add_paragraph("• En cas de retard : pénalité de 3 fois le taux d'intérêt légal")
    doc.add_paragraph("• Indemnité forfaitaire pour frais de recouvrement : 40€")
    
    # Pied de page
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"{data.entreprise.nom} - SIRET {data.entreprise.siret}")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(128, 128, 128)
    
    if data.tva_taux == 0:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(data.mention_legale_tva or "TVA non applicable, article 293 B du Code général des impôts")
        run.font.size = Pt(8)
        run.italic = True
    
    doc.save(filepath)
    
    return filepath, numero_facture, total_ht, total_ttc


# ==================== ROUTES API ====================

@app.get("/")
def root():
    return {"message": "MonDevisPro API", "version": "3.0.0", "status": "ok"}


@app.post("/generer-devis")
async def generer_devis_endpoint(data: DevisRequest):
    try:
        print(f"📄 Début génération devis pour client: {data.client.nom}")
        print(f"📊 Nombre de prestations: {len(data.prestations)}")
        print(f"🎨 Couleur PDF: {data.entreprise.couleur_pdf or 'défaut'}")
        
        # Générer PDF
        print("📝 Génération PDF...")
        filepath_pdf, numero_devis, total_ht, total_ttc = generer_pdf_devis(data)
        print(f"✅ PDF généré: {filepath_pdf}")
        
        # Générer Word
        print("📝 Génération Word...")
        filepath_word, _, _, _ = generer_word_devis(data)
        # Renommer le Word pour avoir le même numéro
        new_word_path = os.path.join(PDF_FOLDER, f"{numero_devis}.docx")
        if os.path.exists(filepath_word) and filepath_word != new_word_path:
            os.rename(filepath_word, new_word_path)
        print(f"✅ Word généré: {new_word_path}")
        
        # Upload sur Supabase Storage
        print("📤 Upload PDF sur Supabase...")
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis}.pdf")
        print(f"✅ PDF uploadé: {pdf_url}")
        
        print("📤 Upload Word sur Supabase...")
        word_url = upload_to_supabase(new_word_path, f"{numero_devis}.docx")
        print(f"✅ Word uploadé: {word_url}")
        
        return {
            "success": True,
            "numero_devis": numero_devis,
            "total_ht": total_ht,
            "total_ttc": total_ttc,
            "pdf_filename": f"{numero_devis}.pdf",
            "pdf_url": pdf_url,
            "word_filename": f"{numero_devis}.docx",
            "word_url": word_url
        }
    except Exception as e:
        print(f"❌ Erreur dans generer_devis_endpoint: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generer-devis-simple")
async def generer_devis_simple_endpoint(data: DevisRequestSimple):
    try:
        tva_taux = data.entreprise.tva_taux if data.entreprise.tva_taux is not None else 20.0
        conditions = data.entreprise.conditions_paiement or "30% à la commande, solde à réception"
        
        full_data = DevisRequest(
            entreprise=data.entreprise,
            client=Client(
                nom=data.devis_data.client_nom,
                adresse="",
                cp_ville="",
                tel=""
            ),
            prestations=data.devis_data.prestations,
            tva_taux=tva_taux,
            conditions_paiement=conditions,
            delai_realisation=data.devis_data.delai,
            validite_jours=data.validite_jours,
            remise_type=data.devis_data.remise_type,
            remise_valeur=data.devis_data.remise_valeur or 0
        )
        
        # Générer PDF
        filepath_pdf, numero_devis, total_ht, total_ttc = generer_pdf_devis(full_data)
        
        # Générer Word
        filepath_word, _, _, _ = generer_word_devis(full_data)
        new_word_path = os.path.join(PDF_FOLDER, f"{numero_devis}.docx")
        if os.path.exists(filepath_word) and filepath_word != new_word_path:
            os.rename(filepath_word, new_word_path)
        
        # Upload sur Supabase Storage
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis}.pdf")
        word_url = upload_to_supabase(new_word_path, f"{numero_devis}.docx")
        
        return {
            "success": True,
            "numero_devis": numero_devis,
            "total_ht": total_ht,
            "total_ttc": total_ttc,
            "pdf_filename": f"{numero_devis}.pdf",
            "pdf_url": pdf_url,
            "word_filename": f"{numero_devis}.docx",
            "word_url": word_url
        }
    except Exception as e:
        print(f"❌ Erreur dans generer_devis_simple_endpoint: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generer-facture")
async def generer_facture_endpoint(data: FactureRequest):
    try:
        # Générer PDF
        filepath_pdf, numero_facture, total_ht, total_ttc = generer_pdf_facture(data)
        
        # Générer Word
        filepath_word, _, _, _ = generer_word_facture(data)
        new_word_path = os.path.join(PDF_FOLDER, f"{numero_facture}.docx")
        if os.path.exists(filepath_word) and filepath_word != new_word_path:
            os.rename(filepath_word, new_word_path)
        
        # Upload sur Supabase Storage
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_facture}.pdf")
        word_url = upload_to_supabase(new_word_path, f"{numero_facture}.docx")
        
        return {
            "success": True,
            "numero_facture": numero_facture,
            "total_ht": total_ht,
            "total_ttc": total_ttc,
            "pdf_filename": f"{numero_facture}.pdf",
            "pdf_url": pdf_url,
            "word_filename": f"{numero_facture}.docx",
            "word_url": word_url
        }
    except Exception as e:
        print(f"❌ Erreur dans generer_facture_endpoint: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/{filename}")
async def download_file(filename: str):
    filepath = os.path.join(PDF_FOLDER, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Fichier non trouvé")
    
    # Déterminer le type MIME
    if filename.endswith('.pdf'):
        media_type = "application/pdf"
    elif filename.endswith('.docx'):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        media_type = "application/octet-stream"
    
    return FileResponse(filepath, media_type=media_type, filename=filename)


@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/debug-env")
def debug_env():
    """Endpoint de debug pour voir les variables d'environnement (à supprimer après)"""
    all_env = dict(os.environ)
    # Masquer les valeurs sensibles
    safe_env = {}
    for key, value in all_env.items():
        if any(sensitive in key.upper() for sensitive in ['KEY', 'PASSWORD', 'SECRET', 'TOKEN']):
            safe_env[key] = f"{value[:10]}... (masqué)" if value else "VIDE"
        else:
            safe_env[key] = value[:50] + "..." if len(value) > 50 else value
    
    return {
        "all_env_keys": sorted(list(all_env.keys())),
        "supabase_vars": {
            "SUPABASE_URL": "OUI" if os.getenv("SUPABASE_URL") else "NON",
            "SUPABASE_SERVICE_KEY": "OUI" if os.getenv("SUPABASE_SERVICE_KEY") else "NON",
            "RAILWAY_SUPABASE_URL": "OUI" if os.getenv("RAILWAY_SUPABASE_URL") else "NON",
            "DATABASE_URL": "OUI" if os.getenv("DATABASE_URL") else "NON",
        },
        "safe_env": safe_env
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)