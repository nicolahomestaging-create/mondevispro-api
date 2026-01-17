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
    acompte_references: Optional[List[str]] = None  # Références des factures d'acompte (numéros) pour affichage
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


def dessiner_totaux_devis(c, width, y_totaux, total_ht_initial, total_ht_final, remise_totale, tva_par_taux, total_ttc, data, lignes_deja_remisees):
    """
    Dessine les totaux pour un devis - utilise les lignes normalisées comme source de vérité
    
    RÈGLE ABSOLUE : Les lignes affichées sont DÉJÀ remisées (remise appliquée ligne par ligne)
    → AUCUNE remise globale à afficher (incompatible avec multi-TVA)
    → Afficher UNIQUEMENT : Total HT, TVA par taux, Total TTC
    """
    x_label = 130*mm
    x_value = width - 18*mm
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    
    y_offset = 0
    
    # RÈGLE ABSOLUE : Les lignes sont TOUJOURS remisées (remise appliquée ligne par ligne)
    # → Afficher UNIQUEMENT : Total HT (somme des lignes déjà remisées)
    # → JAMAIS de "Remise" ou "Total HT après remise" (incompatible avec multi-TVA)
    c.drawString(x_label, y_totaux, "Total HT")
    c.drawRightString(x_value, y_totaux, f"{total_ht_final:.2f} €")
    y_offset = 6*mm
    
    # Afficher TVA par taux
    for taux in sorted(tva_par_taux.keys()):
        montant = tva_par_taux[taux]
        if taux > 0:
            c.drawString(x_label, y_totaux - y_offset, f"TVA ({taux}%)")
            c.drawRightString(x_value, y_totaux - y_offset, f"{montant:.2f} €")
            y_offset += 6*mm
        elif len(tva_par_taux) == 1:
            c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
            y_offset += 6*mm
    
    # Total TTC
    c.setFillColor(get_couleur_principale(data))
    c.roundRect(x_label - 5*mm, y_totaux - y_offset - 8*mm, 68*mm, 10*mm, 2*mm, fill=True, stroke=False)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x_label, y_totaux - y_offset - 5*mm, "TOTAL TTC")
    c.drawRightString(x_value, y_totaux - y_offset - 5*mm, f"{total_ttc:.2f} €")
    
    return y_totaux - y_offset - 8*mm


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


def dessiner_lignes_normalisees(c, width, lignes_normalisees, y_table, data, index_debut=0):
    """Dessine les lignes normalisées (en-tête + lignes) et retourne la position Y finale"""
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
    
    # Dessiner les lignes normalisées
    for i, ligne in enumerate(lignes_normalisees):
        y_ligne -= 10*mm
        
        # Alterner les couleurs de fond
        if (index_debut + i) % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(ligne['description'], 50))
        c.drawString(90*mm, y_ligne + 2*mm, str(ligne['quantite']))
        c.drawString(105*mm, y_ligne + 2*mm, ligne['unite'])
        
        # Prix unitaire affiché (calculé depuis ht_final)
        prix_unitaire = ligne['ht_final'] / ligne['quantite'] if ligne['quantite'] > 0 else 0
        c.drawString(125*mm, y_ligne + 2*mm, f"{prix_unitaire:.2f} €")
        c.drawString(150*mm, y_ligne + 2*mm, f"{ligne['tva_taux']:.1f}%")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{ligne['ht_final']:.2f} €")
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    return y_ligne - 10*mm


