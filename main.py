"""
MonDevisPro API
Génère des devis PDF professionnels
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

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

app = FastAPI(
    title="MonDevisPro API",
    description="API de génération de devis PDF professionnels",
    version="1.1.0"
)

# CORS pour permettre les appels depuis Make.com et le site web
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dossier pour stocker les PDFs générés
PDF_FOLDER = "generated_pdfs"
os.makedirs(PDF_FOLDER, exist_ok=True)

# Couleurs
BLEU_PRINCIPAL = HexColor('#1a5276')
BLEU_CLAIR = HexColor('#3498db')
GRIS_FONCE = HexColor('#2c3e50')
GRIS_CLAIR = HexColor('#ecf0f1')
GRIS_TEXTE = HexColor('#555555')
ORANGE_ACCENT = HexColor('#e67e22')


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
    cp_ville: Optional[str] = ""
    tel: str
    email: str
    logo_url: Optional[str] = None

class Client(BaseModel):
    nom: str
    adresse: Optional[str] = ""
    cp_ville: Optional[str] = ""
    tel: Optional[str] = ""

class DevisRequest(BaseModel):
    entreprise: Entreprise
    client: Client
    prestations: List[Prestation]
    tva_taux: float = 20.0
    conditions_paiement: str = "30% à la commande, solde à réception"
    delai_realisation: str = "À définir"
    validite_jours: int = 30

class DevisDataFromAI(BaseModel):
    client_nom: str
    prestations: List[Prestation]
    delai: str = "À définir"

class DevisRequestSimple(BaseModel):
    entreprise: Entreprise
    devis_data: DevisDataFromAI


# ==================== FONCTIONS UTILITAIRES ====================

def telecharger_logo(logo_url: str) -> Optional[ImageReader]:
    """Télécharge le logo depuis l'URL et retourne un ImageReader"""
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


def tronquer_texte(texte: str, max_chars: int) -> str:
    """Tronque le texte s'il dépasse la limite"""
    if len(texte) <= max_chars:
        return texte
    return texte[:max_chars-3] + "..."


def formater_adresse_complete(adresse: str, cp_ville: str) -> str:
    """Combine adresse et cp_ville sur une seule ligne"""
    parties = []
    if adresse and adresse.strip():
        parties.append(adresse.strip())
    if cp_ville and cp_ville.strip():
        parties.append(cp_ville.strip())
    return ", ".join(parties) if parties else ""


# ==================== GÉNÉRATION PDF ====================

