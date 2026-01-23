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
    tva_taux: float = 20.0
    conditions_paiement: str = "30% à la commande, solde à réception"
    delai_realisation: str = "À définir"
    validite_jours: int = 30
    remise_type: Optional[str] = None  # "pourcentage" ou "fixe"
    remise_valeur: Optional[float] = 0
    numero_devis: Optional[str] = None  # Numéro de devis fourni par le client (OBLIGATOIRE)

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
    numero_facture: Optional[str] = None  # Numéro de facture fourni par le frontend
    date_echeance_jours: int = 30
    mention_legale_tva: Optional[str] = ""
    rib: Optional[RIB] = None
    remise_type: Optional[str] = None  # "pourcentage" ou "montant"
    remise_valeur: Optional[float] = 0
    statut: Optional[str] = "en_attente"  # "en_attente", "payee", etc.
    total_ht: Optional[float] = None  # Total HT pour factures d'acompte
    total_ttc: Optional[float] = None  # Total TTC pour factures d'acompte
    is_facture_acompte: Optional[bool] = None  # Flag pour factures d'acompte (None par défaut pour détecter si la valeur est envoyée)
    taux_acompte: Optional[float] = None  # Taux d'acompte en pourcentage


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
    print(f"🔍 dessiner_en_tete_page - numero_devis reçu: '{numero_devis}'")
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
    """
    Dessine les totaux pour un devis avec affichage de la remise si présente
    """
    x_label = 130*mm
    x_value = width - 18*mm
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    
    y_offset = 0
    
    # Récupérer les informations de remise depuis data
    remise_type = getattr(data, 'remise_type', None)
    remise_valeur_raw = getattr(data, 'remise_valeur', None)
    
    # Convertir remise_valeur en nombre
    remise_valeur = 0
    if remise_valeur_raw is not None:
        try:
            remise_valeur = float(remise_valeur_raw)
        except (ValueError, TypeError):
            remise_valeur = 0
    
    # Normaliser remise_type
    if remise_type:
        remise_type = str(remise_type).strip()
        if remise_type == "" or remise_type.lower() == "none":
            remise_type = None
    
    # Calculer la remise totale à partir de data si nécessaire
    remise_totale = remise
    if remise_totale == 0 and remise_type and remise_valeur > 0:
        if remise_type == "pourcentage":
            remise_totale = total_ht_avant_acompte * (remise_valeur / 100)
        elif remise_type in ["montant", "fixe"]:
            remise_totale = remise_valeur
    
    # Total HT (avant remise si remise présente)
    if remise_totale > 0:
        c.drawString(x_label, y_totaux, "Total HT avant remise")
    else:
        c.drawString(x_label, y_totaux, "Total HT")
    c.drawRightString(x_value, y_totaux, f"{total_ht_avant_acompte:.2f} €")
    y_offset = 6*mm
    
    # Afficher la remise si elle existe
    if remise_totale > 0:
        if remise_type == "pourcentage" and remise_valeur > 0:
            c.drawString(x_label, y_totaux - y_offset, f"Remise ({remise_valeur}%)")
        else:
            c.drawString(x_label, y_totaux - y_offset, "Remise")
        
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{remise_totale:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
        
        # Total HT après remise
        c.drawString(x_label, y_totaux - y_offset, "Total HT après remise")
        c.drawRightString(x_value, y_totaux - y_offset, f"{total_ht_final:.2f} €")
        y_offset += 6*mm
    
    # Afficher l'acompte si présent
    if total_acompte > 0:
        c.drawString(x_label, y_totaux - y_offset, "Acompte déduit")
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{total_acompte:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
    
    # Calculer TVA par taux à partir des prestations
    tva_par_taux = {}
    for prestation in data.prestations:
        total_ligne = prestation.quantite * prestation.prix_unitaire
        if total_ligne > 0:  # Ignorer les acomptes
            # Utiliser le taux de TVA de la prestation si disponible, sinon le taux global
            taux = getattr(prestation, 'tva_taux', None) or tva_taux
            if taux not in tva_par_taux:
                tva_par_taux[taux] = 0
            # Appliquer la remise proportionnellement si nécessaire
            if remise_totale > 0:
                ratio_remise = (total_ht_avant_acompte - remise_totale) / total_ht_avant_acompte if total_ht_avant_acompte > 0 else 1
                total_ligne_apres_remise = total_ligne * ratio_remise
            else:
                total_ligne_apres_remise = total_ligne
            # Déduire l'acompte si présent
            total_ligne_final = total_ligne_apres_remise
            tva_par_taux[taux] += total_ligne_final * (taux / 100)
    
    # Si pas de prestations avec TVA, utiliser le calcul simple
    if not tva_par_taux:
        montant_tva_total = total_ht_final * (tva_taux / 100)
        if tva_taux > 0:
            tva_par_taux[tva_taux] = montant_tva_total
        else:
            tva_par_taux[0] = 0
    
    # Afficher TVA par taux
    for taux in sorted(tva_par_taux.keys(), reverse=True):
        montant = tva_par_taux[taux]
        if taux > 0:
            c.drawString(x_label, y_totaux - y_offset, f"TVA ({taux}%)")
            c.drawRightString(x_value, y_totaux - y_offset, f"{montant:.2f} €")
            y_offset += 6*mm
        elif len(tva_par_taux) == 1:
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
            c.setFont("Helvetica", 10)
            y_offset += 6*mm
    
    # Total TTC avec encadré coloré
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
    c.drawString(105*mm, y_table + 3*mm, "Qté")
    c.drawString(120*mm, y_table + 3*mm, "Unité")
    c.drawString(142*mm, y_table + 3*mm, "P.U. HT")
    c.drawRightString(width - 18*mm, y_table + 3*mm, "Total HT")
    
    y_ligne = y_table - 2*mm
    total_ht_avant_acompte = 0
    total_acompte = 0
    
    # Dessiner les lignes
    for i, prestation in enumerate(prestations):
        y_ligne -= 10*mm
        total_ligne = prestation.quantite * prestation.prix_unitaire
        
        # Séparer les prestations positives et les acomptes (négatifs)
        if total_ligne >= 0:
            total_ht_avant_acompte += total_ligne
        else:
            total_acompte += abs(total_ligne)
        
        # Alterner les couleurs de fond
        hauteur_ligne = 10*mm
        
        # Gérer descriptions enrichies
        description_principale = prestation.description if hasattr(prestation, 'description') else ''
        description_detaillee = getattr(prestation, 'description_detaillee', '') or ''
        notes = getattr(prestation, 'notes', '') or ''
        
        if description_detaillee or notes:
            lignes_detail = []
            if description_detaillee:
                mots_detail = description_detaillee.split()
                ligne_courante = ""
                for mot in mots_detail:
                    if len(ligne_courante + " " + mot) <= 45:
                        ligne_courante = (ligne_courante + " " + mot).strip()
                    else:
                        if ligne_courante:
                            lignes_detail.append(ligne_courante)
                        ligne_courante = mot
                if ligne_courante:
                    lignes_detail.append(ligne_courante)
            
            nb_lignes_detail = len(lignes_detail)
            if notes:
                nb_lignes_detail += 1
            
            if nb_lignes_detail > 0:
                hauteur_ligne = (10 + (nb_lignes_detail * 4)) * mm
        
        if (index_debut + i) % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - hauteur_ligne + 10*mm, width - 30*mm, hauteur_ligne, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        
        # Description principale
        y_text = y_ligne + 2*mm
        c.drawString(18*mm, y_text, tronquer_texte(description_principale, 50))
        
        # Description détaillée et notes
        y_detail = y_ligne - 3*mm
        if description_detaillee:
            c.setFont("Helvetica", 7)
            c.setFillColor(HexColor('#666666'))
            for ligne_detail in lignes_detail[:3]:
                c.drawString(18*mm, y_detail, tronquer_texte(ligne_detail, 60))
                y_detail -= 3.5*mm
        
        if notes:
            c.setFont("Helvetica-Oblique", 7)
            c.setFillColor(HexColor('#888888'))
            notes_texte = f"Note: {tronquer_texte(notes, 55)}"
            c.drawString(18*mm, y_detail, notes_texte)
            y_detail -= 3.5*mm
        
        # Colonnes standard
        c.setFont("Helvetica", 9)
        c.setFillColor(GRIS_FONCE)
        c.drawString(107*mm, y_ligne + 2*mm, str(prestation.quantite))
        c.drawString(120*mm, y_ligne + 2*mm, prestation.unite if hasattr(prestation, 'unite') else 'u')
        c.drawString(142*mm, y_ligne + 2*mm, f"{prestation.prix_unitaire:.2f} €")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne:.2f} €")
        
        y_ligne -= hauteur_ligne
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    return y_ligne - 10*mm, total_ht_avant_acompte, total_acompte


def dessiner_tableau_prestations(c, width, data, y_table, tva_taux):
    """Dessine le tableau des prestations pour une facture avec totaux"""
    # ============================================================
    # CAS SPÉCIAL : FACTURE D'ACOMPTE AVEC TOTAL_TTC FOURNI
    # ============================================================
    # Si c'est une facture d'acompte ET que total_ttc est fourni,
    # utiliser directement ces valeurs au lieu de recalculer
    
    # Essayer plusieurs méthodes pour récupérer les valeurs (Pydantic peut stocker différemment)
    is_facture_acompte = False
    total_ttc_fourni = None
    total_ht_fourni = None
    
    # Méthode 1: Attribut direct
    if hasattr(data, 'is_facture_acompte'):
        is_facture_acompte = bool(data.is_facture_acompte) if data.is_facture_acompte is not None else False
    if hasattr(data, 'total_ttc'):
        total_ttc_fourni = data.total_ttc
    if hasattr(data, 'total_ht'):
        total_ht_fourni = data.total_ht
    
    # Méthode 2: getattr (fallback)
    if is_facture_acompte is False:
        is_facture_acompte = getattr(data, 'is_facture_acompte', False)
    if total_ttc_fourni is None:
        total_ttc_fourni = getattr(data, 'total_ttc', None)
    if total_ht_fourni is None:
        total_ht_fourni = getattr(data, 'total_ht', None)
    
    # Initialiser total_ttc pour qu'il soit toujours défini
    total_ttc = 0.0
    total_ht_final = 0.0
    montant_tva = 0.0
    
    print(f"🔍 DEBUG dessiner_tableau_prestations - is_facture_acompte: {is_facture_acompte}, total_ttc_fourni: {total_ttc_fourni} (type: {type(total_ttc_fourni)}), total_ht_fourni: {total_ht_fourni} (type: {type(total_ht_fourni)})")
    print(f"   data.total_ttc direct (hasattr): {hasattr(data, 'total_ttc')}, valeur: {data.total_ttc if hasattr(data, 'total_ttc') else 'N/A'}")
    print(f"   data.total_ht direct (hasattr): {hasattr(data, 'total_ht')}, valeur: {data.total_ht if hasattr(data, 'total_ht') else 'N/A'}")
    print(f"   data.is_facture_acompte direct (hasattr): {hasattr(data, 'is_facture_acompte')}, valeur: {data.is_facture_acompte if hasattr(data, 'is_facture_acompte') else 'N/A'}")
    
    # Essayer aussi model_dump si disponible (Pydantic v2)
    if hasattr(data, 'model_dump'):
        try:
            data_dict = data.model_dump()
            print(f"   model_dump - total_ttc: {data_dict.get('total_ttc', 'NON TROUVÉ')}")
            print(f"   model_dump - total_ht: {data_dict.get('total_ht', 'NON TROUVÉ')}")
            print(f"   model_dump - is_facture_acompte: {data_dict.get('is_facture_acompte', 'NON TROUVÉ')}")
            # Utiliser les valeurs de model_dump si elles sont None
            if total_ttc_fourni is None:
                total_ttc_fourni = data_dict.get('total_ttc')
            if total_ht_fourni is None:
                total_ht_fourni = data_dict.get('total_ht')
            if not is_facture_acompte:
                is_facture_acompte = bool(data_dict.get('is_facture_acompte', False))
        except Exception as e:
            print(f"   ⚠️ Erreur model_dump: {e}")
    
    if is_facture_acompte and total_ttc_fourni is not None:
        # Convertir en float si nécessaire (peut être string, int, ou float)
        try:
            if isinstance(total_ttc_fourni, str):
                total_ttc = float(total_ttc_fourni.replace(',', '.'))
            else:
                total_ttc = float(total_ttc_fourni)
        except (ValueError, TypeError) as e:
            print(f"❌ ERREUR conversion total_ttc_fourni: {e}, valeur: {total_ttc_fourni}")
            total_ttc = 0.0
        
        print(f"✅ FACTURE D'ACOMPTE - Utilisation de total_ttc fourni: {total_ttc_fourni} -> {total_ttc}")
        print(f"   total_ht fourni: {total_ht_fourni}")
        print(f"   tva_taux: {tva_taux}")
        
        if total_ht_fourni is not None:
            try:
                if isinstance(total_ht_fourni, str):
                    total_ht_final = float(total_ht_fourni.replace(',', '.'))
                else:
                    total_ht_final = float(total_ht_fourni)
            except (ValueError, TypeError) as e:
                print(f"❌ ERREUR conversion total_ht_fourni: {e}, valeur: {total_ht_fourni}")
                total_ht_final = total_ttc if tva_taux == 0 else total_ttc / (1 + tva_taux / 100)
            montant_tva = total_ttc - total_ht_final
            print(f"   Utilisation total_ht fourni: HT={total_ht_final}, TVA={montant_tva}, TTC={total_ttc}")
        elif tva_taux == 0:
            # Si TVA = 0, HT = TTC (utiliser total_ttc fourni directement)
            total_ht_final = total_ttc
            montant_tva = 0
            print(f"   TVA = 0, donc HT = TTC: HT={total_ht_final}, TTC={total_ttc}")
        else:
            total_ht_final = total_ttc / (1 + tva_taux / 100)
            montant_tva = total_ttc - total_ht_final
            print(f"   Calcul HT à partir de TTC: HT={total_ht_final}, TVA={montant_tva}, TTC={total_ttc}")
        
        # Pour l'affichage dans le tableau, utiliser total_ht_final
        total_ht_avant_acompte = total_ht_final
        total_acompte = 0
        remise = 0
        total_ht_apres_remise = total_ht_final
        
        print(f"✅ Totaux facture d'acompte - HT: {total_ht_final:.2f}, TVA: {montant_tva:.2f}, TTC: {total_ttc:.2f}")
    else:
        # Calcul normal : initialiser les variables
        total_ht_avant_acompte = 0
        total_acompte = 0
        # Initialiser total_ttc pour éviter les erreurs (sera recalculé plus tard)
        total_ttc = 0.0
        total_ht_final = 0.0
        montant_tva = 0.0
    
    # En-tête du tableau
    c.setFillColor(get_couleur_principale(data))
    c.rect(15*mm, y_table, width - 30*mm, 10*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18*mm, y_table + 3*mm, "Description")
    c.drawString(105*mm, y_table + 3*mm, "Qté")
    c.drawString(120*mm, y_table + 3*mm, "Unité")
    c.drawString(142*mm, y_table + 3*mm, "P.U. HT")
    c.drawRightString(width - 18*mm, y_table + 3*mm, "Total HT")
    
    y_ligne = y_table - 2*mm
    
    # Dessiner les lignes
    for i, prestation in enumerate(data.prestations):
        # Calculer hauteur de ligne selon contenu
        hauteur_ligne = 10*mm
        description_principale = prestation.description if hasattr(prestation, 'description') else ''
        description_detaillee = getattr(prestation, 'description_detaillee', '') or ''
        notes = getattr(prestation, 'notes', '') or ''
        
        if description_detaillee or notes:
            lignes_detail = []
            if description_detaillee:
                mots_detail = description_detaillee.split()
                ligne_courante = ""
                for mot in mots_detail:
                    if len(ligne_courante + " " + mot) <= 45:
                        ligne_courante = (ligne_courante + " " + mot).strip()
                    else:
                        if ligne_courante:
                            lignes_detail.append(ligne_courante)
                        ligne_courante = mot
                if ligne_courante:
                    lignes_detail.append(ligne_courante)
            
            nb_lignes_detail = len(lignes_detail)
            if notes:
                nb_lignes_detail += 1
            
            if nb_lignes_detail > 0:
                hauteur_ligne = (10 + (nb_lignes_detail * 4)) * mm
        
        y_ligne -= hauteur_ligne
        
        # Pour facture d'acompte avec valeurs fournies, utiliser total_ht_final pour l'affichage
        if is_facture_acompte and total_ttc_fourni is not None:
            total_ligne = total_ht_final  # Utiliser le HT fourni pour l'affichage
        else:
            total_ligne = prestation.quantite * prestation.prix_unitaire
            # Séparer les prestations positives et les acomptes (négatifs)
            if total_ligne >= 0:
                total_ht_avant_acompte += total_ligne
            else:
                total_acompte += abs(total_ligne)
        
        print(f"🔍 DEBUG prestation - description: {getattr(prestation, 'description', 'N/A')}, quantite: {prestation.quantite}, prix_unitaire: {prestation.prix_unitaire}, total_ligne: {total_ligne}")
        
        # Alterner les couleurs de fond
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - hauteur_ligne + 10*mm, width - 30*mm, hauteur_ligne, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        
        # Description principale
        y_text = y_ligne + 2*mm
        c.drawString(18*mm, y_text, tronquer_texte(description_principale, 50))
        
        # Description détaillée et notes
        y_detail = y_ligne - 3*mm
        if description_detaillee:
            c.setFont("Helvetica", 7)
            c.setFillColor(HexColor('#666666'))
            for ligne_detail in lignes_detail[:3]:
                c.drawString(18*mm, y_detail, tronquer_texte(ligne_detail, 60))
                y_detail -= 3.5*mm
        
        if notes:
            c.setFont("Helvetica-Oblique", 7)
            c.setFillColor(HexColor('#888888'))
            notes_texte = f"Note: {tronquer_texte(notes, 55)}"
            c.drawString(18*mm, y_detail, notes_texte)
            y_detail -= 3.5*mm
        
        # Colonnes standard
        c.setFont("Helvetica", 9)
        c.setFillColor(GRIS_FONCE)
        c.drawString(107*mm, y_ligne + 2*mm, str(prestation.quantite))
        c.drawString(120*mm, y_ligne + 2*mm, prestation.unite if hasattr(prestation, 'unite') else 'u')
        c.drawString(142*mm, y_ligne + 2*mm, f"{prestation.prix_unitaire:.2f} €")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne:.2f} €")
    
    y_ligne -= 5*mm
    
    # Ligne de séparation
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    y_totaux = y_ligne - 10*mm
    
    # Calculer les totaux finaux
    if is_facture_acompte and total_ttc_fourni is not None:
        # Les valeurs ont déjà été calculées au début de la fonction
        # total_ttc, total_ht_final, montant_tva sont déjà définis
        # IMPORTANT: Ne PAS recalculer total_ttc, utiliser celui fourni
        total_ht = total_ht_final
        remise = 0
        total_ht_apres_remise = total_ht_final
        # montant_tva a déjà été calculé au début de la fonction
        # total_ttc a déjà été défini au début de la fonction (ligne ~742)
        print(f"✅ Utilisation des totaux fournis pour facture d'acompte - HT: {total_ht_final:.2f}, TVA: {montant_tva:.2f}, TTC: {total_ttc:.2f}")
        print(f"   Vérification: total_ttc_fourni={total_ttc_fourni}, total_ttc={total_ttc}")
    else:
        # Calcul normal pour factures classiques
        # Calcul de la remise
        remise = 0
        if hasattr(data, 'remise_type') and data.remise_type and hasattr(data, 'remise_valeur') and data.remise_valeur and data.remise_valeur > 0:
            if data.remise_type == "pourcentage":
                remise = total_ht_avant_acompte * (data.remise_valeur / 100)
            elif data.remise_type == "montant":
                remise = data.remise_valeur
        
        # Appliquer la remise, puis déduire l'acompte
        total_ht_apres_remise = total_ht_avant_acompte - remise
        total_ht_final = total_ht_apres_remise - total_acompte
        montant_tva = total_ht_final * (tva_taux / 100)
        total_ttc = total_ht_final + montant_tva
        # Pour l'affichage, utiliser le total HT avant remise et acompte
        total_ht = total_ht_avant_acompte
    
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
        c.drawRightString(x_value, y_totaux - y_offset, f"{total_ht_apres_remise:.2f} €")
        y_offset += 6*mm
    
    # Afficher l'acompte si présent
    if total_acompte > 0:
        c.drawString(x_label, y_totaux - y_offset, "Acompte déduit")
        c.setFillColor(HexColor('#e74c3c'))
        c.drawRightString(x_value, y_totaux - y_offset, f"-{total_acompte:.2f} €")
        c.setFillColor(GRIS_FONCE)
        y_offset += 6*mm
    
    if tva_taux > 0:
        c.drawString(x_label, y_totaux - y_offset, f"TVA ({tva_taux}%)")
        c.drawRightString(x_value, y_totaux - y_offset, f"{montant_tva:.2f} €")
        y_offset += 6*mm
    else:
        c.drawString(x_label, y_totaux - y_offset, "TVA non applicable")
        y_offset += 6*mm
    
    # Total TTC
    # Pour facture d'acompte, s'assurer qu'on utilise bien le total_ttc fourni
    print(f"🔍 AVANT calcul TOTAL TTC - is_facture_acompte: {is_facture_acompte}, total_ttc_fourni: {total_ttc_fourni}, total_ttc (calculé): {total_ttc}")
    if is_facture_acompte and total_ttc_fourni is not None:
        # Convertir en float si nécessaire (peut être string, int, ou float)
        try:
            if isinstance(total_ttc_fourni, str):
                total_ttc_final = float(total_ttc_fourni.replace(',', '.'))
            else:
                total_ttc_final = float(total_ttc_fourni)
        except (ValueError, TypeError) as e:
            print(f"❌ ERREUR conversion total_ttc_fourni pour TOTAL TTC: {e}, valeur: {total_ttc_fourni}")
            total_ttc_final = total_ttc
        print(f"✅ FACTURE D'ACOMPTE - Utilisation de total_ttc fourni pour TOTAL TTC: {total_ttc_fourni} (type: {type(total_ttc_fourni)}) -> {total_ttc_final:.2f}")
        print(f"   Comparaison: total_ttc_fourni={total_ttc_fourni}, total_ttc_final={total_ttc_final:.2f}, total_ttc (calculé)={total_ttc:.2f}")
    else:
        total_ttc_final = total_ttc
        print(f"🔍 Utilisation total_ttc calculé: {total_ttc_final:.2f}")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(x_label, y_totaux - y_offset, "TOTAL TTC")
    
    # DERNIÈRE VÉRIFICATION : S'assurer qu'on utilise bien total_ttc_fourni pour les factures d'acompte
    if is_facture_acompte and total_ttc_fourni is not None:
        # Forcer l'utilisation de total_ttc_fourni
        try:
            if isinstance(total_ttc_fourni, str):
                total_ttc_final_force = float(total_ttc_fourni.replace(',', '.'))
            else:
                total_ttc_final_force = float(total_ttc_fourni)
            print(f"🔒 FORCE total_ttc_final = {total_ttc_final_force:.2f} (depuis total_ttc_fourni={total_ttc_fourni})")
            total_ttc_final = total_ttc_final_force
        except (ValueError, TypeError) as e:
            print(f"❌ ERREUR conversion finale total_ttc_fourni: {e}, valeur: {total_ttc_fourni}")
            print(f"   Utilisation de total_ttc_final précédent: {total_ttc_final:.2f}")
    
    c.drawRightString(x_value, y_totaux - y_offset, f"{total_ttc_final:.2f} €")
    
    print(f"🔍 RETOUR dessiner_tableau_prestations - total_ht_final: {total_ht_final:.2f}, total_ttc_final: {total_ttc_final:.2f}")
    print(f"   is_facture_acompte: {is_facture_acompte}, total_ttc_fourni: {total_ttc_fourni}")
    print(f"   ✅ VALEUR FINALE AFFICHÉE DANS PDF: {total_ttc_final:.2f} €")
    
    return y_totaux - y_offset - 5*mm, total_ht_final, total_ttc_final


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