def calculer_lignes_finales(data, tva_taux_global):
    """
    Calcule les lignes finales avec normalisation, fusion et remise.
    Cette fonction est la SOURCE UNIQUE de vérité pour tous les calculs.
    
    RÈGLE MÉTIER FONDAMENTALE :
    Un devis figé est une source de vérité ABSOLUE.
    Si une facture est générée à partir d'un devis accepté (option A / devis figé),
    alors AUCUN recalcul n'est autorisé.
    
    COMPORTEMENT DEVIS FIGÉ (STRICT) :
    Si devis_fige == True :
    - Utiliser UNIQUEMENT lignes_finales_devis
    - Aucune normalisation de description
    - Aucune fusion de lignes
    - Aucun recalcul de TVA
    - Aucun remapping de taux
    - Les champs ht_final et tva_taux sont considérés COMME DÉFINITIFS
    
    CALCUL DES TOTAUX (unique et simple) :
    - total_ht = somme(ligne.ht_final)
    - total_tva = somme(ht_final × tva_taux par ligne)
    - total_ttc = total_ht + total_tva
    
    FACTURE FINALE AVEC ACOMPTE :
    Si acompte_ttc_deja_facture est présent :
    - net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
    - NE PAS recalculer la TVA
    - NE PAS répartir l'acompte en HT/TVA
    
    INTERDICTIONS EXPLICITES :
    - INTERDIT de recalculer les lignes à partir des règles courantes
    - INTERDIT de modifier la TVA entre devis et facture
    - INTERDIT de "corriger" les données du devis
    
    CONTRÔLE DE SÉCURITÉ :
    Si facture issue d'un devis figé ET si total recalculé ≠ total devis
    → lever une erreur explicite
    
    RÈGLES DE FUSION (cas normal, pas devis figé) :
    - Fusion uniquement si description normalisée + TVA + unité identiques
    - Si description identique mais TVA ou unité différente → lignes distinctes (warning)
    - Le moteur ne corrige pas les erreurs de saisie métier, il reflète strictement les données
    
    BUT FINAL :
    Même devis → même facture (hors acompte).
    Le moteur doit être DÉTERMINISTE, TRAÇABLE et FISCALLEMENT CONFORME.
    
    Retourne :
    - lignes_normalisees : liste des lignes finales (après normalisation/fusion/remise)
    - total_ht_initial : somme des HT avant remise
    - total_ht_final : somme des HT après remise
    - tva_par_taux : dictionnaire {taux: montant_tva}
    - total_tva : somme des TVA
    - total_ttc : total HT + total TVA
    - lignes_deja_remisees : booléen indiquant si les lignes sont déjà remisées
    - devis_fige : booléen indiquant si c'est un devis figé (source de vérité absolue)
    - warnings : liste des warnings de cohérence métier (prestations similaires avec TVA/unité différentes)
    """
    # Récupérer les paramètres
    acompte_ttc_deja_facture = getattr(data, 'acompte_ttc_deja_facture', 0) or 0
    is_facture_acompte = getattr(data, 'is_facture_acompte', False)
    taux_acompte = getattr(data, 'taux_acompte', None)
    lignes_finales_devis = getattr(data, 'lignes_finales_devis', None)
    
    # ============================================================
    # VALIDATION STRICTE : Facture issue d'un devis
    # ============================================================
    
    # Si lignes_finales_devis est présent, la facture est issue d'un devis
    # → Vérifier que les prestations ne modifient pas les lignes du devis
    # NOTE : Cette validation est optionnelle car on utilisera TOUJOURS lignes_finales_devis
    # même si des prestations sont fournies
    if lignes_finales_devis and len(lignes_finales_devis) > 0:
        # RÈGLE ABSOLUE : Les lignes de facture DOIVENT être identiques au devis
        # Vérifier si des prestations sont aussi fournies (ce qui serait une tentative de modification)
        if hasattr(data, 'prestations') and data.prestations and len(data.prestations) > 0:
            # Comparer chaque ligne du devis avec les prestations fournies
            erreurs_validation = []
            
            # Créer un mapping des lignes du devis par description normalisée + TVA + unité
            lignes_devis_map = {}
            for ligne_devis in lignes_finales_devis:
                cle = (
                    ligne_devis.description.strip().lower(),
                    ligne_devis.tva_taux,
                    ligne_devis.unite
                )
                if cle not in lignes_devis_map:
                    lignes_devis_map[cle] = []
                lignes_devis_map[cle].append(ligne_devis)
            
            # Vérifier chaque prestation fournie
            for i, prestation in enumerate(data.prestations):
                desc_norm = prestation.description.strip().lower()
                tva_prestation = prestation.tva_taux if prestation.tva_taux is not None else tva_taux_global
                unite_prestation = prestation.unite
                ht_prestation = prestation.quantite * prestation.prix_unitaire
                
                cle = (desc_norm, tva_prestation, unite_prestation)
                
                if cle in lignes_devis_map:
                    # Ligne trouvée dans le devis → vérifier que HT et TVA sont identiques
                    ligne_devis_correspondante = lignes_devis_map[cle][0]
                    ht_devis = ligne_devis_correspondante.ht_apres_remise
                    
                    # Vérifier HT (tolérance de 0.01 € pour arrondis)
                    if abs(ht_prestation - ht_devis) > 0.01:
                        erreurs_validation.append(
                            f"Ligne {i+1} '{prestation.description}': HT facture ({ht_prestation:.2f} €) "
                            f"≠ HT devis ({ht_devis:.2f} €)"
                        )
                    
                    # Vérifier TVA
                    if abs(tva_prestation - ligne_devis_correspondante.tva_taux) > 0.01:
                        erreurs_validation.append(
                            f"Ligne {i+1} '{prestation.description}': TVA facture ({tva_prestation}%) "
                            f"≠ TVA devis ({ligne_devis_correspondante.tva_taux}%)"
                        )
                    
                    # Vérifier unité
                    if unite_prestation != ligne_devis_correspondante.unite:
                        erreurs_validation.append(
                            f"Ligne {i+1} '{prestation.description}': Unité facture ('{unite_prestation}') "
                            f"≠ Unité devis ('{ligne_devis_correspondante.unite}')"
                        )
                else:
                    # Ligne non trouvée dans le devis → nouvelle ligne interdite
                    erreurs_validation.append(
                        f"Ligne {i+1} '{prestation.description}' n'existe pas dans le devis. "
                        f"Les factures issues d'un devis ne peuvent pas ajouter de nouvelles lignes."
                    )
            
            # NOTE : Validation désactivée car on utilisera TOUJOURS lignes_finales_devis
            # Les prestations peuvent être modifiées par l'utilisateur dans l'UI, mais elles seront ignorées
            # On log juste un avertissement pour diagnostic
            if erreurs_validation:
                print(f"⚠️ AVERTISSEMENT: Différences détectées entre prestations et lignes_finales_devis")
                print(f"   Erreurs: {len(erreurs_validation)}")
                for err in erreurs_validation[:3]:  # Limiter à 3 pour ne pas surcharger les logs
                    print(f"     - {err}")
                print(f"   → Les prestations seront IGNORÉES, utilisation de lignes_finales_devis")
                # On ne lève plus d'erreur, on ignore simplement les prestations
    
    # ============================================================
    # ÉTAPE 1 : CONSTRUIRE LES LIGNES FINALES (source de vérité)
    # ============================================================
    
    # ============================================================
    # DÉTECTION DEVIS FIGÉ (CENTRALE ET PROPAGÉE PARTOUT)
    # ============================================================
    
    # RÈGLE MÉTIER FONDAMENTALE : Un devis figé est une source de vérité ABSOLUE
    # Si une facture est générée à partir d'un devis accepté (option A / devis figé),
    # alors AUCUN recalcul n'est autorisé.
    
    devis_fige = (lignes_finales_devis and len(lignes_finales_devis) > 0)
    
    # RÈGLE ABSOLUE : Si devis figé, IGNORER complètement les prestations du FactureRequest
    # Le devis figé est la source unique de vérité, aucune autre source n'est autorisée
    # INTERDICTIONS EXPLICITES :
    # - INTERDIT de recalculer les lignes à partir des règles courantes
    # - INTERDIT de modifier la TVA entre devis et facture
    # - INTERDIT de "corriger" les données du devis
    if devis_fige:
        # Vérifier qu'on n'essaie pas d'utiliser des prestations différentes
        if hasattr(data, 'prestations') and data.prestations and len(data.prestations) > 0:
            # La validation stricte a déjà été faite plus haut (ligne ~698)
            # Si on arrive ici, c'est que les prestations sont identiques au devis
            # → On les ignore quand même et on utilise uniquement lignes_finales_devis
            pass
    
    lignes_finales = []  # Liste des lignes finales à afficher
    
    if devis_fige:
        # ============================================================
        # CAS A : DEVIS FIGÉ - Facture issue d'un devis (STRICT)
        # ============================================================
        # RÈGLE MÉTIER FONDAMENTALE : Un devis figé est une source de vérité ABSOLUE
        # → Utiliser UNIQUEMENT lignes_finales_devis
        # → Aucune normalisation de description
        # → Aucune fusion de lignes
        # → Aucun recalcul de TVA
        # → Aucun remapping de taux
        # → Les champs ht_final et tva_taux sont considérés COMME DÉFINITIFS
        
        for ligne in lignes_finales_devis:
            # Copie directe sans aucune modification
            # Les valeurs sont DÉFINITIVES et ne doivent JAMAIS être recalculées
            lignes_finales.append({
                'description': ligne.description,      # Description EXACTE (pas de strip/lower)
                'quantite': ligne.quantite,            # Quantité FIGÉE
                'unite': ligne.unite,                   # Unité FIGÉE
                'ht_initial': ligne.ht_apres_remise,   # HT déjà remisé (FIGÉ)
                'ht_final': ligne.ht_apres_remise,     # HT FIGÉ (DÉFINITIF - ne jamais recalculer)
                'tva_taux': ligne.tva_taux,            # TVA FIGÉE (DÉFINITIF - ne jamais modifier)
                'deja_remise': True,
                'devis_fige': True  # Flag pour bypasser TOUTE logique de traitement
            })
    else:
        # CAS B : Lignes non remisées (calcul normal)
        # → Calculer HT initial, appliquer remise ligne par ligne
        remise_type = getattr(data, 'remise_type', None)
        remise_valeur = getattr(data, 'remise_valeur', 0) or 0
        
        # Calculer le ratio de remise si montant fixe
        total_ht_initial_global = sum(p.quantite * p.prix_unitaire for p in data.prestations if p.quantite * p.prix_unitaire > 0)
        ratio_remise = 0
        if remise_type == "montant" and total_ht_initial_global > 0:
            ratio_remise = remise_valeur / total_ht_initial_global
        elif remise_type == "pourcentage":
            ratio_remise = remise_valeur / 100
        
        for prestation in data.prestations:
            ht_initial = prestation.quantite * prestation.prix_unitaire
            if ht_initial <= 0:
                continue
            
            # RÈGLE ABSOLUE : Appliquer remise ligne par ligne AVANT TVA
            # Dans un panier multi-TVA, il n'existe PAS de remise globale
            # La remise DOIT être appliquée ligne par ligne
            if remise_type == "pourcentage":
                # ht_final = ht_initial * (1 - remise_pct)
                ht_final = ht_initial * (1 - ratio_remise)
            elif remise_type == "montant":
                # Répartir proportionnellement
                remise_ligne = ht_initial * ratio_remise
                ht_final = ht_initial - remise_ligne
            else:
                ht_final = ht_initial
            
            # Taux TVA
            tva_taux = prestation.tva_taux if prestation.tva_taux is not None else tva_taux_global
            if tva_taux is None:
                raise ValueError(f"Taux TVA manquant pour '{prestation.description}'")
            
            lignes_finales.append({
                'description': prestation.description,
                'quantite': prestation.quantite,
                'unite': prestation.unite,
                'ht_initial': ht_initial,
                'ht_final': ht_final,
                'tva_taux': tva_taux,
                'deja_remise': False
            })
    
    # ============================================================
    # ÉTAPE 2 : NORMALISATION ET FUSION (AVANT tout calcul)
    # ============================================================
    
    # ============================================================
    # ÉTAPE 2 : NORMALISATION ET FUSION (AVANT tout calcul)
    # ============================================================
    
    # RÈGLE ABSOLUE : Si devis figé → AUCUNE normalisation, AUCUNE fusion, AUCUN traitement
    # INTERDICTIONS EXPLICITES :
    # - INTERDIT de recalculer les lignes à partir des règles courantes
    # - INTERDIT de modifier la TVA entre devis et facture
    # - INTERDIT de "corriger" les données du devis
    # - INTERDIT toute normalisation de description
    # - INTERDIT toute fusion de lignes
    # - INTERDIT tout remapping de taux TVA
    
    if devis_fige:
        # ============================================================
        # DEVIS FIGÉ : Utiliser les lignes telles quelles (miroir exact du devis)
        # ============================================================
        # → Pas de normalisation (description conservée exactement, pas de strip/lower)
        # → Pas de fusion (toutes les lignes conservées distinctes, même si descriptions similaires)
        # → Pas de traitement intelligent (les lignes sont immuables)
        # → Les lignes sont déjà figées dans le devis, aucune modification autorisée
        # → Aucune logique métier intelligente n'est autorisée sur un devis figé
        
        lignes_normalisees = []
        for i, ligne in enumerate(lignes_finales):
            # Copie directe sans aucune modification
            # ASSERTION : Les lignes doivent être identiques au devis
            assert ligne.get('devis_fige', False), f"ERREUR: Ligne {i+1} devis figé sans flag devis_fige"
            
            # Vérifier que les valeurs correspondent exactement au devis
            ligne_devis_originale = lignes_finales_devis[i]
            assert ligne['description'] == ligne_devis_originale.description, \
                f"ERREUR: Description modifiée ligne {i+1}"
            assert ligne['quantite'] == ligne_devis_originale.quantite, \
                f"ERREUR: Quantité modifiée ligne {i+1}"
            assert ligne['unite'] == ligne_devis_originale.unite, \
                f"ERREUR: Unité modifiée ligne {i+1}"
            assert abs(ligne['ht_final'] - ligne_devis_originale.ht_apres_remise) < 0.01, \
                f"ERREUR: HT modifié ligne {i+1} (facture: {ligne['ht_final']:.2f}, devis: {ligne_devis_originale.ht_apres_remise:.2f})"
            assert abs(ligne['tva_taux'] - ligne_devis_originale.tva_taux) < 0.01, \
                f"ERREUR: TVA modifiée ligne {i+1} (facture: {ligne['tva_taux']:.2f}%, devis: {ligne_devis_originale.tva_taux:.2f}%)"
            
            # Copie directe : les valeurs sont DÉFINITIVES
            lignes_normalisees.append({
                'description': ligne['description'],  # EXACTEMENT comme dans le devis (pas de normalisation)
                'quantite': ligne['quantite'],        # Quantité FIGÉE
                'unite': ligne['unite'],              # Unité FIGÉE
                'ht_initial': ligne['ht_initial'],     # HT initial FIGÉ
                'ht_final': ligne['ht_final'],        # HT final FIGÉ (DÉFINITIF - ne jamais recalculer)
                'tva_taux': ligne['tva_taux'],        # TVA FIGÉE (DÉFINITIF - ne jamais modifier)
                'deja_remise': ligne['deja_remise'],
                'devis_fige': True
            })
        warnings = []  # Pas de warnings pour devis figé (les lignes sont immuables)
        
        # ASSERTION DE SÉCURITÉ : Vérifier qu'on a bien le même nombre de lignes
        assert len(lignes_normalisees) == len(lignes_finales_devis), \
            f"ERREUR: Nombre de lignes différent ({len(lignes_normalisees)} vs {len(lignes_finales_devis)})"
    else:
        # CAS NORMAL : Normalisation et fusion autorisées
        # RÈGLE STRICTE : Fusion uniquement si description + TVA + unité identiques
        # Clé de fusion : (description_norm, tva_taux, unite)
        cles_fusion = {}  # {(desc_norm, tva_taux, unite): index}
        lignes_normalisees = []
        warnings = []  # Liste des warnings de cohérence métier
        
        for ligne in lignes_finales:
            desc_norm = ligne['description'].strip().lower()
            tva_taux = ligne['tva_taux']
            unite = ligne['unite']
            
            # Clé de fusion : description + TVA + unité
            cle_fusion = (desc_norm, tva_taux, unite)
            
            if cle_fusion in cles_fusion:
                # Fusionner avec ligne existante (description + TVA + unité identiques)
                index = cles_fusion[cle_fusion]
                ligne_existante = lignes_normalisees[index]
                
                # Fusionner : additionner quantités et HT
                ligne_existante['quantite'] += ligne['quantite']
                ligne_existante['ht_final'] += ligne['ht_final']
                ligne_existante['ht_initial'] += ligne['ht_initial']
            else:
                # Vérifier si description identique mais TVA ou unité différente (warning)
                desc_similaire = False
                for (desc_existante, tva_existante, unite_existante), index_existant in cles_fusion.items():
                    if desc_existante == desc_norm and (tva_existante != tva_taux or unite_existante != unite):
                        desc_similaire = True
                        ligne_existante = lignes_normalisees[index_existant]
                        warnings.append(
                            f"Prestations similaires '{ligne['description']}' avec TVA/unité différentes : "
                            f"TVA {tva_existante}%/{unite_existante} vs TVA {tva_taux}%/{unite} - "
                            f"Lignes conservées distinctes"
                        )
                        break
                
                # Nouvelle ligne (description + TVA + unité unique)
                cles_fusion[cle_fusion] = len(lignes_normalisees)
                lignes_normalisees.append(ligne.copy())
        
        # Afficher les warnings si présents
        if warnings:
            print("⚠️ WARNINGS DE COHÉRENCE MÉTIER:")
            for warning in warnings:
                print(f"  - {warning}")
    
    # ============================================================
    # ÉTAPE 3 : APPLIQUER ACOMPTE SI FACTURE D'ACOMPTE
    # ============================================================
    
    # RÈGLE : L'acompte ne s'applique QUE pour les factures d'acompte
    # Pour les factures finales issues d'un devis figé, on déduit l'acompte TTC après (étape 5)
    # Les factures d'acompte sont des factures séparées qui ne modifient jamais les lignes du devis
    if is_facture_acompte and taux_acompte is not None and taux_acompte > 0:
        # Facture d'acompte : calculer l'acompte proportionnellement sur chaque ligne
        # Note: Pour un devis figé, même l'acompte doit respecter les lignes du devis
        for ligne in lignes_normalisees:
            # Calculer l'acompte sur le HT figé (proportionnellement)
            ligne['ht_final'] = ligne['ht_final'] * (taux_acompte / 100)
            # La TVA sera recalculée sur ce HT d'acompte (étape 4)
    
    # ============================================================
    # ÉTAPE 4 : CALCULER TVA LIGNE PAR LIGNE (source de vérité)
    # ============================================================
    
    # RÈGLE ABSOLUE : TVA calculée uniquement comme ht_final × tva_taux
    # Pour devis figé : le taux TVA est FIGÉ dans chaque ligne, jamais modifié
    # → Utiliser directement le taux TVA de chaque ligne (aucun recalcul de taux)
    # → Aucun remapping de taux, aucune redistribution
    # → Le calcul est DÉTERMINISTE et REPRODUCTIBLE
    
    tva_par_taux = {}
    for ligne in lignes_normalisees:
        # TVA = ht_final × tva_taux (calcul unique et simple)
        # Pour devis figé : tva_taux est DÉFINITIF, ht_final est DÉFINITIF
        # → Le calcul est déterministe et reproductible
        # → Même devis → même facture (hors acompte)
        tva_ligne = ligne['ht_final'] * (ligne['tva_taux'] / 100)
        tva_par_taux[ligne['tva_taux']] = tva_par_taux.get(ligne['tva_taux'], 0) + tva_ligne
    
    # ============================================================
    # ÉTAPE 5 : CALCULER LES TOTAUX (somme des lignes uniquement)
    # ============================================================
    
    # RÈGLE ABSOLUE : Les totaux sont UNIQUEMENT la somme des lignes
    # → Total HT = somme(ht_ligne_final) des lignes
    # → TVA = somme(tva_ligne) calculée ligne par ligne
    # → Total TTC = Total HT + TVA
    # → Aucun recalcul global, aucun ajustement, aucune correction
    # → Interdiction absolue de recalculer la TVA à partir d'un autre total
    
    # Pour devis figé :
    # - total_ht = somme(ht_ligne_final) des lignes du devis
    # - total_tva = somme(tva_ligne) où tva_ligne = ht_ligne_final × tva_rate
    # - total_ttc = total_ht + total_tva
    # - Même devis ⇒ même facture (hors acompte)
    
    total_ht_initial = sum(ligne['ht_initial'] for ligne in lignes_normalisees)
    total_ht_final = sum(ligne['ht_final'] for ligne in lignes_normalisees)
    total_tva = sum(tva_par_taux.values())  # Somme des TVA par ligne (issue des lignes, pas recalculée)
    total_ttc = total_ht_final + total_tva  # Total TTC = HT + TVA
    
    # Pour facture finale issue d'un devis figé avec acompte :
    # Net à payer TTC = Total TTC devis figé - somme des acomptes TTC déjà facturés
    # La TVA n'est JAMAIS recalculée après déduction de l'acompte
    # → NE PAS recalculer la TVA
    # → NE PAS déduire d'HT
    # → Calculer uniquement : net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
    
    # Détecter si lignes déjà remisées
    lignes_deja_remisees = any(ligne.get('deja_remise', False) for ligne in lignes_normalisees)
    
    # ============================================================
    # ÉTAPE 6 : CONTRÔLES DE COHÉRENCE (OBLIGATOIRES)
    # ============================================================
    
    # Vérifier que les totaux correspondent aux lignes
    total_ht_verif = sum(ligne['ht_final'] for ligne in lignes_normalisees)
    total_tva_verif = sum(ligne['ht_final'] * (ligne['tva_taux'] / 100) for ligne in lignes_normalisees)
    
    if abs(total_ht_final - total_ht_verif) > 0.01:
        raise ValueError(f"ERREUR COHÉRENCE: total_ht_final ({total_ht_final}) != somme lignes ({total_ht_verif})")
    
    if abs(total_tva - total_tva_verif) > 0.01:
        raise ValueError(f"ERREUR COHÉRENCE: total_tva ({total_tva}) != somme TVA lignes ({total_tva_verif})")
    
    if abs(total_ttc - (total_ht_final + total_tva)) > 0.01:
        raise ValueError(f"ERREUR COHÉRENCE: total_ttc ({total_ttc}) != total_ht + total_tva ({total_ht_final + total_tva})")
    
    # ============================================================
    # CONTRÔLE DE SÉCURITÉ : Facture finale issue d'un devis figé
    # ============================================================
    
    # RÈGLE MÉTIER FONDAMENTALE : Même devis → même facture (hors acompte)
    # Le moteur doit être DÉTERMINISTE, TRAÇABLE et FISCALLEMENT CONFORME
    
    if devis_fige and not is_facture_acompte:
        # ============================================================
        # VALIDATION STRICTE : Facture finale issue d'un devis figé
        # ============================================================
        # RÈGLE ABSOLUE : Facture TTC = Devis TTC − Acompte TTC (si présent)
        # Les montants affichés correspondent EXACTEMENT aux lignes affichées
        # Même devis ⇒ même facture (hors acompte)
        
        # Calculer le total TTC théorique du devis (pour validation)
        # Formule : somme(ht_apres_remise × (1 + tva_taux / 100)) pour chaque ligne
        total_ttc_theorique = sum(
            ligne.ht_apres_remise * (1 + ligne.tva_taux / 100)
            for ligne in lignes_finales_devis
        )
        
        # CONTRÔLE DE SÉCURITÉ : Vérifier que le total TTC calculé correspond au total théorique
        # Si facture issue d'un devis figé ET si total recalculé ≠ total devis
        # → lever une erreur explicite
        if abs(total_ttc - total_ttc_theorique) > 0.01:
            raise ValueError(
                f"ERREUR CRITIQUE - INCOHÉRENCE DEVIS/FACTURE:\n"
                f"  Total TTC facture recalculé: {total_ttc:.2f} €\n"
                f"  Total TTC devis (source de vérité): {total_ttc_theorique:.2f} €\n"
                f"  Écart: {abs(total_ttc - total_ttc_theorique):.2f} €\n\n"
                f"RÈGLE VIOLÉE: Une facture issue d'un devis figé doit avoir un total TTC identique (hors acompte).\n"
                f"Un devis figé est une source de vérité ABSOLUE. Aucun recalcul n'est autorisé.\n"
                f"Vérifiez que les lignes du devis sont reprises à l'identique sans modification."
            )
        
        # Si un acompte a déjà été facturé, le net à payer sera différent
        # mais le total TTC de base (avant déduction acompte) doit être identique
        if acompte_ttc_deja_facture > 0:
            # RÈGLE : net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
            # → NE PAS recalculer la TVA
            # → NE PAS répartir l'acompte en HT/TVA
            net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
            
            # Validation : net_a_payer_ttc doit être positif ou nul
            if net_a_payer_ttc < 0:
                raise ValueError(
                    f"ERREUR VALIDATION ACOMPTE:\n"
                    f"  Net à payer TTC: {net_a_payer_ttc:.2f} € (négatif)\n"
                    f"  Total TTC: {total_ttc:.2f} €\n"
                    f"  Acompte TTC déjà facturé: {acompte_ttc_deja_facture:.2f} €\n\n"
                    f"L'acompte TTC dépasse le total TTC. Vérifiez les montants."
                )
    
    return {
        'lignes_normalisees': lignes_normalisees,
        'total_ht_initial': total_ht_initial,
        'total_ht_final': total_ht_final,
        'tva_par_taux': tva_par_taux,
        'total_tva': total_tva,
        'total_ttc': total_ttc,
        'lignes_deja_remisees': lignes_deja_remisees,
        'acompte_ttc_deja_facture': acompte_ttc_deja_facture,
        'is_facture_acompte': is_facture_acompte,
        'devis_fige': devis_fige,  # Flag explicite : devis figé = source de vérité immuable
        'warnings': warnings  # Warnings de cohérence métier (pas d'erreur, juste information)
    }