def generer_pdf(data: DevisRequest) -> str:
    """Génère un PDF de devis et retourne le chemin du fichier"""
    
    # Générer un nom de fichier unique
    numero_devis = f"DEV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    filename = f"{numero_devis}.pdf"
    filepath = os.path.join(PDF_FOLDER, filename)
    
    # Dates
    date_devis = datetime.now().strftime("%d/%m/%Y")
    date_validite = (datetime.now() + timedelta(days=data.validite_jours)).strftime("%d/%m/%Y")
    
    # Télécharger le logo
    logo = telecharger_logo(data.entreprise.logo_url)
    
    # Création du canvas
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    
    # ==================== EN-TÊTE ====================
    
    c.setFillColor(BLEU_PRINCIPAL)
    c.rect(0, height - 45*mm, width, 45*mm, fill=True, stroke=False)
    
    # Position X de départ pour le texte (après le logo si présent)
    text_start_x = 15*mm
    
    # Logo (si disponible)
    if logo:
        try:
            # Logo en haut à gauche, taille 35x35mm max
            logo_size = 30*mm
            logo_x = 15*mm
            logo_y = height - 40*mm
            c.drawImage(logo, logo_x, logo_y, width=logo_size, height=logo_size, preserveAspectRatio=True, mask='auto')
            text_start_x = 50*mm  # Décaler le texte après le logo
        except Exception as e:
            print(f"Erreur affichage logo: {e}")
    
    # Nom entreprise
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    nom_entreprise = tronquer_texte(data.entreprise.nom.upper(), 30)
    c.drawString(text_start_x, height - 18*mm, nom_entreprise)
    
    # Gérant (si renseigné)
    if data.entreprise.gerant and data.entreprise.gerant.strip():
        c.setFont("Helvetica", 9)
        c.drawString(text_start_x, height - 26*mm, f"Gérant : {data.entreprise.gerant}")
    
    # DEVIS à droite
    c.setFont("Helvetica-Bold", 28)
    c.drawRightString(width - 20*mm, height - 18*mm, "DEVIS")
    
    c.setFont("Helvetica", 11)
    c.drawRightString(width - 20*mm, height - 28*mm, f"N° {numero_devis}")
    
    # Date dans l'en-tête
    c.setFont("Helvetica", 9)
    c.drawRightString(width - 20*mm, height - 36*mm, f"Date : {date_devis}")
    
    # ==================== INFOS ENTREPRISE & CLIENT ====================
    
    y_position = height - 60*mm
    
    # Bloc entreprise
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(15*mm, y_position - 32*mm, 85*mm, 38*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(BLEU_PRINCIPAL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20*mm, y_position, "ÉMETTEUR")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    y_text = y_position - 8*mm
    
    # Nom entreprise
    c.drawString(20*mm, y_text, tronquer_texte(data.entreprise.nom, 40))
    
   # Adresse sur une ligne
    if data.entreprise.adresse:
        c.drawString(20*mm, y_text - 5*mm, tronquer_texte(data.entreprise.adresse, 42))
    
    # CP + Ville sur une autre ligne
    if data.entreprise.cp_ville:
        c.drawString(20*mm, y_text - 10*mm, tronquer_texte(data.entreprise.cp_ville, 42))
    
    # Téléphone
    c.drawString(20*mm, y_text - 15*mm, f"Tél : {data.entreprise.tel}")
    
    # Email (tronqué si nécessaire)
    c.drawString(20*mm, y_text - 20*mm, f"Email : {tronquer_texte(data.entreprise.email, 35)}")
    
    # SIRET
    c.drawString(20*mm, y_text - 25*mm, f"SIRET : {data.entreprise.siret}")
    
    # Bloc client
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(110*mm, y_position - 32*mm, 85*mm, 38*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(BLEU_PRINCIPAL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(115*mm, y_position, "DESTINATAIRE")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    y_text = y_position - 8*mm
    c.drawString(115*mm, y_text, data.client.nom)
    if data.client.adresse:
        c.drawString(115*mm, y_text - 5*mm, tronquer_texte(data.client.adresse, 40))
    if data.client.cp_ville:
        c.drawString(115*mm, y_text - 10*mm, data.client.cp_ville)
    if data.client.tel:
        c.drawString(115*mm, y_text - 15*mm, f"Tél : {data.client.tel}")
    
    # Validité
    c.setFillColor(GRIS_TEXTE)
    c.setFont("Helvetica", 9)
    c.drawRightString(width - 20*mm, y_position - 25*mm, f"Validité : {date_validite}")
    
    # ==================== TABLEAU PRESTATIONS ====================
    
    y_table = y_position - 50*mm
    
    # En-tête tableau
    c.setFillColor(BLEU_PRINCIPAL)
    c.rect(15*mm, y_table, width - 30*mm, 10*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18*mm, y_table + 3*mm, "Description")
    c.drawString(105*mm, y_table + 3*mm, "Qté")
    c.drawString(120*mm, y_table + 3*mm, "Unité")
    c.drawString(142*mm, y_table + 3*mm, "P.U. HT")
    c.drawRightString(width - 18*mm, y_table + 3*mm, "Total HT")
    
    # Lignes
    y_ligne = y_table - 2*mm
    total_ht = 0
    
    for i, prestation in enumerate(data.prestations):
        y_ligne -= 10*mm
        total_ligne = prestation.quantite * prestation.prix_unitaire
        total_ht += total_ligne
        
        if i % 2 == 0:
            c.setFillColor(HexColor('#f8f9fa'))
            c.rect(15*mm, y_ligne - 2*mm, width - 30*mm, 10*mm, fill=True, stroke=False)
        
        c.setFillColor(GRIS_FONCE)
        c.setFont("Helvetica", 9)
        c.drawString(18*mm, y_ligne + 2*mm, tronquer_texte(prestation.description, 50))
        c.drawString(107*mm, y_ligne + 2*mm, str(prestation.quantite))
        c.drawString(120*mm, y_ligne + 2*mm, prestation.unite)
        c.drawString(142*mm, y_ligne + 2*mm, f"{prestation.prix_unitaire:.2f} €")
        c.drawRightString(width - 18*mm, y_ligne + 2*mm, f"{total_ligne:.2f} €")
    
    # Ligne séparation
    y_ligne -= 5*mm
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.line(15*mm, y_ligne, width - 15*mm, y_ligne)
    
    # ==================== TOTAUX ====================
    
    y_totaux = y_ligne - 10*mm
    montant_tva = total_ht * (data.tva_taux / 100)
    total_ttc = total_ht + montant_tva
    
    x_label = 130*mm
    x_value = width - 18*mm
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 10)
    c.drawString(x_label, y_totaux, "Total HT")
    c.drawRightString(x_value, y_totaux, f"{total_ht:.2f} €")
    
    c.drawString(x_label, y_totaux - 6*mm, f"TVA ({data.tva_taux}%)")
    c.drawRightString(x_value, y_totaux - 6*mm, f"{montant_tva:.2f} €")
    
    c.setFillColor(BLEU_PRINCIPAL)
    c.roundRect(x_label - 5*mm, y_totaux - 20*mm, 68*mm, 10*mm, 2*mm, fill=True, stroke=False)
    
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x_label, y_totaux - 17*mm, "TOTAL TTC")
    c.drawRightString(x_value, y_totaux - 17*mm, f"{total_ttc:.2f} €")
    
    # ==================== CONDITIONS ====================
    
    y_conditions = y_totaux - 45*mm
    
    c.setFillColor(GRIS_CLAIR)
    c.roundRect(15*mm, y_conditions - 25*mm, width - 30*mm, 35*mm, 3*mm, fill=True, stroke=False)
    
    c.setFillColor(BLEU_PRINCIPAL)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20*mm, y_conditions + 2*mm, "CONDITIONS")
    
    c.setFillColor(GRIS_FONCE)
    c.setFont("Helvetica", 9)
    c.drawString(20*mm, y_conditions - 8*mm, f"• Délai de réalisation : {data.delai_realisation}")
    c.drawString(20*mm, y_conditions - 14*mm, f"• Conditions de paiement : {data.conditions_paiement}")
    c.drawString(20*mm, y_conditions - 20*mm, f"• Devis valable jusqu'au : {date_validite}")
    
    # ==================== SIGNATURE ====================
    
    y_signature = y_conditions - 53*mm
    
    c.setStrokeColor(GRIS_CLAIR)
    c.setLineWidth(1)
    c.roundRect(110*mm, y_signature - 10*mm, 80*mm, 40*mm, 3*mm, fill=False, stroke=True)
    
    c.setFillColor(GRIS_TEXTE)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(115*mm, y_signature + 22*mm, "Bon pour accord")
    
    c.setFont("Helvetica", 8)
    c.drawString(115*mm, y_signature + 12*mm, "Date :")
    c.drawString(115*mm, y_signature + 2*mm, "Signature :")
    
    c.setFont("Helvetica-Oblique", 7)
    c.drawString(115*mm, y_signature - 5*mm, "(Signature précédée de la mention \"Bon pour accord\")")
    
    # ==================== PIED DE PAGE ====================
    
    c.setStrokeColor(BLEU_PRINCIPAL)
    c.setLineWidth(2)
    c.line(15*mm, 25*mm, width - 15*mm, 25*mm)
    
    c.setFillColor(GRIS_TEXTE)
    c.setFont("Helvetica", 7)
    
    # Pied de page avec adresse complète
    adresse_pied = formater_adresse_complete(data.entreprise.adresse, data.entreprise.cp_ville)
    c.drawCentredString(width/2, 18*mm, f"{data.entreprise.nom} - SIRET {data.entreprise.siret}")
    c.drawCentredString(width/2, 13*mm, f"{adresse_pied} - Tél : {data.entreprise.tel}")
    
    # TVA intracommunautaire
    siret_clean = data.entreprise.siret.replace(' ', '').replace('.', '')
    c.drawCentredString(width/2, 8*mm, f"TVA intracommunautaire : FR{siret_clean[:9] if len(siret_clean) >= 9 else siret_clean}")
    
    c.setFillColor(BLEU_CLAIR)
    c.setFont("Helvetica-Oblique", 6)
    c.drawRightString(width - 15*mm, 4*mm, "Généré par MonDevisPro.fr")
    
    c.save()
    
    return filepath, numero_devis, total_ttc