def generer_pdf_devis(data: DevisRequest, numero_devis_force: Optional[str] = None) -> str:
    # PRIORITÉ 1: Utiliser le numéro forcé (paramètre explicite)
    # PRIORITÉ 2: Utiliser le numéro fourni dans data.numero_devis
    # PRIORITÉ 3: Générer un nouveau numéro (ne devrait jamais arriver)
    
    if numero_devis_force and str(numero_devis_force).strip():
        numero_devis = str(numero_devis_force).strip()
        print(f"✅ Utilisation du numéro de devis FORCÉ (paramètre): '{numero_devis}'")
    elif data.numero_devis and str(data.numero_devis).strip():
        numero_devis = str(data.numero_devis).strip()
        print(f"✅ Utilisation du numéro de devis fourni dans data: '{numero_devis}'")
    else:
        # Si aucun numéro n'est fourni, c'est une erreur critique
        numero_devis = f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        print(f"❌ ERREUR CRITIQUE: numero_devis non fourni ou vide!")
        print(f"   - numero_devis_force = '{numero_devis_force}'")
        print(f"   - data.numero_devis = '{data.numero_devis}'")
        print(f"   - Génération d'un nouveau numéro (ce ne devrait pas arriver): {numero_devis}")
        print(f"⚠️ ATTENTION: Le numéro généré ({numero_devis}) ne correspondra pas au numéro en base de données!")
    
    filename = f"{numero_devis}.pdf"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    date_validite = (datetime.now() + timedelta(days=data.validite_jours)).strftime("%d/%m/%Y")
    
    logo = telecharger_logo(data.entreprise.logo_url)
    
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    
    # Calculer les totaux globaux sur toutes les prestations
    total_ht_avant_acompte = 0
    total_acompte = 0
    for prestation in data.prestations:
        total_ligne = prestation.quantite * prestation.prix_unitaire
        if total_ligne >= 0:
            total_ht_avant_acompte += total_ligne
        else:
            total_acompte += abs(total_ligne)
    
    # Calcul de la remise directement à partir de data.remise_type et data.remise_valeur
    remise_type = getattr(data, 'remise_type', None)
    remise_valeur = getattr(data, 'remise_valeur', 0) or 0
    
    # Normaliser remise_type
    if remise_type:
        remise_type = str(remise_type).strip()
        if remise_type == "" or remise_type.lower() == "none":
            remise_type = None
    
    # Convertir remise_valeur en nombre
    try:
        remise_valeur = float(remise_valeur)
    except (ValueError, TypeError):
        remise_valeur = 0
    
    # Calculer remise_totale à partir de remise_type et remise_valeur
    if remise_type == "pourcentage" and remise_valeur > 0:
        remise = total_ht_avant_acompte * (remise_valeur / 100)
        print(f"✅ Remise pourcentage calculée: {remise:.2f} € ({remise_valeur}% de {total_ht_avant_acompte:.2f})")
    elif remise_type in ["montant", "fixe"] and remise_valeur > 0:
        remise = remise_valeur
        print(f"✅ Remise montant calculée: {remise:.2f} €")
    else:
        remise = 0
        if remise_type:
            print(f"⚠️ Remise_type défini ('{remise_type}') mais remise_valeur invalide: {remise_valeur}")
        else:
            print(f"ℹ️ Pas de remise définie")
    
    # Appliquer la remise, puis déduire l'acompte
    total_ht_apres_remise = total_ht_avant_acompte - remise
    total_ht_final = total_ht_apres_remise - total_acompte
    montant_tva = total_ht_final * (data.tva_taux / 100)
    total_ttc = total_ht_final + montant_tva
    total_ht = total_ht_avant_acompte  # Pour l'affichage
    
    # Stocker la remise dans data pour qu'elle soit accessible dans dessiner_totaux
    # On utilise une approche différente : on va passer les valeurs directement
    print(f"📋 Données finales - remise: {remise:.2f}, remise_type dans data: '{getattr(data, 'remise_type', None)}', remise_valeur dans data: {getattr(data, 'remise_valeur', None)}")
    
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
            
            # Log avant dessiner_totaux pour vérifier les valeurs
            print(f"📊 AVANT dessiner_totaux - remise: {remise:.2f}, remise_type: '{getattr(data, 'remise_type', None)}', remise_valeur: {getattr(data, 'remise_valeur', None)}")
            
            # Dessiner les totaux
            y_fin_totaux = dessiner_totaux(c, width, y_totaux, total_ht, total_ht_avant_acompte, total_acompte, remise, data.tva_taux, total_ht_final, total_ttc, data)
            
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