def calculer_lignes_devis_fige_strict(data):
    """
    MODE "DEVIS FIGÉ" STRICT - Source unique de vérité absolue
    
    RÈGLE MAÎTRE : Un devis accepté devient une source comptable IMMUTABLE.
    La facture finale doit être une copie exacte du devis accepté.
    
    Si document_source == "devis_accepté" :
    - INTERDICTION de modifier les lignes
    - INTERDICTION de recalculer la TVA
    - INTERDICTION de fusionner ou normaliser
    - INTERDICTION de corriger unité / taux / description
    
    Cette fonction bypass complètement calculer_lignes_finales pour les devis figés.
    Elle utilise DIRECTEMENT les champs figés du devis sans aucun traitement.
    
    Toute logique de :
    - normalisation
    - fusion
    - recalcul TVA
    - redistribution
    est STRICTEMENT DÉSACTIVÉE dès qu'un devis est accepté.
    
    Retourne :
    - lignes_normalisees : lignes du devis utilisées telles quelles
    - total_ht_final : somme(ht_ligne) des lignes du devis
    - total_tva : somme(tva_ligne) des lignes du devis
    - total_ttc : total_ht + total_tva
    - tva_par_taux : dictionnaire {taux: montant_tva} calculé à partir des lignes
    - net_a_payer_ttc : total_ttc - acompte_ttc_deja_facture (si acompte)
    - immutable_source : True (flag indiquant que la source est immuable)
    """
    lignes_finales_devis = getattr(data, 'lignes_finales_devis', None)
    acompte_ttc_deja_facture = getattr(data, 'acompte_ttc_deja_facture', 0) or 0
    is_facture_acompte = getattr(data, 'is_facture_acompte', False)
    taux_acompte = getattr(data, 'taux_acompte', None)
    
    # Flag immutable_source = true
    immutable_source = True
    
    if not lignes_finales_devis or len(lignes_finales_devis) == 0:
        raise ValueError("ERREUR: calculer_lignes_devis_fige_strict appelé sans lignes_finales_devis")
    
    # Log : Mode devis figé activé
    print(f"🔒 MODE DEVIS FIGÉ STRICT ACTIVÉ - Source immuable (immutable_source={immutable_source})")
    print(f"   Nombre de lignes du devis: {len(lignes_finales_devis)}")
    print(f"   Toute modification est INTERDITE")
    
    # ============================================================
    # UTILISER DIRECTEMENT LES CHAMPS FIGÉS DU DEVIS
    # ============================================================
    # RÈGLE MAÎTRE : Si document_source == "devis_accepté"
    # - INTERDICTION de modifier les lignes
    # - INTERDICTION de recalculer la TVA
    # - INTERDICTION de fusionner ou normaliser
    # - INTERDICTION de corriger unité / taux / description
    
    # NE PAS appeler calculer_lignes_finales
    # NE PAS normaliser les descriptions
    # NE PAS fusionner les lignes
    # NE PAS recalculer les quantités
    # NE PAS recalculer les prix unitaires
    # NE PAS recalculer les taux de TVA
    # NE PAS recalculer les HT ligne (sauf pour facture d'acompte proportionnelle)
    
    lignes_normalisees = []
    tva_par_taux = {}
    
    for i, ligne_devis in enumerate(lignes_finales_devis):
        # Utiliser DIRECTEMENT les champs figés du devis
        # Les valeurs sont DÉFINITIVES et ne doivent JAMAIS être recalculées
        
        # Chaque ligne garde EXACTEMENT :
        # - description
        # - quantité
        # - unité
        # - PU HT (calculé à partir de ht_ligne / quantite)
        # - taux TVA
        # - HT ligne
        
        # Calculer tva_ligne à partir des champs figés
        # Note: Si le devis a déjà tva_ligne calculé, on peut l'utiliser
        # Sinon, on calcule: tva_ligne = ht_ligne × tva_taux / 100
        ht_ligne = ligne_devis.ht_apres_remise
        tva_taux = ligne_devis.tva_taux
        tva_ligne = ht_ligne * (tva_taux / 100)
        
        # Calculer prix_unitaire_ht à partir de ht_ligne et quantite
        prix_unitaire_ht = ht_ligne / ligne_devis.quantite if ligne_devis.quantite > 0 else 0
        
        # Cas facture d'acompte : appliquer le taux d'acompte proportionnellement
        # (C'est la SEULE exception autorisée : calcul proportionnel pour acompte)
        if is_facture_acompte and taux_acompte is not None and taux_acompte > 0:
            ht_ligne_original = ht_ligne
            ht_ligne = ht_ligne * (taux_acompte / 100)
            tva_ligne = ht_ligne * (tva_taux / 100)  # TVA recalculée proportionnellement
            prix_unitaire_ht = ht_ligne / ligne_devis.quantite if ligne_devis.quantite > 0 else 0
            print(f"   Ligne {i+1}: Acompte {taux_acompte}% appliqué (HT: {ht_ligne_original:.2f} → {ht_ligne:.2f})")
        
        lignes_normalisees.append({
            'description': ligne_devis.description,      # Description EXACTE (pas de normalisation)
            'quantite': ligne_devis.quantite,            # Quantité FIGÉE
            'unite': ligne_devis.unite,                   # Unité FIGÉE
            'prix_unitaire_ht': prix_unitaire_ht,        # Calculé à partir de ht_ligne / quantite
            'ht_initial': ligne_devis.ht_apres_remise,   # HT initial (avant acompte si facture d'acompte)
            'ht_final': ht_ligne,                        # HT final (FIGÉ ou proportionnel si acompte)
            'tva_taux': tva_taux,                        # TVA FIGÉE (ne jamais modifier)
            'tva_ligne': tva_ligne,                      # TVA ligne (calculée à partir des champs figés)
            'deja_remise': True,
            'devis_fige': True
        })
        
        # Grouper TVA par taux
        tva_par_taux[tva_taux] = tva_par_taux.get(tva_taux, 0) + tva_ligne
    
    # ============================================================
    # CALCUL DES TOTAUX (seule chose autorisée)
    # ============================================================
    # Les totaux sont calculés UNIQUEMENT comme :
    # total_ht = somme(ht_lignes)
    # total_tva = somme(tva_lignes)
    # total_ttc = total_ht + total_tva
    # Aucune autre logique n'est autorisée
    
    total_ht_final = sum(ligne['ht_final'] for ligne in lignes_normalisees)
    total_tva = sum(ligne['tva_ligne'] for ligne in lignes_normalisees)
    total_ttc = total_ht_final + total_tva
    
    print(f"   Totaux calculés (somme des lignes uniquement):")
    print(f"     Total HT: {total_ht_final:.2f} €")
    print(f"     Total TVA: {total_tva:.2f} €")
    print(f"     Total TTC: {total_ttc:.2f} €")
    
    # ============================================================
    # CAS FACTURE FINALE AVEC ACOMPTE
    # ============================================================
    # RÈGLE FISCALE ABSOLUE : Si plusieurs taux de TVA, l'acompte DOIT être ventilé
    # proportionnellement par taux AVANT toute déduction.
    # Il est INTERDIT de soustraire un acompte TTC global sur un panier multi-TVA.
    
    # Initialiser les variables pour le cas multi-TVA avec acompte
    total_ht_restant = total_ht_final
    total_tva_restante = total_tva
    
    if not is_facture_acompte and acompte_ttc_deja_facture > 0:
        nombre_taux_tva = len(tva_par_taux)
        
        if nombre_taux_tva > 1:
            # ============================================================
            # VENTILATION PROPORTIONNELLE PAR TAUX DE TVA (OBLIGATOIRE)
            # ============================================================
            print(f"🔧 VENTILATION ACOMPTE MULTI-TVA ({nombre_taux_tva} taux détectés)")
            print(f"   Acompte TTC à ventiler: {acompte_ttc_deja_facture:.2f} €")
            
            # 1) Calculer la base HT par taux
            base_ht_par_taux = {}
            for ligne in lignes_normalisees:
                tva_taux = ligne['tva_taux']
                if tva_taux not in base_ht_par_taux:
                    base_ht_par_taux[tva_taux] = 0
                base_ht_par_taux[tva_taux] += ligne['ht_final']
            
            total_ht_base = sum(base_ht_par_taux.values())
            print(f"   Base HT totale: {total_ht_base:.2f} €")
            for taux, base_ht in base_ht_par_taux.items():
                print(f"     - Taux {taux}%: {base_ht:.2f} €")
            
            # 2) Convertir l'acompte TTC en HT (approximation : utiliser le taux moyen pondéré)
            # Calculer le taux moyen pondéré de TVA
            taux_moyen_pondere = total_tva / total_ht_final if total_ht_final > 0 else 0
            acompte_ht_total = acompte_ttc_deja_facture / (1 + taux_moyen_pondere / 100) if taux_moyen_pondere > 0 else acompte_ttc_deja_facture
            print(f"   Taux moyen pondéré: {taux_moyen_pondere:.2f}%")
            print(f"   Acompte HT (approximatif): {acompte_ht_total:.2f} €")
            
            # 3) Ventiler l'acompte HT proportionnellement par taux
            acompte_ht_par_taux = {}
            acompte_tva_par_taux = {}
            for taux, base_ht_taux in base_ht_par_taux.items():
                if total_ht_base > 0:
                    proportion = base_ht_taux / total_ht_base
                    acompte_ht_taux = acompte_ht_total * proportion
                    acompte_tva_taux = acompte_ht_taux * (taux / 100)
                    acompte_ht_par_taux[taux] = acompte_ht_taux
                    acompte_tva_par_taux[taux] = acompte_tva_taux
                    print(f"     Taux {taux}%: proportion {proportion:.4f}, acompte HT {acompte_ht_taux:.2f} €, TVA {acompte_tva_taux:.2f} €")
            
            # 4) Calculer les montants restants par taux
            ht_restant_par_taux = {}
            tva_restante_par_taux = {}
            for taux in base_ht_par_taux.keys():
                ht_restant_par_taux[taux] = base_ht_par_taux[taux] - acompte_ht_par_taux.get(taux, 0)
                tva_restante_par_taux[taux] = (base_ht_par_taux[taux] * taux / 100) - acompte_tva_par_taux.get(taux, 0)
            
            # 5) Recalculer les totaux finaux
            total_ht_restant = sum(ht_restant_par_taux.values())
            total_tva_restante = sum(tva_restante_par_taux.values())
            net_a_payer_ttc = total_ht_restant + total_tva_restante
            
            # Mettre à jour les totaux pour l'affichage
            total_ht_final = total_ht_restant
            total_tva = total_tva_restante
            tva_par_taux = tva_restante_par_taux
            
            print(f"   ✅ Totaux après ventilation:")
            print(f"     Total HT restant: {total_ht_restant:.2f} €")
            print(f"     Total TVA restante: {total_tva_restante:.2f} €")
            print(f"     Net à payer TTC: {net_a_payer_ttc:.2f} €")
            
            if net_a_payer_ttc < 0:
                raise ValueError(
                    f"ERREUR VALIDATION ACOMPTE: "
                    f"Net à payer TTC ({net_a_payer_ttc:.2f} €) < 0 après ventilation. "
                    f"L'acompte TTC ({acompte_ttc_deja_facture:.2f} €) dépasse le total TTC ({total_ttc:.2f} €)."
                )
        else:
            # ============================================================
            # CAS UN SEUL TAUX : Comportement actuel autorisé
            # ============================================================
            net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
            print(f"   Acompte TTC déjà facturé (mono-TVA): {acompte_ttc_deja_facture:.2f} €")
            print(f"   Net à payer TTC: {net_a_payer_ttc:.2f} € (Total TTC - Acompte TTC)")
            if net_a_payer_ttc < 0:
                raise ValueError(
                    f"ERREUR VALIDATION ACOMPTE: "
                    f"Net à payer TTC ({net_a_payer_ttc:.2f} €) < 0. "
                    f"L'acompte TTC ({acompte_ttc_deja_facture:.2f} €) dépasse le total TTC ({total_ttc:.2f} €)."
                )
    else:
        net_a_payer_ttc = total_ttc
    
    # ============================================================
    # PROTECTION DURE : Vérifier que les lignes correspondent au devis
    # ============================================================
    # assert lignes_facture == lignes_devis (structure et valeurs)
    # sinon lever une erreur bloquante
    # Logs si tentative de modification bloquée
    
    assert len(lignes_normalisees) == len(lignes_finales_devis), \
        f"❌ ERREUR CRITIQUE: Nombre de lignes différent ({len(lignes_normalisees)} vs {len(lignes_finales_devis)})"
    
    for i, (ligne_facture, ligne_devis) in enumerate(zip(lignes_normalisees, lignes_finales_devis)):
        # Vérifier description
        if ligne_facture['description'] != ligne_devis.description:
            print(f"❌ TENTATIVE DE MODIFICATION BLOQUÉE ligne {i+1}: Description modifiée")
            print(f"   Devis: '{ligne_devis.description}'")
            print(f"   Facture: '{ligne_facture['description']}'")
            raise ValueError(
                f"ERREUR CRITIQUE ligne {i+1}: Description modifiée. "
                f"Un devis accepté est IMMUTABLE. Toute modification est INTERDITE."
            )
        
        # Vérifier quantité
        if ligne_facture['quantite'] != ligne_devis.quantite:
            print(f"❌ TENTATIVE DE MODIFICATION BLOQUÉE ligne {i+1}: Quantité modifiée")
            print(f"   Devis: {ligne_devis.quantite}")
            print(f"   Facture: {ligne_facture['quantite']}")
            raise ValueError(
                f"ERREUR CRITIQUE ligne {i+1}: Quantité modifiée. "
                f"Un devis accepté est IMMUTABLE. Toute modification est INTERDITE."
            )
        
        # Vérifier unité
        if ligne_facture['unite'] != ligne_devis.unite:
            print(f"❌ TENTATIVE DE MODIFICATION BLOQUÉE ligne {i+1}: Unité modifiée")
            print(f"   Devis: '{ligne_devis.unite}'")
            print(f"   Facture: '{ligne_facture['unite']}'")
            raise ValueError(
                f"ERREUR CRITIQUE ligne {i+1}: Unité modifiée. "
                f"Un devis accepté est IMMUTABLE. Toute modification est INTERDITE."
            )
        
        # Vérifier HT (tolérance pour facture d'acompte)
        if not (is_facture_acompte and taux_acompte):
            if abs(ligne_facture['ht_final'] - ligne_devis.ht_apres_remise) >= 0.01:
                print(f"❌ TENTATIVE DE MODIFICATION BLOQUÉE ligne {i+1}: HT modifié")
                print(f"   Devis: {ligne_devis.ht_apres_remise:.2f} €")
                print(f"   Facture: {ligne_facture['ht_final']:.2f} €")
                raise ValueError(
                    f"ERREUR CRITIQUE ligne {i+1}: HT modifié. "
                    f"Un devis accepté est IMMUTABLE. Toute modification est INTERDITE."
                )
        
        # Vérifier TVA
        if abs(ligne_facture['tva_taux'] - ligne_devis.tva_taux) >= 0.01:
            print(f"❌ TENTATIVE DE MODIFICATION BLOQUÉE ligne {i+1}: TVA modifiée")
            print(f"   Devis: {ligne_devis.tva_taux:.2f}%")
            print(f"   Facture: {ligne_facture['tva_taux']:.2f}%")
            raise ValueError(
                f"ERREUR CRITIQUE ligne {i+1}: TVA modifiée. "
                f"Un devis accepté est IMMUTABLE. Toute modification est INTERDITE."
            )
    
    print(f"✅ Validation OK: Toutes les lignes correspondent exactement au devis accepté")
    
    # OBJECTIF FINAL : Même devis accepté → même facture → mêmes totaux → toujours.
    print(f"✅ MODE DEVIS FIGÉ STRICT TERMINÉ - Source immuable respectée")
    
    # Dans le cas multi-TVA avec acompte, utiliser les totaux après ventilation
    # Sinon, utiliser les totaux initiaux
    if not is_facture_acompte and acompte_ttc_deja_facture > 0 and len(tva_par_taux) > 1:
        # Les totaux ont été recalculés dans la section ventilation
        # Utiliser total_ht_restant et total_tva_restante
        total_ht_final_affichage = total_ht_restant
        total_tva_affichage = total_tva_restante
    else:
        # Utiliser les totaux initiaux
        total_ht_final_affichage = total_ht_final
        total_tva_affichage = total_tva
    
    return {
        'lignes_normalisees': lignes_normalisees,
        'total_ht_initial': total_ht_final,  # Pour devis figé, ht_initial = ht_final (déjà remisé)
        'total_ht_final': total_ht_final_affichage,  # Après ventilation si multi-TVA avec acompte
        'tva_par_taux': tva_par_taux,  # Déjà mis à jour avec les TVA restantes en cas de ventilation
        'total_tva': total_tva_affichage,  # Après ventilation si multi-TVA avec acompte
        'total_ttc': total_ttc,  # Total TTC initial (avant déduction acompte)
        'net_a_payer_ttc': net_a_payer_ttc,  # Net à payer après déduction acompte
        'lignes_deja_remisees': True,
        'acompte_ttc_deja_facture': acompte_ttc_deja_facture,
        'is_facture_acompte': is_facture_acompte,
        'devis_fige': True,
        'immutable_source': immutable_source,  # Flag indiquant que la source est immuable
        'warnings': []  # Pas de warnings pour devis figé
    }