# ==================== ROUTES API ====================

@app.get("/")
def root():
    return {"message": "MonDevisPro API", "version": "1.1.0", "status": "ok"}


@app.post("/generer-devis")
async def generer_devis_endpoint(data: DevisRequest):
    """Génère un devis PDF et retourne les informations"""
    try:
        filepath, numero_devis, total_ttc = generer_pdf(data)
        
        return {
            "success": True,
            "numero_devis": numero_devis,
            "total_ttc": total_ttc,
            "pdf_filename": os.path.basename(filepath),
            "pdf_url": f"/download/{os.path.basename(filepath)}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generer-devis-simple")
async def generer_devis_simple_endpoint(data: DevisRequestSimple):
    """Génère un devis PDF depuis le format simplifié (avec devis_data d'OpenAI)"""
    try:
        # Convertir le format simple vers le format complet
        full_data = DevisRequest(
            entreprise=data.entreprise,
            client=Client(
                nom=data.devis_data.client_nom,
                adresse="",
                cp_ville="",
                tel=""
            ),
            prestations=data.devis_data.prestations,
            tva_taux=20.0,
            conditions_paiement="30% à la commande, solde à réception",
            delai_realisation=data.devis_data.delai
        )
        
        filepath, numero_devis, total_ttc = generer_pdf(full_data)
        
        return {
            "success": True,
            "numero_devis": numero_devis,
            "total_ttc": total_ttc,
            "pdf_filename": os.path.basename(filepath),
            "pdf_url": f"/download/{os.path.basename(filepath)}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/{filename}")
async def download_pdf(filename: str):
    """Télécharge un PDF généré"""
    filepath = os.path.join(PDF_FOLDER, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="PDF non trouvé")
    
    return FileResponse(
        filepath,
        media_type="application/pdf",
        filename=filename
    )


@app.get("/health")
def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