def generer_pdf_facture(data: FactureRequest, numero_facture_force: Optional[str] = None) -> str:
    # PRIORITÉ 1: Utiliser le numéro forcé (paramètre explicite)
    # PRIORITÉ 2: Utiliser le numéro fourni dans data.numero_facture
    # PRIORITÉ 3: Générer un nouveau numéro (ne devrait jamais arriver)
    
    if numero_facture_force and str(numero_facture_force).strip():
        numero_facture = str(numero_facture_force).strip()
        print(f"✅ Facture PDF - Utilisation du numéro FORCÉ (paramètre): '{numero_facture}'")
    elif data.numero_facture and str(data.numero_facture).strip():
        numero_facture = str(data.numero_facture).strip()
        print(f"✅ Facture PDF - Utilisation du numéro fourni dans data: '{numero_facture}'")
    else:
        # Si aucun numéro n'est fourni, c'est une erreur critique
        numero_facture = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        print(f"❌ ERREUR CRITIQUE: numero_facture non fourni ou vide!")
        print(f"   - numero_facture_force = '{numero_facture_force}'")
        print(f"   - data.numero_facture = '{data.numero_facture}'")
        print(f"   - Génération d'un nouveau numéro (ce ne devrait pas arriver): {numero_facture}")
        print(f"⚠️ ATTENTION: Le numéro généré ({numero_facture}) ne correspondra pas au numéro en base de données!")
    
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
    print(f"🔍 RETOUR generer_pdf_facture - total_ht: {total_ht:.2f}, total_ttc: {total_ttc:.2f}")
    print(f"   is_facture_acompte: {getattr(data, 'is_facture_acompte', False)}, total_ttc fourni: {getattr(data, 'total_ttc', None)}")
    
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