def dessiner_tableau_prestations(c, width, data, y_table, tva_taux_global):
    """
    Dessine le tableau des prestations pour une facture avec totaux - TVA par ligne
    
    MODE "DEVIS FIGÉ" STRICT :
    Si devis_fige == True, utilise calculer_lignes_devis_fige_strict au lieu de calculer_lignes_finales.
    """
    # Détecter si c'est un devis figé
    lignes_finales_devis = getattr(data, 'lignes_finales_devis', None)
    devis_fige = (lignes_finales_devis and len(lignes_finales_devis) > 0)
    
    # Log pour diagnostic
    if devis_fige:
        print(f"🔒 MODE DEVIS FIGÉ DÉTECTÉ dans dessiner_tableau_prestations")
        print(f"   Nombre de lignes_finales_devis: {len(lignes_finales_devis)}")
        print(f"   TVA par ligne:", [f"{l.tva_taux}%" for l in lignes_finales_devis])
    else:
        print(f"⚠️ MODE NORMAL - lignes_finales_devis non présent ou vide")
        print(f"   lignes_finales_devis: {lignes_finales_devis}")
    
    # ============================================================
    # BRANCHE EXPLICITE : MODE "DEVIS FIGÉ" STRICT
    # ============================================================
    if devis_fige:
        # NE PAS appeler calculer_lignes_finales
        # Utiliser directement calculer_lignes_devis_fige_strict
        print(f"✅ Utilisation de calculer_lignes_devis_fige_strict (bypass calculer_lignes_finales)")
        resultats = calculer_lignes_devis_fige_strict(data)
    else:
        # Cas normal : utiliser calculer_lignes_finales
        print(f"⚠️ Utilisation de calculer_lignes_finales (mode normal)")
        resultats = calculer_lignes_finales(data, tva_taux_global)
    
    lignes_normalisees = resultats['lignes_normalisees']
    total_ht_initial = resultats['total_ht_initial']
    total_ht_final = resultats['total_ht_final']
    tva_par_taux = resultats['tva_par_taux']  # Déjà mis à jour avec TVA restantes si ventilation
    total_tva = resultats['total_tva']  # Déjà mis à jour avec TVA restante si ventilation
    total_ttc = resultats['total_ttc']  # Total TTC initial (avant déduction acompte)
    net_a_payer_ttc = resultats.get('net_a_payer_ttc', total_ttc)  # Utiliser le net calculé (avec ventilation si multi-TVA)
    lignes_deja_remisees = resultats['lignes_deja_remisees']
    acompte_ttc_deja_facture = resultats['acompte_ttc_deja_facture']
    is_facture_acompte = resultats['is_facture_acompte']
    devis_fige = resultats['devis_fige']  # Flag explicite : devis figé = contractuel
    
    # NOTE : net_a_payer_ttc est déjà calculé correctement dans calculer_lignes_devis_fige_strict
    # avec ventilation proportionnelle si multi-TVA, donc on l'utilise directement
    
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
    
    # Afficher les lignes
    for i, ligne in enumerate(lignes_normalisees):
        y_ligne -= 10*mm
        
        # Alterner couleurs
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(ligne['description'], 50))
        c.drawString(90*mm, y_ligne + 2*mm, str(ligne['quantite']))
        c.drawString(105*mm, y_ligne + 2*mm, ligne['unite'])
        
        # Prix unitaire affiché (calculé depuis ht_final)
        prix_unitaire = ligne['ht_final'] / ligne['quantite'] if ligne['quantite'] > 0 else 0
        c.drawString(125*mm, y_ligne + 2*mm, f"{prix_unitaire:.2f} €")
        c.drawString(150*mm, y_ligne + 2*mm, f"{ligne['tva_taux']:.1f}%")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{ligne['ht_final']:.2f} €")
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    y_totaux = y_ligne - 10*mm
    
    # Afficher les totaux (miroir des calculs)
    # RÈGLE ABSOLUE : Les lignes affichées sont DÉJÀ remisées
    # → AUCUNE remise globale à afficher (incompatible avec multi-TVA)
    # → Afficher UNIQUEMENT : Total HT, TVA par taux, Total TTC
    # → Pour devis figé : TVA issue des lignes, pas recalculée
    x_label = 130*mm
    x_value = width - 18*mm
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    
    y_offset = 0
    
    # Afficher UNIQUEMENT : Total HT (somme des lignes déjà remisées)
    # Pour devis figé : total_ht = somme(ht_ligne_final) des lignes du devis
    c.drawString(x_label, y_totaux, "Total HT")
    c.drawRightString(x_value, y_totaux, f"{total_ht_final:.2f} €")
    y_offset = 6*mm
    
    # Afficher TVA par taux
    # Pour devis figé : TVA issue des lignes (tva_ligne = ht_ligne_final × tva_rate)
    # → Aucune redistribution, aucun recalcul global
    for taux in sorted(tva_par_taux.keys()):
        montant = tva_par_taux[taux]
        if taux > 0:
            c.drawString(x_label, y_totaux - y_offset, f"TVA ({taux}%)")
            c.drawRightString(x_value, y_totaux - y_offset, f"{montant:.2f} €")
            y_offset += 6*mm
        elif len(tva_par_taux) == 1:
            c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
            y_offset += 6*mm
    
    # Total TTC
    # Pour devis figé : total_ttc = total_ht + total_tva (somme des lignes uniquement)
    # En cas de ventilation multi-TVA avec acompte, total_ht_final et total_tva sont déjà après ventilation
    # donc total_ht_final + total_tva = net_a_payer_ttc
    # On calcule le total TTC à afficher : si ventilation, c'est le net, sinon c'est le total initial
    total_ttc_a_afficher = total_ht_final + total_tva  # Toujours cohérent (après ventilation si applicable)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x_label, y_totaux - y_offset, "TOTAL TTC")
    c.drawRightString(x_value, y_totaux - y_offset, f"{total_ttc_a_afficher:.2f} €")
    y_offset += 6*mm
    
    # Facture finale issue d'un devis figé : acompte et net à payer
    # RÈGLE ABSOLUE : NE PAS recalculer la TVA, NE PAS déduire d'HT
    # → Calculer uniquement : net_a_payer_ttc = total_ttc - acompte_ttc_deja_facture
    if not is_facture_acompte and acompte_ttc_deja_facture > 0:
        # Ligne de séparation visuelle avant l'acompte
        y_offset += 3*mm
        c.setStrokeColor(HexColor('#e0e0e0'))
        c.setLineWidth(0.5)
        c.line(x_label - 5*mm, y_totaux - y_offset, x_value + 5*mm, y_totaux - y_offset)
        y_offset += 4*mm
        
        # Libellé de l'acompte (sans référence dans le libellé principal)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(GRIS_FONCE)
        c.drawString(x_label, y_totaux - y_offset, "Acompte déjà facturé")
        
        # Montant de l'acompte en rouge, gras et plus grand pour visibilité maximale
        c.setFillColor(HexColor('#e74c3c'))
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(x_value, y_totaux - y_offset, f"- {acompte_ttc_deja_facture:.2f} €")
        y_offset += 5*mm
        
        # Référence(s) de l'acompte sur une ligne séparée en plus petit
        acompte_references = getattr(data, 'acompte_references', None)
        if acompte_references and len(acompte_references) > 0:
            references_str = ', '.join(acompte_references)
            c.setFont("Helvetica", 8)
            c.setFillColor(HexColor('#666666'))
            c.drawString(x_label, y_totaux - y_offset, f"Référence(s): {references_str}")
            y_offset += 4*mm
        else:
            y_offset += 2*mm
        
        c.setFillColor(GRIS_FONCE)
        
        # Encadré pour "NET À PAYER TTC"
        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(get_couleur_principale(data))
        c.roundRect(x_label - 5*mm, y_totaux - y_offset - 8*mm, 68*mm, 10*mm, 2*mm, fill=True, stroke=False)
        c.setFillColor(white)
        c.drawString(x_label, y_totaux - y_offset - 5*mm, "NET À PAYER TTC")
        c.drawRightString(x_value, y_totaux - y_offset - 5*mm, f"{net_a_payer_ttc:.2f} €")
        y_offset += 6*mm
    
    return y_totaux - y_offset - 5*mm, total_ht_final, net_a_payer_ttc if not is_facture_acompte and acompte_ttc_deja_facture > 0 else total_ttc


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
    
    # Détecter si c'est un devis figé
    lignes_finales_devis = getattr(data, 'lignes_finales_devis', None)
    devis_fige = (lignes_finales_devis and len(lignes_finales_devis) > 0)
    
    # Log pour diagnostic
    if devis_fige:
        print(f"🔒 MODE DEVIS FIGÉ DÉTECTÉ dans generer_pdf_devis")
        print(f"   Nombre de lignes_finales_devis: {len(lignes_finales_devis)}")
        print(f"   TVA par ligne:", [f"{l.tva_taux}%" for l in lignes_finales_devis])
    else:
        print(f"⚠️ MODE NORMAL dans generer_pdf_devis - lignes_finales_devis non présent")
    
    # ============================================================
    # BRANCHE EXPLICITE : MODE "DEVIS FIGÉ" STRICT
    # ============================================================
    if devis_fige:
        # NE PAS appeler calculer_lignes_finales
        # Utiliser directement calculer_lignes_devis_fige_strict
        print(f"✅ Utilisation de calculer_lignes_devis_fige_strict (bypass calculer_lignes_finales)")
        resultats = calculer_lignes_devis_fige_strict(data)
    else:
        # Cas normal : utiliser calculer_lignes_finales
        print(f"⚠️ Utilisation de calculer_lignes_finales (mode normal)")
        tva_taux_global = getattr(data, 'tva_taux', 20.0)
        resultats = calculer_lignes_finales(data, tva_taux_global)
    
    lignes_normalisees = resultats['lignes_normalisees']
    total_ht_initial = resultats['total_ht_initial']
    total_ht_final = resultats['total_ht_final']
    tva_par_taux = resultats['tva_par_taux']
    total_tva = resultats['total_tva']
    total_ttc = resultats['total_ttc']
    lignes_deja_remisees = resultats['lignes_deja_remisees']
    
    # Calculer remise totale pour affichage
    remise_totale = total_ht_initial - total_ht_final if not lignes_deja_remisees else 0
    
    # Pagination : diviser les lignes normalisées en groupes
    lignes_par_page = 11  # Nombre de lignes par page
    groupes_lignes = []
    for i in range(0, len(lignes_normalisees), lignes_par_page):
        groupes_lignes.append(lignes_normalisees[i:i + lignes_par_page])
    
    # Si aucune ligne, créer au moins une page vide
    if not groupes_lignes:
        groupes_lignes = [[]]
    
    mention_tva = ""
    if data.tva_taux == 0:
        mention_tva = "TVA non applicable, article 293 B du Code général des impôts"
    
    # Dessiner chaque groupe de lignes
    for page_num, groupe_lignes in enumerate(groupes_lignes):
        est_premiere_page = (page_num == 0)
        est_derniere_page = (page_num == len(groupes_lignes) - 1)
        
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
        
        # Dessiner les lignes de prestations (utiliser les lignes normalisées)
        index_debut = page_num * lignes_par_page
        groupe_lignes_page = groupe_lignes
        y_totaux_tableau = dessiner_lignes_normalisees(c, width, groupe_lignes_page, y_table, data, index_debut)
        
        # Si dernière page, dessiner les totaux, signature et conditions
        if est_derniere_page:
            y_totaux = y_totaux_tableau
            
            # Dessiner les totaux (utiliser les résultats de calculer_lignes_finales)
            y_fin_totaux = dessiner_totaux_devis(c, width, y_totaux, total_ht_initial, total_ht_final, remise_totale, tva_par_taux, total_ttc, data, lignes_deja_remisees)
            
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