def generer_word_devis(data: DevisRequest, numero_devis_force: Optional[str] = None) -> str:
    """Génère un devis au format Word"""
    # PRIORITÉ 1: Utiliser le numéro forcé (paramètre explicite)
    # PRIORITÉ 2: Utiliser le numéro fourni dans data.numero_devis
    # PRIORITÉ 3: Générer un nouveau numéro (ne devrait jamais arriver)
    
    if numero_devis_force and str(numero_devis_force).strip():
        numero_devis = str(numero_devis_force).strip()
        print(f"✅ Word - Utilisation du numéro de devis FORCÉ (paramètre): '{numero_devis}'")
    elif data.numero_devis and str(data.numero_devis).strip():
        numero_devis = str(data.numero_devis).strip()
        print(f"✅ Word - Utilisation du numéro de devis fourni dans data: '{numero_devis}'")
    else:
        numero_devis = f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        print(f"⚠️ Word - numero_devis non fourni ou vide, génération d'un nouveau numéro: {numero_devis}")
    
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


def generer_word_facture(data: FactureRequest, numero_facture_force: Optional[str] = None) -> str:
    """Génère une facture au format Word"""
    # PRIORITÉ 1: Utiliser le numéro forcé (paramètre explicite)
    # PRIORITÉ 2: Utiliser le numéro fourni dans data.numero_facture
    # PRIORITÉ 3: Générer un nouveau numéro (ne devrait jamais arriver)
    
    if numero_facture_force and str(numero_facture_force).strip():
        numero_facture = str(numero_facture_force).strip()
        print(f"✅ Facture Word - Utilisation du numéro FORCÉ (paramètre): '{numero_facture}'")
    elif data.numero_facture and str(data.numero_facture).strip():
        numero_facture = str(data.numero_facture).strip()
        print(f"✅ Facture Word - Utilisation du numéro fourni dans data: '{numero_facture}'")
    else:
        numero_facture = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        print(f"⚠️ Facture Word - numero_facture non fourni ou vide, génération d'un nouveau numéro: {numero_facture}")
    
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
        # IMPORTANT: Récupérer le numéro AVANT toute autre opération
        # Si Pydantic n'a pas reçu le champ, il sera None
        numero_devis_recu = None
        
        # Essayer de récupérer depuis data.numero_devis
        if hasattr(data, 'numero_devis') and data.numero_devis:
            numero_devis_recu = str(data.numero_devis).strip()
            print(f"✅ Numéro de devis récupéré depuis data.numero_devis: '{numero_devis_recu}'")
        else:
            print(f"❌ ERREUR: data.numero_devis est None ou vide!")
            print(f"   - data.numero_devis = '{data.numero_devis}'")
            print(f"   - Type: {type(data.numero_devis)}")
            print(f"   - hasattr(data, 'numero_devis'): {hasattr(data, 'numero_devis')}")
            raise HTTPException(status_code=400, detail="Le numéro de devis est obligatoire et n'a pas été fourni dans la requête")
        
        if not numero_devis_recu or not numero_devis_recu.strip():
            print(f"❌ ERREUR CRITIQUE: Numéro de devis vide après traitement!")
            raise HTTPException(status_code=400, detail="Le numéro de devis est obligatoire")
        
        print(f"📄 Début génération devis pour client: {data.client.nom}")
        print(f"📊 Nombre de prestations: {len(data.prestations)}")
        print(f"🎨 Couleur PDF: {data.entreprise.couleur_pdf or 'défaut'}")
        print(f"📋 Numéro de devis à utiliser: '{numero_devis_recu}'")
        print(f"💰 Remise - type: '{data.remise_type}', valeur: {data.remise_valeur}, type valeur: {type(data.remise_valeur)}")
        
        # FORCER l'utilisation du numéro reçu en mettant à jour data.numero_devis
        # Utiliser model_copy pour Pydantic v2 ou copy pour v1
        try:
            if hasattr(data, 'model_copy'):
                data = data.model_copy(update={'numero_devis': numero_devis_recu})
            else:
                data.numero_devis = numero_devis_recu
            print(f"✅ data.numero_devis mis à jour avec: '{data.numero_devis}'")
        except Exception as e:
            print(f"⚠️ Impossible de mettre à jour data.numero_devis: {e}")
            # Créer un nouveau dict avec le numéro forcé
            data_dict = data.model_dump() if hasattr(data, 'model_dump') else data.dict()
            data_dict['numero_devis'] = numero_devis_recu
            data = DevisRequest(**data_dict)
            print(f"✅ data recréé avec numero_devis: '{data.numero_devis}'")
        
        # Générer PDF avec le numéro FORCÉ (paramètre explicite)
        print("📝 Génération PDF...")
        filepath_pdf, numero_devis_pdf, total_ht, total_ttc = generer_pdf_devis(data, numero_devis_force=numero_devis_recu)
        print(f"✅ PDF généré: {filepath_pdf}")
        print(f"📋 Numéro de devis utilisé dans PDF: '{numero_devis_pdf}'")
        print(f"📋 Numéro de devis reçu initialement: '{numero_devis_recu}'")
        
        # Le numéro utilisé DOIT correspondre au numéro reçu (on l'a forcé)
        if numero_devis_pdf != numero_devis_recu:
            print(f"❌ ERREUR CRITIQUE: Le numéro utilisé ({numero_devis_pdf}) diffère du numéro reçu ({numero_devis_recu})")
            print(f"   - Le PDF contient probablement le mauvais numéro!")
            # Renommer le fichier PDF pour correspondre au bon numéro
            correct_pdf_path = os.path.join(PDF_FOLDER, f"{numero_devis_recu}.pdf")
            if os.path.exists(filepath_pdf) and filepath_pdf != correct_pdf_path:
                print(f"🔄 Renommage du PDF de '{filepath_pdf}' vers '{correct_pdf_path}'")
                os.rename(filepath_pdf, correct_pdf_path)
                filepath_pdf = correct_pdf_path
            numero_devis_final = numero_devis_recu
        else:
            numero_devis_final = numero_devis_pdf
            print(f"✅ Le numéro de devis est cohérent: {numero_devis_final}")
        
        # Générer Word avec le numéro FORCÉ (paramètre explicite)
        print("📝 Génération Word...")
        filepath_word, numero_devis_word, _, _ = generer_word_devis(data, numero_devis_force=numero_devis_recu)
        # Renommer le Word pour avoir le même numéro que le PDF
        new_word_path = os.path.join(PDF_FOLDER, f"{numero_devis_final}.docx")
        if os.path.exists(filepath_word) and filepath_word != new_word_path:
            os.rename(filepath_word, new_word_path)
        print(f"✅ Word généré: {new_word_path}")
        
        # Upload sur Supabase Storage
        print("📤 Upload PDF sur Supabase...")
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_devis_final}.pdf")
        print(f"✅ PDF uploadé: {pdf_url}")
        
        print("📤 Upload Word sur Supabase...")
        word_url = upload_to_supabase(new_word_path, f"{numero_devis_final}.docx")
        print(f"✅ Word uploadé: {word_url}")
        
        return {
            "success": True,
            "numero_devis": numero_devis_final,  # IMPORTANT: Retourner le numéro final (celui du dashboard)
            "total_ht": total_ht,
            "total_ttc": total_ttc,
            "pdf_filename": f"{numero_devis_final}.pdf",
            "pdf_url": pdf_url,
            "word_filename": f"{numero_devis_final}.docx",
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
            remise_valeur=data.devis_data.remise_valeur or 0,
            numero_devis=None  # Pour l'IA, on peut générer un nouveau numéro
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
        # DEBUG: Vérifier immédiatement is_facture_acompte AVANT toute autre opération
        print(f"🔍 IMMÉDIAT - data.is_facture_acompte (direct): {data.is_facture_acompte if hasattr(data, 'is_facture_acompte') else 'ATTRIBUT NON TROUVÉ'}")
        print(f"🔍 IMMÉDIAT - type(data.is_facture_acompte): {type(data.is_facture_acompte) if hasattr(data, 'is_facture_acompte') else 'N/A'}")
        print(f"🔍 IMMÉDIAT - data.total_ttc (direct): {data.total_ttc if hasattr(data, 'total_ttc') else 'ATTRIBUT NON TROUVÉ'}")
        print(f"🔍 IMMÉDIAT - data.total_ht (direct): {data.total_ht if hasattr(data, 'total_ht') else 'ATTRIBUT NON TROUVÉ'}")
        
        # IMPORTANT: Récupérer le numéro AVANT toute autre opération
        # Si Pydantic n'a pas reçu le champ, il sera None
        numero_facture_recu = None
        
        # Essayer de récupérer depuis data.numero_facture
        if hasattr(data, 'numero_facture') and data.numero_facture:
            numero_facture_recu = str(data.numero_facture).strip()
            print(f"✅ Numéro de facture récupéré depuis data.numero_facture: '{numero_facture_recu}'")
        else:
            # Si le numéro n'est pas fourni, générer un numéro par défaut (pour rétrocompatibilité)
            # mais logger un avertissement
            numero_facture_recu = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            print(f"⚠️ AVERTISSEMENT: data.numero_facture est None ou vide!")
            print(f"   - data.numero_facture = '{data.numero_facture}'")
            print(f"   - Type: {type(data.numero_facture)}")
            print(f"   - hasattr(data, 'numero_facture'): {hasattr(data, 'numero_facture')}")
            print(f"   - Génération d'un numéro par défaut: '{numero_facture_recu}'")
            print(f"   - ⚠️ Ce numéro pourrait ne pas correspondre au numéro en base de données!")
        
        if not numero_facture_recu or not numero_facture_recu.strip():
            # Dernière vérification de sécurité
            numero_facture_recu = f"FAC-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            print(f"⚠️ Numéro de facture vide après traitement, génération d'un numéro par défaut: '{numero_facture_recu}'")
        
        print(f"📄 Début génération facture pour client: {data.client.nom}")
        print(f"📊 Nombre de prestations: {len(data.prestations)}")
        print(f"🎨 Couleur PDF: {data.entreprise.couleur_pdf or 'défaut'}")
        print(f"📋 Numéro de facture à utiliser: '{numero_facture_recu}'")
        
        # DEBUG: Vérifier les valeurs pour facture d'acompte
        # Utiliser directement data.is_facture_acompte (Pydantic devrait le gérer)
        # Si None, cela signifie que le champ n'a pas été envoyé, donc on considère que ce n'est PAS une facture d'acompte
        is_facture_acompte = data.is_facture_acompte if data.is_facture_acompte is not None else False
        # Forcer en booléen pour être sûr
        is_facture_acompte = bool(is_facture_acompte)
        
        # DEBUG supplémentaire : vérifier model_dump pour voir toutes les valeurs
        try:
            data_dict = data.model_dump()
            print(f"🔍 DEBUG model_dump complet - is_facture_acompte: {data_dict.get('is_facture_acompte')}")
            print(f"🔍 DEBUG model_dump complet - total_ttc: {data_dict.get('total_ttc')}")
            print(f"🔍 DEBUG model_dump complet - total_ht: {data_dict.get('total_ht')}")
        except Exception as e:
            print(f"   ⚠️ Erreur model_dump: {e}")
        
        total_ttc_recu = None
        total_ht_recu = None
        if hasattr(data, 'total_ttc'):
            total_ttc_recu = data.total_ttc
        if hasattr(data, 'total_ht'):
            total_ht_recu = data.total_ht
        if total_ttc_recu is None:
            total_ttc_recu = getattr(data, 'total_ttc', None)
        if total_ht_recu is None:
            total_ht_recu = getattr(data, 'total_ht', None)
        # Essayer aussi model_dump si disponible
        if hasattr(data, 'model_dump'):
            try:
                data_dict = data.model_dump()
                if total_ttc_recu is None:
                    total_ttc_recu = data_dict.get('total_ttc')
                if total_ht_recu is None:
                    total_ht_recu = data_dict.get('total_ht')
            except Exception as e:
                print(f"   ⚠️ Erreur model_dump pour total_ttc/total_ht: {e}")
        
        print(f"🔍 DEBUG FACTURE ACOMPTE (generer_facture_endpoint):")
        print(f"   is_facture_acompte: {is_facture_acompte} (type: {type(is_facture_acompte)})")
        print(f"   hasattr(data, 'is_facture_acompte'): {hasattr(data, 'is_facture_acompte')}")
        if hasattr(data, 'is_facture_acompte'):
            print(f"   data.is_facture_acompte direct: {data.is_facture_acompte} (type: {type(data.is_facture_acompte)})")
        print(f"   total_ttc reçu: {total_ttc_recu} (type: {type(total_ttc_recu)}, valeur brute: {repr(total_ttc_recu)})")
        print(f"   total_ht reçu: {total_ht_recu} (type: {type(total_ht_recu)}, valeur brute: {repr(total_ht_recu)})")
        if data.prestations and len(data.prestations) > 0:
            print(f"   prix_unitaire prestation: {data.prestations[0].prix_unitaire} (type: {type(data.prestations[0].prix_unitaire)})")
            print(f"   quantite prestation: {data.prestations[0].quantite}")
        # Vérifier si les valeurs sont bien des nombres
        if total_ttc_recu is not None:
            try:
                total_ttc_float = float(total_ttc_recu)
                print(f"   ✅ total_ttc converti en float: {total_ttc_float:.2f}")
            except (ValueError, TypeError) as e:
                print(f"   ❌ ERREUR conversion total_ttc: {e}")
        if total_ht_recu is not None:
            try:
                total_ht_float = float(total_ht_recu)
                print(f"   ✅ total_ht converti en float: {total_ht_float:.2f}")
            except (ValueError, TypeError) as e:
                print(f"   ❌ ERREUR conversion total_ht: {e}")
        
        # FORCER l'utilisation du numéro reçu en mettant à jour data.numero_facture
        try:
            if hasattr(data, 'model_copy'):
                data = data.model_copy(update={'numero_facture': numero_facture_recu})
            else:
                data.numero_facture = numero_facture_recu
            print(f"✅ data.numero_facture mis à jour avec: '{data.numero_facture}'")
        except Exception as e:
            print(f"⚠️ Impossible de mettre à jour data.numero_facture: {e}")
            # Créer un nouveau dict avec le numéro forcé
            data_dict = data.model_dump() if hasattr(data, 'model_dump') else data.dict()
            data_dict['numero_facture'] = numero_facture_recu
            data = FactureRequest(**data_dict)
            print(f"✅ data recréé avec numero_facture: '{data.numero_facture}'")
        
        # Générer PDF avec le numéro forcé
        filepath_pdf, numero_facture_pdf, total_ht, total_ttc = generer_pdf_facture(data, numero_facture_force=numero_facture_recu)
        
        # Vérifier que le numéro utilisé correspond bien au numéro reçu
        if numero_facture_pdf != numero_facture_recu:
            print(f"❌ ERREUR CRITIQUE: Le numéro utilisé ({numero_facture_pdf}) diffère du numéro reçu ({numero_facture_recu})")
            # Utiliser le numéro reçu (celui du dashboard) - c'est la source de vérité
            numero_facture_final = numero_facture_recu
            # Renommer le fichier PDF pour correspondre au bon numéro
            correct_pdf_path = os.path.join(PDF_FOLDER, f"{numero_facture_final}.pdf")
            if os.path.exists(filepath_pdf) and filepath_pdf != correct_pdf_path:
                print(f"🔄 Renommage du PDF de '{filepath_pdf}' vers '{correct_pdf_path}'")
                os.rename(filepath_pdf, correct_pdf_path)
                filepath_pdf = correct_pdf_path
        else:
            numero_facture_final = numero_facture_pdf
            print(f"✅ Le numéro de facture est cohérent: {numero_facture_final}")
        
        # Générer Word avec le numéro forcé
        filepath_word, _, _, _ = generer_word_facture(data, numero_facture_force=numero_facture_recu)
        new_word_path = os.path.join(PDF_FOLDER, f"{numero_facture_final}.docx")
        if os.path.exists(filepath_word) and filepath_word != new_word_path:
            print(f"🔄 Renommage du Word de '{filepath_word}' vers '{new_word_path}'")
            os.rename(filepath_word, new_word_path)
        
        # Upload sur Supabase Storage
        pdf_url = upload_to_supabase(filepath_pdf, f"{numero_facture_final}.pdf")
        word_url = upload_to_supabase(new_word_path, f"{numero_facture_final}.docx")
        
        return {
            "success": True,
            "numero_facture": numero_facture_final,
            "total_ht": total_ht,
            "total_ttc": total_ttc,
            "pdf_filename": f"{numero_facture_final}.pdf",
            "pdf_url": pdf_url,
            "word_filename": f"{numero_facture_final}.docx",
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
